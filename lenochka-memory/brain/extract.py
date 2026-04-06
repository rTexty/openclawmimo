"""Извлечение сущностей из Telegram-сообщений."""

import re
from datetime import datetime

from brain.llm import _call_llm, _extract_json

EXTRACT_SYSTEM = """Ты — извлекатель сущностей из Telegram-сообщений для CRM.
Извлекай только то, что ЯВНО указано или сильно подразумевается.

Возвращай ТОЛЬКО JSON без форматирования:
{
  "contact": {"name": "...", "tg_username": "...", "company": "..."},
  "lead": {"source": "...", "amount": 0, "probability": 0.0-1.0},
  "deal": {"stage": "...", "amount": 0},
  "task": {"description": "...", "due_date": "YYYY-MM-DD или null", "priority": "low|normal|high|urgent"},
  "agreement": {"summary": "...", "amount": 0, "due_date": "YYYY-MM-DD или null"},
  "amounts": [0],
  "dates": ["YYYY-MM-DD"],
  "products": ["..."],
  "risk_type": "..." или null
}

Если сущность не найдена, ставь null. Не выдумывай данные."""


def extract_entities(text, label=None, chat_context=None):
    """Извлечь сущности из текста сообщения."""
    context_str = f"\nКлассификация: {label}" if label else ""
    context_str += f"\nКонтекст чата: {chat_context}" if chat_context else ""

    result = _call_llm(EXTRACT_SYSTEM, f"Сообщение:\n{text}{context_str}")

    if result:
        try:
            data = _extract_json(result)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return _extract_heuristic(text)


def _extract_heuristic(text):
    """Простое извлечение без LLM."""
    entities = {
        "contact": None,
        "lead": None,
        "deal": None,
        "task": None,
        "agreement": None,
        "amounts": [],
        "dates": [],
        "products": [],
        "risk_type": None,
    }

    # Суммы: "150000 рублей", "150 000 руб", "150к", "150 тыс"
    amounts_raw = re.findall(
        r"(\d[\d\s,.]*)\s*(тыс|к)?\s*(?:руб|₽|рублей)?", text, re.IGNORECASE
    )
    for num_str, multiplier, *_ in amounts_raw:
        if not num_str.strip():
            continue
        cleaned = num_str.replace(" ", "").replace(",", ".")
        try:
            val = float(cleaned)
            if multiplier and multiplier.lower() in ("тыс", "к"):
                val *= 1000
            if val > 0 and val not in entities["amounts"]:
                entities["amounts"].append(val)
        except ValueError:
            pass

    # Даты
    dates = re.findall(r"(\d{1,2}[./]\d{1,2}[./]?\d{0,4})", text)
    for d in dates:
        parts = re.split(r"[./]", d)
        if len(parts) >= 2:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) > 2 and parts[2] else datetime.now().year
            if 1 <= day <= 31 and 1 <= month <= 12:
                entities["dates"].append(f"{year}-{month:02d}-{day:02d}")

    # @username
    usernames = re.findall(r"@(\w+)", text)
    if usernames:
        entities["contact"] = {
            "name": None,
            "tg_username": usernames[0],
            "company": None,
        }

    return entities
