import json
import sys

from mem._db import get_db
from mem.store import store, _content_hash


def ingest(text, contact_id=None, chat_thread_id=None, source_message_id=None):
    """Полный пайплайн: classify → extract → store."""
    try:
        from brain import classify_message, extract_entities
    except ImportError:
        print("Модуль brain.py не найден")
        return None

    # 0. Дедупликация по content hash
    content_hash = _content_hash(text)
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM memories WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing:
        conn.close()
        print(f"⏭️ Дубликат (hash={content_hash}), пропускаю")
        return {"label": "duplicate", "skipped": True, "existing_id": existing["id"]}

    # Также проверяем source_message_id если передан
    if source_message_id:
        existing_msg = conn.execute(
            "SELECT id FROM memories WHERE source_message_id = ? AND chat_thread_id = ?",
            (source_message_id, chat_thread_id),
        ).fetchone()
        if existing_msg:
            conn.close()
            print(f"⏭️ Дубликат (source_message_id={source_message_id}), пропускаю")
            return {
                "label": "duplicate",
                "skipped": True,
                "existing_id": existing_msg["id"],
            }
    conn.close()

    # 1. Классификация
    label, conf, reason = classify_message(text)
    print(f"📊 Классификация: {label} (conf={conf:.2f}) — {reason}")

    # 2. Извлечение сущностей
    entities = extract_entities(text, label)
    print(f"🔍 Сущности: {json.dumps(entities, ensure_ascii=False)}")

    # 3. Запись в память (для важных типов)
    result = {"label": label, "confidence": conf, "entities": entities, "stored": False}

    if label in ("task", "decision", "lead-signal", "risk"):
        importance = 0.8 if label in ("decision", "risk") else 0.6
        mid = store(
            content=f"[{label}] {text[:200]}",
            mem_type="episodic",
            importance=importance,
            category=label,
            contact_id=contact_id,
            chat_thread_id=chat_thread_id,
            source_message_id=source_message_id,
            content_hash=content_hash,
            auto_associate=True,
        )
        result["stored"] = True
        result["memory_id"] = mid
        print(f"✅ Обработано и записано в память")
    else:
        print(f"⏭️ Тип '{label}' — пропускаю запись в память")

    return result


def context(query, contact_id=None, deal_id=None, intent="search"):
    """Собрать контекст-пакет для LLM."""
    try:
        from brain import build_context_packet

        packet = build_context_packet(
            query, contact_id=contact_id, deal_id=deal_id, intent=intent
        )
        return packet
    except ImportError:
        print("Модуль brain.py не найден")
        return None


def digest(date=None):
    """Утренний дайджест."""
    try:
        from brain import generate_daily_digest

        return generate_daily_digest(date)
    except ImportError:
        print("Модуль brain.py не найден")
        return None


def weekly():
    """Недельный дайджест."""
    try:
        from brain import generate_weekly_digest

        return generate_weekly_digest()
    except ImportError:
        print("Модуль brain.py не найден")
        return None
