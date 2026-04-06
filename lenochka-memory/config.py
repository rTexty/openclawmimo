"""
Lenochka — централизованная конфигурация.

Загружает переменные из .env (PROJECT_ROOT/.env).
Все скрипты импортируют константы отсюда — не хардкодят сами.
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv не установлен — используем системные env vars


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"[config] Обязательная переменная не задана: {key}. Проверь .env")
    return val


# ── Telegram ──────────────────────────────────────────────────────────────────
OWNER_ID: str = _require("OWNER_ID")
BOT_USERNAME: str = _require("BOT_USERNAME")

# ── Timezone ──────────────────────────────────────────────────────────────────
_tz_offset = int(os.getenv("OWNER_TZ_OFFSET", "8"))
TZ_OWNER = timezone(timedelta(hours=_tz_offset))

# ── Пути ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "lenochka-memory"

DB_PATH = Path(os.getenv("DB_PATH", str(MEMORY_DIR / "db" / "lenochka.db")))
LOAD_DIR_BASE = Path(os.getenv("LOAD_DIR_BASE", "/tmp/lenochka_load"))
