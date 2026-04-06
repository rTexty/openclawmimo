import json
import sys

from mem._config import DB_PATH
from mem._db import get_db
from mem.migrations import init, _migrate_db
from mem.store import store, _content_hash
from mem.search import recall, recall_assoc
from mem.crm import (
    crm_contact,
    crm_deals,
    crm_overdue_tasks,
    crm_abandoned,
    crm_leads,
    crm_daily_summary,
)
from mem.pipeline import ingest, context, digest, weekly
from mem.maintenance import consolidate, prune_messages, stats
from mem.import_batch import import_batch

_DOC = """
Lenochka Memory CLI v2 — Единый инструмент памяти
Объединяет CRM-БД + Agent Memory (vector) + CHAOS в одном CLI.

Использование:
    python3 mem.py init                          — создать БД
    python3 mem.py store "текст" [--type ...]    — записать memory
    python3 mem.py recall "запрос" [--strategy]  — поиск по памяти
    python3 mem.py recall-assoc --from-id N      — связанные memories
    python3 mem.py crm <subcommand>              — CRM-запросы
    python3 mem.py ingest "текст" [--contact-id] — полный пайплайн
    python3 mem.py context "запрос"              — контекст-пакет для LLM
    python3 mem.py digest                        — дайджест
    python3 mem.py weekly                        — недельный дайджест
    python3 mem.py consolidate                   — консолидация
    python3 mem.py prune-messages --older-than N — архивация
    python3 mem.py stats                         — статистика
"""


