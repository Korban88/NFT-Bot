import os
from pydantic import BaseSettings, AnyHttpUrl, validator

class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_TELEGRAM_ID: int

    TONAPI_KEY: str
    TON_WALLET_ADDRESS: str  # наш TON-адрес для приёма платежей

    PINATA_JWT: str
    PINATA_GATEWAY: AnyHttpUrl = "https://gateway.pinata.cloud/ipfs/"

    class Config:
        env_file = ".env"
        case_sensitive = True

    @validator("TON_WALLET_ADDRESS")
    def check_wallet(cls, v):
        if not v or len(v) < 20:
            raise ValueError("TON_WALLET_ADDRESS looks invalid")
        return v

settings = Settings(
    BOT_TOKEN=os.getenv("BOT_TOKEN", ""),
    ADMIN_TELEGRAM_ID=int(os.getenv("ADMIN_TELEGRAM_ID", "0")),
    TONAPI_KEY=os.getenv("TONAPI_KEY", ""),
    TON_WALLET_ADDRESS=os.getenv("TON_WALLET_ADDRESS", ""),
    PINATA_JWT=os.getenv("PINATA_JWT", ""),
    PINATA_GATEWAY=os.getenv("PINATA_GATEWAY", "https://gateway.pinata.cloud/ipfs/"),
)
