#!/usr/bin/env python3
import argparse
import sys
import json
from pathlib import Path
import sqlite3

# Import DB Path from lenochka-memory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lenochka-memory"))

try:
    import mem
except ImportError as e:
    print(f"Error importing mem.py: {e}")
    sys.exit(1)


def get_db():
    conn = sqlite3.connect(str(mem.DB_PATH))
    conn.row_factory = sqlite3.Row
    # Enforce foreign keys and WAL
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _upsert_deal(conn: sqlite3.Connection, amount: float | None, contact_id: int, message_id: int):
    """Обновляет сделку на основе суммы и контакта."""
    if not contact_id:
        raise ValueError("Contact ID is required for deals")

    existing = conn.execute(
        """SELECT id, amount FROM deals
           WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
           ORDER BY created_at DESC LIMIT 1""",
        (contact_id,),
    ).fetchone()

    amount_val = amount or 0.0

    if existing:
        # Улучшение: если пришла новая сумма, обновляем ее, если она больше или явно согласована
        if amount_val > 0:
            conn.execute(
                "UPDATE deals SET amount = ?, updated_at = datetime('now') WHERE id = ?",
                (amount_val, existing["id"]),
            )
    else:
        conn.execute(
            "INSERT INTO deals (contact_id, amount, stage, notes) VALUES (?, ?, 'discovery', ?)",
            (contact_id, amount_val, f"created by OpenClaw msg#{message_id}"),
        )


def _create_task(conn: sqlite3.Connection, t: dict, contact_id: int | None, message_id: int):
    """Task entity → tasks table."""
    conn.execute(
        """INSERT INTO tasks (description, related_type, related_id, due_at,
                              priority, source_message_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            t.get("description", "Untitled task"),
            "contact" if contact_id else "other",
            contact_id,
            t.get("due_date"),
            t.get("priority", "normal"),
            message_id,
        ),
    )


def cmd_deal(args):
    """Команда OpenClaw для транзакций со сделками (Deals)."""
    conn = get_db()
    try:
        # 1. Update/Create deal amount
        if args.amount is not None:
            _upsert_deal(conn, args.amount, args.contact_id, args.message_id or 0)
            
        # 2. Update stage if explicitly provided and not default
        if args.stage and args.stage != "discovery":
            conn.execute("UPDATE deals SET stage = ?, updated_at = datetime('now') WHERE contact_id = ? AND stage != ?",
                         (args.stage, args.contact_id, args.stage))
        
        conn.commit()
        print(json.dumps({"status": "success", "message": f"Deal updated in CRM for contact_id={args.contact_id}"}))
    except Exception as e:
        conn.rollback()
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    finally:
        conn.close()


def cmd_task(args):
    """Команда OpenClaw для создания задач-будильников (Tasks)."""
    conn = get_db()
    try:
        t = {
            "description": args.description,
            "due_date": args.due_date,
            "priority": args.priority
        }
        _create_task(conn, t, args.contact_id, args.message_id or 0)
        conn.commit()
        print(json.dumps({"status": "success", "message": f"Task created: {args.description}"}))
    except Exception as e:
        conn.rollback()
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Lenochka CRM Manager Adapter for OpenClaw")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Deal command
    deal_p = subparsers.add_parser("deal", help="Update deal for a contact")
    deal_p.add_argument("--contact_id", type=int, required=True, help="CRM Contact ID")
    deal_p.add_argument("--amount", type=float, default=None, help="Deal amount (float)")
    deal_p.add_argument("--stage", type=str, default="discovery", help="Deal stage (discovery, closed_won, closed_lost)")
    deal_p.add_argument("--message_id", type=int, default=0, help="Source Message ID")

    # Task command
    task_p = subparsers.add_parser("task", help="Create a follow-up task")
    task_p.add_argument("--contact_id", type=int, required=True, help="CRM Contact ID")
    task_p.add_argument("--description", type=str, required=True, help="Task description")
    task_p.add_argument("--due_date", type=str, required=True, help="Due date (YYYY-MM-DD HH:MM:SS format)")
    task_p.add_argument("--priority", type=str, default="normal", help="Priority (normal, high, urgent)")
    task_p.add_argument("--message_id", type=int, default=0, help="Source Message ID")

    args = parser.parse_args()

    if args.command == "deal":
        cmd_deal(args)
    elif args.command == "task":
        cmd_task(args)

if __name__ == "__main__":
    main()
