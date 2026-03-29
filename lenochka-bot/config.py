"""
Lenochka Telegram Bot — Config
"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Telegram
    bot_token: str = ""
    owner_id: int = 0

    # Database (absolute path resolved at runtime)
    db_path: str = ""

    # LLM (OpenAI-compatible)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "mimo-v2-pro"

    # Pipeline
    pipeline_batch_size: int = 10
    pipeline_batch_interval: float = 3.0

    # Throttling
    rate_limit_messages: int = 30

    # Digest (GMT+8)
    digest_hour: int = 8
    digest_minute: int = 0
    weekly_day: int = 6  # Sunday

    # Webhook (optional)
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_port: int = 8443

    class Config:
        env_prefix = "LEN_"
        env_file = ".env"


settings = Settings()

# Resolve db_path
if not settings.db_path:
    settings.db_path = str(
        Path(__file__).resolve().parent.parent / "lenochka-memory" / "db" / "lenochka.db"
    )
