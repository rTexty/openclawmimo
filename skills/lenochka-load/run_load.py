#!/usr/bin/env python3
"""
Lenochka Load — Оркестратор импорта истории чатов.

Парсит файлы Telegram экспорта (JSON/HTML), создаёт контакты и chat_threads,
импортирует сообщения батчами в БД.

Использование:
  python3 run_load.py --dir /tmp/lenochka_load/chat_1 --chat_id "chat_1"
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lenochka-memory"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "lenochka-load"))

try:
    import mem
except ImportError as e:
    print(json.dumps({"status": "error", "error": f"Import error: {e}"}))
    sys.exit(1)


def parse_file(file_path: Path) -> list[dict]:
    """Парсить файл через существующий парсер."""
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        from parsers.json_parser import parse_file

        return parse_file(str(file_path))
    elif suffix == ".html":
        from parsers.html_parser import parse_file

        return parse_file(str(file_path))
    else:
        return []


def resolve_contact(conn, name: str) -> int:
    """Найти или создать контакт по имени."""
    existing = conn.execute("SELECT id FROM contacts WHERE name=?", (name,)).fetchone()
    if existing:
        return existing["id"]
    conn.execute("INSERT INTO contacts (name) VALUES (?)", (name,))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def resolve_chat_thread(conn, chat_id: str, contact_id: int) -> int:
    """Найти или создать chat_thread."""
    thread_key = f"imported_{chat_id}_{contact_id}"
    existing = conn.execute(
        "SELECT id FROM chat_threads WHERE tg_chat_id=?", (thread_key,)
    ).fetchone()
    if existing:
        return existing["id"]
    conn.execute(
        "INSERT INTO chat_threads (tg_chat_id, contact_id, type, title) VALUES (?, ?, 'personal', ?)",
        (thread_key, contact_id, f"Imported chat {chat_id}"),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def run_load(dir_path: str, chat_id: str) -> dict:
    """Главная функция импорта."""
    directory = Path(dir_path)
    if not directory.exists():
        return {"status": "error", "error": f"Directory not found: {dir_path}"}

    files = list(directory.glob("*.json")) + list(directory.glob("*.html"))
    if not files:
        return {"status": "error", "error": "No JSON or HTML files found"}

    all_messages: list[dict] = []
    errors: list[str] = []
    files_parsed = 0

    for f in sorted(files):
        try:
            msgs = parse_file(f)
            if msgs:
                all_messages.extend(msgs)
                files_parsed += 1
            else:
                errors.append(f"{f.name}: no messages parsed")
        except Exception as e:
            errors.append(f"{f.name}: {e}")

    if not all_messages:
        return {
            "status": "error",
            "error": "No messages found in any file",
            "errors": errors,
        }

    by_sender: dict[str, list[dict]] = {}
    for msg in all_messages:
        name = msg.get("from_name", "Unknown")
        by_sender.setdefault(name, []).append(msg)

    for sender in by_sender:
        by_sender[sender].sort(key=lambda m: m.get("date", ""))

    conn = mem.get_db()
    total_messages = 0
    total_memories = 0
    contacts_created = 0

    try:
        for sender_name, messages in by_sender.items():
            contact_id = resolve_contact(conn, sender_name)
            thread_id = resolve_chat_thread(conn, chat_id, contact_id)

            for i in range(0, len(messages), 100):
                batch = messages[i : i + 100]
                result = mem.import_batch(batch, contact_id, thread_id)
                total_messages += result["messages_inserted"]
                total_memories += result["memories_created"]

            contacts_created += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e), "errors": errors}
    finally:
        conn.close()

    return {
        "status": "ok",
        "files_parsed": files_parsed,
        "total_messages": total_messages,
        "contacts_created": contacts_created,
        "memories_created": total_memories,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Lenochka Load — импорт истории чатов")
    parser.add_argument("--dir", required=True, help="Директория с файлами")
    parser.add_argument("--chat_id", required=True, help="Идентификатор чата")
    args = parser.parse_args()

    result = run_load(args.dir, args.chat_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
