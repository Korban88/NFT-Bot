import httpx
import uuid
from typing import Optional, Dict, Any, List
from config import settings

TONAPI_BASE = "https://tonapi.io/v2"

class TonAPI:
    def __init__(self, api_key: str = settings.TONAPI_KEY, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout, headers=self._headers, http2=True, base_url=TONAPI_BASE)

    async def close(self):
        await self._client.aclose()

    # Health check: краткая инфа по адресу
    async def get_account_info(self, address: str) -> Dict[str, Any]:
        r = await self._client.get(f"/accounts/{address}")
        r.raise_for_status()
        return r.json()

    # Входящие транзакции по адресу
    async def get_incoming_txs(self, address: str, limit: int = 20) -> List[Dict[str, Any]]:
        params = {"limit": limit}
        r = await self._client.get(f"/blockchain/accounts/{address}/transactions", params=params)
        r.raise_for_status()
        data = r.json()
        return data.get("transactions", [])

    @staticmethod
    def build_ton_transfer_url(to_address: str, amount_ton: float, comment: Optional[str] = None) -> str:
        # amount в nanoTON
        nano = int(amount_ton * 1_000_000_000)
        base = f"ton://transfer/{to_address}?amount={nano}"
        if comment:
            # Безопасное кодирование text=
            qp = httpx.QueryParams({"text": comment})
            base += f"&text={qp['text']}"
        return base

    @staticmethod
    def unique_comment(prefix: str = "nftbot") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
