import httpx
import uuid
from typing import Optional, Dict, Any, List, Tuple
from config import settings

TONAPI_BASE = "https://tonapi.io/v2"

class TonAPI:
    def __init__(self, api_key: str = settings.TONAPI_KEY, timeout: float = 20.0):
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout, headers=self._headers, http2=True, base_url=TONAPI_BASE)

    async def close(self):
        await self._client.aclose()

    # Короткая инфа по адресу (health)
    async def get_account_info(self, address: str) -> Dict[str, Any]:
        r = await self._client.get(f"/accounts/{address}")
        r.raise_for_status()
        return r.json()

    # Входящие транзакции по адресу
    async def get_incoming_txs(self, address: str, limit: int = 50) -> List[Dict[str, Any]]:
        params = {"limit": limit}
        r = await self._client.get(f"/blockchain/accounts/{address}/transactions", params=params)
        r.raise_for_status()
        data = r.json()
        return data.get("transactions", [])

    # Поиск платежа по комментарию и сумме (в TON)
    async def find_payment_by_comment(
        self,
        address: str,
        comment_text: str,
        min_amount_ton: float = 0.1,
        limit: int = 50,
    ) -> Optional[Tuple[Dict[str, Any], int]]:
        """
        Ищем входящую транзакцию с текстом комментария = comment_text
        и суммой не меньше min_amount_ton.

        Возврат: (tx_dict, amount_nano) или None
        """
        txs = await self.get_incoming_txs(address, limit=limit)
        comment_text = (comment_text or "").strip()

        nano_min = int(min_amount_ton * 1_000_000_000)

        for tx in txs:
            # Часто комментарий и сумма лежат в in_msg
            in_msg = tx.get("in_msg", {}) or {}
            msg_text = in_msg.get("message") or in_msg.get("comment") or ""  # разные поля у TonAPI/версий
            value_nano = in_msg.get("value") or 0

            # На всякий — подстрахуемся, если структура иная
            if not msg_text and "in_msg" in tx and isinstance(tx["in_msg"], dict):
                # иногда текст бывает в payload/message
                msg_text = tx["in_msg"].get("payload") or ""

            if isinstance(msg_text, str) and msg_text.strip() == comment_text and int(value_nano) >= nano_min:
                return tx, int(value_nano)

        return None

    @staticmethod
    def build_ton_transfer_url(to_address: str, amount_ton: float, comment: Optional[str] = None) -> str:
        nano = int(amount_ton * 1_000_000_000)
        base = f"ton://transfer/{to_address}?amount={nano}"
        if comment:
            qp = httpx.QueryParams({"text": comment})
            base += f"&text={qp['text']}"
        return base

    @staticmethod
    def unique_comment(prefix: str = "nftbot") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