def main():
    if len(sys.argv) < 2:
        print(_DOC)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    def get_arg(name, default=None):
        for i, a in enumerate(args):
            if a == f"--{name}" and i + 1 < len(args):
                return args[i + 1]
        return default

    if cmd == "init":
        init()

    elif cmd == "migrate":
        if not DB_PATH.exists():
            print("БД не найдена. Сначала: python3 mem.py init")
            sys.exit(1)
        conn = get_db()
        _migrate_db(conn)
        conn.close()

    elif cmd == "store":
        if not args:
            print(
                'Usage: mem.py store "content" [--type episodic] [--importance 0.7] ...'
            )
            sys.exit(1)
        content = args[0]
        store(
            content=content,
            mem_type=get_arg("type", "episodic"),
            importance=float(get_arg("importance", "0.5")),
            contact_id=int(get_arg("contact-id")) if get_arg("contact-id") else None,
            chat_thread_id=int(get_arg("chat-thread-id"))
            if get_arg("chat-thread-id")
            else None,
            deal_id=int(get_arg("deal-id")) if get_arg("deal-id") else None,
            source_message_id=int(get_arg("source-message-id"))
            if get_arg("source-message-id")
            else None,
            tags=json.loads(get_arg("tags")) if get_arg("tags") else None,
        )

    elif cmd == "recall":
        if not args:
            print(
                'Usage: mem.py recall "query" [--strategy hybrid] [--contact-id N] ...'
            )
            sys.exit(1)
        query = args[0]
        results = recall(
            query=query,
            strategy=get_arg("strategy", "hybrid"),
            contact_id=int(get_arg("contact-id")) if get_arg("contact-id") else None,
            deal_id=int(get_arg("deal-id")) if get_arg("deal-id") else None,
            mem_type=get_arg("mem-type"),
            limit=int(get_arg("limit", "20")),
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif cmd == "recall-assoc":
        memory_id = int(get_arg("from-memory-id", "0"))
        results = recall_assoc(
            memory_id=memory_id,
            hops=int(get_arg("hops", "1")),
            limit=int(get_arg("limit", "10")),
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif cmd == "crm":
        if len(args) < 1:
            print(
                "Usage: mem.py crm <contact|deals|overdue-tasks|abandoned|leads|daily-summary> [options]"
            )
            sys.exit(1)
        sub = args[0]
        sub_args = args[1:]

        def sub_arg(name, default=None):
            for i, a in enumerate(sub_args):
                if a == f"--{name}" and i + 1 < len(sub_args):
                    return sub_args[i + 1]
            return default

        if sub == "contact":
            result = crm_contact(
                tg=sub_arg("tg"),
                contact_id=int(sub_arg("contact-id"))
                if sub_arg("contact-id")
                else None,
            )
            print(
                json.dumps(result, ensure_ascii=False, indent=2)
                if result
                else "Не найден"
            )
        elif sub == "deals":
            cid = int(sub_arg("contact-id")) if sub_arg("contact-id") else None
            print(json.dumps(crm_deals(contact_id=cid), ensure_ascii=False, indent=2))
        elif sub == "overdue-tasks":
            print(json.dumps(crm_overdue_tasks(), ensure_ascii=False, indent=2))
        elif sub == "abandoned":
            print(
                json.dumps(
                    crm_abandoned(hours=int(sub_arg("hours", "24"))),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif sub == "leads":
            print(
                json.dumps(
                    crm_leads(since=sub_arg("since")), ensure_ascii=False, indent=2
                )
            )
        elif sub == "daily-summary":
            print(
                json.dumps(
                    crm_daily_summary(date=sub_arg("date")),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"Неизвестная CRM-команда: {sub}")
            sys.exit(1)

    elif cmd == "ingest":
        if not args:
            print(
                'Usage: mem.py ingest "текст сообщения" [--contact-id N] [--chat-thread-id N] [--source-message-id N]'
            )
            sys.exit(1)
        text = " ".join(args)
        contact_id = int(get_arg("contact-id")) if get_arg("contact-id") else None
        chat_thread_id = (
            int(get_arg("chat-thread-id")) if get_arg("chat-thread-id") else None
        )
        source_message_id = get_arg("source-message-id")
        result = ingest(
            text,
            contact_id=contact_id,
            chat_thread_id=chat_thread_id,
            source_message_id=source_message_id,
        )
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "context":
        query = " ".join(args) if args else "общий"
        contact_id = int(get_arg("contact-id")) if get_arg("contact-id") else None
        deal_id = int(get_arg("deal-id")) if get_arg("deal-id") else None
        intent = get_arg("intent", "search")
        result = context(query, contact_id=contact_id, deal_id=deal_id, intent=intent)
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "digest":
        date = get_arg("date")
        result = digest(date=date)
        if result:
            print(result)

    elif cmd == "weekly":
        result = weekly()
        if result:
            print(result)

    elif cmd == "classify":
        if not args:
            print('Usage: mem.py classify "текст сообщения"')
            sys.exit(1)
        text = " ".join(args)
        try:
            from brain import classify_message

            label, conf, reason = classify_message(text)
            print(
                json.dumps(
                    {"label": label, "confidence": conf, "reasoning": reason},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except ImportError:
            print("Модуль brain.py не найден")

    elif cmd == "extract":
        if not args:
            print('Usage: mem.py extract "текст сообщения"')
            sys.exit(1)
        text = " ".join(args)
        try:
            from brain import extract_entities

            entities = extract_entities(text)
            print(json.dumps(entities, ensure_ascii=False, indent=2))
        except ImportError:
            print("Модуль brain.py не найден")

    elif cmd == "consolidate":
        consolidate()

    elif cmd == "prune-messages":
        days = int(get_arg("older-than", "180"))
        prune_messages(older_than_days=days)

    elif cmd == "import-batch":
        if not args:
            print(
                "Usage: mem.py import-batch --file messages.json --contact-id N --chat-thread-id N"
            )
            sys.exit(1)
        file_path = get_arg("file")
        contact_id = int(get_arg("contact-id")) if get_arg("contact-id") else None
        chat_thread_id = (
            int(get_arg("chat-thread-id")) if get_arg("chat-thread-id") else None
        )
        if not file_path:
            print("Error: --file is required")
            sys.exit(1)
        with open(file_path, "r", encoding="utf-8") as f:
            messages = json.load(f)
        result = import_batch(messages, contact_id, chat_thread_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "stats":
        stats()

    elif cmd == "raptor":
        level = int(get_arg("level", "0"))
        try:
            from brain import build_raptor

            count = build_raptor(level=level)
            print(f"✅ Создано {count} RAPTOR-нод на уровне {level}")
        except ImportError:
            print("Модуль brain.py не найден")

    else:
        print(f"Неизвестная команда: {cmd}")
        print(_DOC)
        sys.exit(1)
