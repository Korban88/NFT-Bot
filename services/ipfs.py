import httpx
from typing import Dict, Any, Tuple
from config import settings

PINATA_BASE = "https://api.pinata.cloud"

class PinataIPFS:
    def __init__(self, jwt: str = settings.PINATA_JWT, timeout: float = 25.0):
        jwt = (jwt or "").strip()
        # если по ошибке вставили "Bearer ey...", отрежем префикс
        if jwt.lower().startswith("bearer "):
            jwt = jwt.split(" ", 1)[1].strip()
        if not jwt:
            raise ValueError("PINATA_JWT is empty")

        self._headers = {"Authorization": f"Bearer {jwt}"}
        self._client = httpx.AsyncClient(timeout=timeout, headers=self._headers, http2=True, base_url=PINATA_BASE)

    async def close(self):
        await self._client.aclose()

    async def test_auth(self) -> bool:
        # Удобная проверка авторизации перед пином
        r = await self._client.get("/data/testAuthentication")
        return r.status_code == 200

    async def pin_json(self, payload: Dict[str, Any]) -> str:
        r = await self._client.post("/pinning/pinJSONToIPFS", json={"pinataContent": payload})
        r.raise_for_status()
        data = r.json()
        ipfs_hash = data.get("IpfsHash")
        if not ipfs_hash:
            raise RuntimeError("Pinata response has no IpfsHash")
        return ipfs_hash

    def gateway_url(self, cid: str) -> str:
        base = str(settings.PINATA_GATEWAY).rstrip("/")
        return f"{base}/{cid}"

    async def pin_nft_metadata(
        self, name: str, description: str, image_url: str, attributes: Dict[str, Any],
    ) -> Tuple[str, str]:
        metadata = {
            "name": name,
            "description": description,
            "image": image_url,
            "attributes": [{"trait_type": k, "value": v} for k, v in attributes.items()],
        }
        cid = await self.pin_json(metadata)
        return cid, self.gateway_url(cid)
