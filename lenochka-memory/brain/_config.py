"""Конфигурация brain — не импортирует ни один модуль из mem/."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "lenochka.db"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

# LLM config — читаем LEN_LLM_* (единый префикс с config.py)
# Fallback на LENOCHKA_LLM_* для обратной совместимости
LLM_BASE_URL = os.environ.get(
    "LEN_LLM_BASE_URL", os.environ.get("LENOCHKA_LLM_BASE_URL", "")
)
LLM_API_KEY = os.environ.get(
    "LEN_LLM_API_KEY", os.environ.get("LENOCHKA_LLM_API_KEY", "")
)
LLM_MODEL = os.environ.get(
    "LEN_LLM_MODEL", os.environ.get("LENOCHKA_LLM_MODEL", "mimo-v2-pro")
)

GMT8 = timezone(timedelta(hours=8))


def _now_gmt8():
    """Текущее время в GMT+8 (Камиль)."""
    return datetime.now(GMT8)
