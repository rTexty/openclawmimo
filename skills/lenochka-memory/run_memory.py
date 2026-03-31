#!/usr/bin/env python3
"""
Lenochka Memory — OpenClaw Skill Adapter
Полная обёртка над mem.py и brain.py для использования из OpenClaw.

Команды:
  init              — создать/мигрировать БД
  classify          — классификация сообщения
  extract           — извлечение сущностей
  store             — запись в память (memories + vec)
  recall            — поиск по памяти (vector + FTS + keyword → RRF)
  ingest            — полный пайплайн (classify → extract → store → chaos)
  chaos-store       — запись в CHAOS
  chaos-search      — поиск в CHAOS
  context           — контекст-пакет для LLM
  digest            — утренний дайджест
  weekly            — недельный отчёт
  consolidate       — ночная консолидация
  stats             — статистика БД
"""

import argparse
import sys
import json
from pathlib import Path

# Import from lenochka-memory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lenochka-memory"))

try:
    import mem
    import brain
except ImportError as e:
    print(json.dumps({"status": "error", "error": f"Import error: {e}"}))
    sys.exit(1)


def output(data):
    """Единообразный JSON-вывод."""
    if isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_init(args):
    mem.init()
    output({"status": "success", "message": "Database initialized"})


def cmd_classify(args):
    label, conf, reason = brain.classify_message(args.text)
    output({"label": label, "confidence": round(conf, 3), "reasoning": reason})


def cmd_extract(args):
    entities = brain.extract_entities(args.text, label=args.label)
    output(entities)


def cmd_store(args):
    try:
        mid = mem.store(
            content=args.text,
            mem_type=args.type or "episodic",
            importance=float(args.importance or 0.6),
            contact_id=args.contact_id,
            chat_thread_id=args.chat_thread_id,
            deal_id=args.deal_id,
            source_message_id=args.message_id,
            content_hash=args.content_hash,
            auto_associate=not args.no_associate,
        )
        output({"status": "success", "memory_id": mid})
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_recall(args):
    try:
        results = mem.recall(
            query=args.query,
            strategy=args.strategy or "hybrid",
            contact_id=args.contact_id,
            deal_id=args.deal_id,
            mem_type=args.mem_type,
            limit=int(args.limit or 10),
        )
        output(results)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_ingest(args):
    try:
        result = mem.ingest(
            text=args.text,
            contact_id=args.contact_id,
            chat_thread_id=args.chat_thread_id,
            source_message_id=args.message_id,
        )
        output(result)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_chaos_store(args):
    try:
        eid = mem.chaos_store(
            content=args.text,
            category=args.category or "other",
            priority=float(args.priority or 0.5),
            memory_id=args.memory_id,
            contact_id=args.contact_id,
        )
        output({"status": "success", "chaos_id": eid})
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_chaos_search(args):
    try:
        results = mem.chaos_search(
            query=args.query,
            mode=args.mode or "index",
            limit=int(args.limit or 10),
        )
        output(results)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_context(args):
    try:
        packet = brain.build_context_packet(
            query=args.query,
            contact_id=args.contact_id,
            deal_id=args.deal_id,
            intent=args.intent or "search",
            limit=int(args.limit or 15),
        )
        output(packet)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_digest(args):
    try:
        result = brain.generate_daily_digest(date=args.date)
        output(result)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_weekly(args):
    try:
        result = brain.generate_weekly_digest()
        output(result)
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_consolidate(args):
    try:
        mem.consolidate()
        output({"status": "success", "message": "Consolidation completed"})
    except Exception as e:
        output({"status": "error", "error": str(e)})


def cmd_stats(args):
    mem.stats()


def main():
    parser = argparse.ArgumentParser(description="Lenochka Memory — OpenClaw Skill")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialize/migrate database")

    # classify
    p = sub.add_parser("classify", help="Classify a message")
    p.add_argument("--text", required=True)

    # extract
    p = sub.add_parser("extract", help="Extract entities from text")
    p.add_argument("--text", required=True)
    p.add_argument("--label", default=None)

    # store
    p = sub.add_parser("store", help="Store a memory")
    p.add_argument("--text", required=True)
    p.add_argument("--type", default="episodic")
    p.add_argument("--importance", default=0.6, type=float)
    p.add_argument("--contact_id", type=int, default=None)
    p.add_argument("--chat_thread_id", type=int, default=None)
    p.add_argument("--deal_id", type=int, default=None)
    p.add_argument("--message_id", type=int, default=None)
    p.add_argument("--content_hash", default=None)
    p.add_argument("--no_associate", action="store_true")

    # recall
    p = sub.add_parser("recall", help="Search memory")
    p.add_argument("--query", required=True)
    p.add_argument("--strategy", default="hybrid", choices=["hybrid", "vector", "bm25", "keyword"])
    p.add_argument("--contact_id", type=int, default=None)
    p.add_argument("--deal_id", type=int, default=None)
    p.add_argument("--mem_type", default=None)
    p.add_argument("--limit", type=int, default=10)

    # ingest
    p = sub.add_parser("ingest", help="Full pipeline: classify → extract → store → chaos")
    p.add_argument("--text", required=True)
    p.add_argument("--contact_id", type=int, default=None)
    p.add_argument("--chat_thread_id", type=int, default=None)
    p.add_argument("--message_id", type=int, default=None)

    # chaos-store
    p = sub.add_parser("chaos-store", help="Store in CHAOS")
    p.add_argument("--text", required=True)
    p.add_argument("--category", default="other")
    p.add_argument("--priority", default=0.5, type=float)
    p.add_argument("--memory_id", type=int, default=None)
    p.add_argument("--contact_id", type=int, default=None)

    # chaos-search
    p = sub.add_parser("chaos-search", help="Search CHAOS")
    p.add_argument("--query", required=True)
    p.add_argument("--mode", default="index", choices=["index", "full"])
    p.add_argument("--limit", type=int, default=10)

    # context
    p = sub.add_parser("context", help="Build context packet for LLM")
    p.add_argument("--query", required=True)
    p.add_argument("--contact_id", type=int, default=None)
    p.add_argument("--deal_id", type=int, default=None)
    p.add_argument("--intent", default="search", choices=["search", "recall", "core"])
    p.add_argument("--limit", type=int, default=15)

    # digest
    p = sub.add_parser("digest", help="Daily digest")
    p.add_argument("--date", default=None)

    # weekly
    sub.add_parser("weekly", help="Weekly report")

    # consolidate
    sub.add_parser("consolidate", help="Nightly consolidation")

    # stats
    sub.add_parser("stats", help="Database statistics")

    args = parser.parse_args()
    {
        "init": cmd_init,
        "classify": cmd_classify,
        "extract": cmd_extract,
        "store": cmd_store,
        "recall": cmd_recall,
        "ingest": cmd_ingest,
        "chaos-store": cmd_chaos_store,
        "chaos-search": cmd_chaos_search,
        "context": cmd_context,
        "digest": cmd_digest,
        "weekly": cmd_weekly,
        "consolidate": cmd_consolidate,
        "stats": cmd_stats,
    }[args.command](args)


if __name__ == "__main__":
    main()
