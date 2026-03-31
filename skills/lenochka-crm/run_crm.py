#!/usr/bin/env python3
"""
Lenochka CRM — OpenClaw Skill Adapter
Полная обёртка над CRM-таблицами для использования из OpenClaw.

Команды:
  contact   — создать/найти/обновить контакт
  deal      — создать/обновить/закрыть сделку
  task      — создать/обновить задачу
  lead      — создать/обновить лид
  agreement — создать/обновить договор
  query     — запросы к CRM (active-deals, open-tasks, overdue, abandoned, etc.)
"""

import argparse
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lenochka-memory"))

try:
    import mem
except ImportError as e:
    print(json.dumps({"status": "error", "error": f"Import error: {e}"}))
    sys.exit(1)


def get_db():
    conn = sqlite3.connect(str(mem.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def output(data):
    print(json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data)


# ─── CONTACT ───

def cmd_contact(args):
    conn = get_db()
    try:
        if args.find:
            if args.id:
                row = conn.execute("SELECT * FROM contacts WHERE id=?", (args.id,)).fetchone()
            elif args.tg_user_id:
                row = conn.execute("SELECT * FROM contacts WHERE tg_user_id=?", (str(args.tg_user_id),)).fetchone()
            elif args.tg_username:
                row = conn.execute("SELECT * FROM contacts WHERE tg_username=?", (args.tg_username.lstrip("@"),)).fetchone()
            else:
                output({"status": "error", "error": "Specify --id, --tg_user_id, or --tg_username"})
                return
            output(dict(row) if row else {"status": "not_found"})

        elif args.create:
            conn.execute(
                "INSERT INTO contacts (name, tg_username, tg_user_id, company_id, notes) VALUES (?, ?, ?, ?, ?)",
                (args.name or "Unknown", args.tg_username, str(args.tg_user_id) if args.tg_user_id else None,
                 None, args.notes)
            )
            conn.commit()
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            output({"status": "success", "contact_id": cid})

        elif args.update:
            sets, params = [], []
            if args.name: sets.append("name=?"); params.append(args.name)
            if args.tg_username: sets.append("tg_username=?"); params.append(args.tg_username)
            if args.company: sets.append("company_id=(SELECT id FROM companies WHERE name=?)"); params.append(args.company)
            if args.phones: sets.append("phones=?"); params.append(args.phones)
            if args.notes: sets.append("notes=?"); params.append(args.notes)
            sets.append("updated_at=datetime('now')")
            params.append(args.id)
            conn.execute(f"UPDATE contacts SET {','.join(sets)} WHERE id=?", params)
            conn.commit()
            output({"status": "success", "contact_id": args.id})

        elif args.upsert:
            # Find by tg_user_id first, then tg_username
            existing = None
            if args.tg_user_id:
                existing = conn.execute("SELECT id, name FROM contacts WHERE tg_user_id=?",
                                        (str(args.tg_user_id),)).fetchone()
            if not existing and args.tg_username:
                existing = conn.execute("SELECT id, name FROM contacts WHERE tg_username=?",
                                        (args.tg_username.lstrip("@"),)).fetchone()

            if existing:
                # Update name if provided and different
                if args.name and args.name != existing["name"]:
                    conn.execute("UPDATE contacts SET name=?, updated_at=datetime('now') WHERE id=?",
                                 (args.name, existing["id"]))
                    conn.commit()
                output({"status": "exists", "contact_id": existing["id"]})
            else:
                conn.execute(
                    "INSERT INTO contacts (name, tg_username, tg_user_id) VALUES (?, ?, ?)",
                    (args.name or "Unknown", args.tg_username, str(args.tg_user_id) if args.tg_user_id else None)
                )
                conn.commit()
                cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                output({"status": "created", "contact_id": cid})
    except Exception as e:
        conn.rollback()
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


# ─── DEAL ───

def cmd_deal(args):
    conn = get_db()
    try:
        if args.find:
            if args.contact_id:
                rows = conn.execute("""
                    SELECT d.*, c.name as contact_name FROM deals d
                    JOIN contacts c ON d.contact_id = c.id
                    WHERE d.contact_id = ? AND d.stage NOT IN ('closed_won','closed_lost')
                    ORDER BY d.created_at DESC
                """, (args.contact_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT d.*, c.name as contact_name FROM deals d
                    JOIN contacts c ON d.contact_id = c.id
                    WHERE d.stage NOT IN ('closed_won','closed_lost')
                    ORDER BY d.expected_close_at ASC
                """).fetchall()
            output([dict(r) for r in rows])

        elif args.create:
            conn.execute(
                "INSERT INTO deals (contact_id, amount, stage, notes) VALUES (?, ?, ?, ?)",
                (args.contact_id, args.amount or 0, args.stage or "discovery", args.notes or "")
            )
            conn.commit()
            output({"status": "success", "message": "Deal created"})

        elif args.update:
            # Upsert: find active deal or create
            existing = conn.execute("""
                SELECT id, amount FROM deals
                WHERE contact_id = ? AND stage NOT IN ('closed_won','closed_lost')
                ORDER BY created_at DESC LIMIT 1
            """, (args.contact_id,)).fetchone()

            if existing:
                if args.amount and args.amount > (existing["amount"] or 0):
                    conn.execute("UPDATE deals SET amount=?, updated_at=datetime('now') WHERE id=?",
                                 (args.amount, existing["id"]))
                if args.stage:
                    conn.execute("UPDATE deals SET stage=?, updated_at=datetime('now') WHERE id=?",
                                 (args.stage, existing["id"]))
                conn.commit()
                output({"status": "updated", "deal_id": existing["id"]})
            else:
                conn.execute(
                    "INSERT INTO deals (contact_id, amount, stage) VALUES (?, ?, ?)",
                    (args.contact_id, args.amount or 0, args.stage or "discovery")
                )
                conn.commit()
                output({"status": "created", "message": "New deal created"})

        elif args.close:
            conn.execute("""
                UPDATE deals SET stage=?, updated_at=datetime('now')
                WHERE contact_id = ? AND stage NOT IN ('closed_won','closed_lost')
            """, (args.stage, args.contact_id))
            conn.commit()
            output({"status": "success", "message": f"Deal closed as {args.stage}"})

    except Exception as e:
        conn.rollback()
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


# ─── TASK ───

def cmd_task(args):
    conn = get_db()
    try:
        if args.find:
            sql = """
                SELECT t.*, c.name as contact_name FROM tasks t
                LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
                WHERE 1=1
            """
            params = []
            if args.status:
                sql += " AND t.status = ?"; params.append(args.status)
            if args.contact_id:
                sql += " AND t.related_type = 'contact' AND t.related_id = ?"; params.append(args.contact_id)
            sql += " ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, t.due_at ASC"
            if args.limit:
                sql += " LIMIT ?"; params.append(int(args.limit))
            rows = conn.execute(sql, params).fetchall()
            output([dict(r) for r in rows])

        elif args.create:
            conn.execute("""
                INSERT INTO tasks (description, related_type, related_id, due_at, priority, source_message_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                args.description,
                "contact" if args.contact_id else "other",
                args.contact_id,
                args.due_date,
                args.priority or "normal",
                args.message_id,
            ))
            conn.commit()
            output({"status": "success", "message": f"Task created: {args.description}"})

        elif args.update:
            sets, params = [], []
            if args.status: sets.append("status=?"); params.append(args.status)
            if args.due_date: sets.append("due_at=?"); params.append(args.due_date)
            if args.priority: sets.append("priority=?"); params.append(args.priority)
            if args.description: sets.append("description=?"); params.append(args.description)
            sets.append("updated_at=datetime('now')")
            params.append(args.id)
            conn.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", params)
            conn.commit()
            output({"status": "success", "task_id": args.id})

    except Exception as e:
        conn.rollback()
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


# ─── LEAD ───

def cmd_lead(args):
    conn = get_db()
    try:
        if args.find:
            if args.active:
                rows = conn.execute("""
                    SELECT l.*, c.name as contact_name FROM leads l
                    JOIN contacts c ON l.contact_id = c.id
                    WHERE l.status NOT IN ('won','lost')
                    ORDER BY l.created_at DESC
                """).fetchall()
            elif args.contact_id:
                rows = conn.execute("""
                    SELECT l.*, c.name as contact_name FROM leads l
                    JOIN contacts c ON l.contact_id = c.id
                    WHERE l.contact_id = ?
                    ORDER BY l.created_at DESC
                """, (args.contact_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT l.*, c.name as contact_name FROM leads l
                    JOIN contacts c ON l.contact_id = c.id
                    ORDER BY l.created_at DESC LIMIT 20
                """).fetchall()
            output([dict(r) for r in rows])

        elif args.create:
            conn.execute("""
                INSERT INTO leads (contact_id, source, amount, probability, status)
                VALUES (?, ?, ?, ?, 'new')
            """, (args.contact_id, args.source or "telegram", args.amount, args.probability or 0.5))
            conn.commit()
            output({"status": "success", "message": "Lead created"})

        elif args.update:
            conn.execute("UPDATE leads SET status=?, updated_at=datetime('now') WHERE contact_id=? AND status NOT IN ('won','lost')",
                         (args.status, args.contact_id))
            conn.commit()
            output({"status": "success", "message": f"Lead status → {args.status}"})

    except Exception as e:
        conn.rollback()
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


# ─── AGREEMENT ───

def cmd_agreement(args):
    conn = get_db()
    try:
        if args.create:
            conn.execute("""
                INSERT INTO agreements (contact_id, deal_id, summary, amount, due_at)
                VALUES (?, ?, ?, ?, ?)
            """, (args.contact_id, args.deal_id, args.summary, args.amount, args.due_date))
            conn.commit()
            output({"status": "success", "message": "Agreement created"})

        elif args.update:
            sets, params = [], []
            if args.status: sets.append("status=?"); params.append(args.status)
            if args.due_date: sets.append("due_at=?"); params.append(args.due_date)
            sets.append("updated_at=datetime('now')")
            params.append(args.id)
            conn.execute(f"UPDATE agreements SET {','.join(sets)} WHERE id=?", params)
            conn.commit()
            output({"status": "success", "agreement_id": args.id})

    except Exception as e:
        conn.rollback()
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


# ─── QUERY ───

def cmd_query(args):
    conn = get_db()
    try:
        if args.active_deals:
            rows = conn.execute("SELECT * FROM v_active_deals").fetchall()
            output([dict(r) for r in rows])

        elif args.open_tasks:
            limit = int(args.limit or 20)
            rows = conn.execute("""
                SELECT t.*, c.name as contact_name FROM tasks t
                LEFT JOIN contacts c ON t.related_type='contact' AND t.related_id=c.id
                WHERE t.status NOT IN ('done','cancelled')
                ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                         t.due_at ASC
                LIMIT ?
            """, (limit,)).fetchall()
            output([dict(r) for r in rows])

        elif args.overdue_tasks:
            rows = conn.execute("SELECT * FROM v_overdue_tasks").fetchall()
            output([dict(r) for r in rows])

        elif args.abandoned:
            hours = int(args.hours or 48)
            rows = conn.execute("""
                SELECT ct.id, ct.title, c.name as contact_name, c.tg_username,
                       MAX(m.sent_at) as last_message_at,
                       (julianday('now') - julianday(MAX(m.sent_at))) * 24 as hours_since
                FROM chat_threads ct
                JOIN messages m ON m.chat_thread_id = ct.id
                LEFT JOIN contacts c ON ct.contact_id = c.id
                WHERE m.from_user_id != 'self'
                GROUP BY ct.id
                HAVING hours_since > ?
                ORDER BY hours_since DESC
            """, (hours,)).fetchall()
            output([dict(r) for r in rows])

        elif args.active_leads:
            rows = conn.execute("""
                SELECT l.*, c.name as contact_name FROM leads l
                JOIN contacts c ON l.contact_id = c.id
                WHERE l.status NOT IN ('won','lost')
                ORDER BY l.created_at DESC
            """).fetchall()
            output([dict(r) for r in rows])

        elif args.daily_summary:
            date = args.date or datetime.now().strftime("%Y-%m-%d")
            start = f"{date} 00:00:00"
            end = f"{date} 23:59:59"
            stats = {}
            for key, sql in [
                ("messages", "SELECT COUNT(*) as c FROM messages WHERE sent_at BETWEEN ? AND ?"),
                ("new_leads", "SELECT COUNT(*) as c FROM leads WHERE created_at BETWEEN ? AND ?"),
                ("new_tasks", "SELECT COUNT(*) as c FROM tasks WHERE created_at BETWEEN ? AND ?"),
                ("completed_tasks", "SELECT COUNT(*) as c FROM tasks WHERE status='done' AND updated_at BETWEEN ? AND ?"),
                ("memories", "SELECT COUNT(*) as c FROM memories WHERE created_at BETWEEN ? AND ?"),
            ]:
                stats[key] = conn.execute(sql, (start, end)).fetchone()["c"]
            output({"date": date, **stats})

        elif args.contact_full:
            cid = args.contact_id
            contact = dict(conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone() or {})
            deals = [dict(r) for r in conn.execute(
                "SELECT * FROM deals WHERE contact_id=? ORDER BY created_at DESC", (cid,)).fetchall()]
            tasks = [dict(r) for r in conn.execute(
                "SELECT * FROM tasks WHERE related_type='contact' AND related_id=? AND status NOT IN ('done','cancelled') ORDER BY due_at ASC", (cid,)).fetchall()]
            leads = [dict(r) for r in conn.execute(
                "SELECT * FROM leads WHERE contact_id=? ORDER BY created_at DESC LIMIT 5", (cid,)).fetchall()]
            output({"contact": contact, "deals": deals, "tasks": tasks, "leads": leads})

        elif args.upcoming:
            days = int(args.days or 3)
            results = []
            for sql, label in [
                (f"SELECT 'task' as type, t.id, t.description as title, t.due_at, t.priority, c.name as contact_name FROM tasks t LEFT JOIN contacts c ON t.related_type='contact' AND t.related_id=c.id WHERE t.due_at BETWEEN datetime('now') AND datetime('now','+{days} days') AND t.status NOT IN ('done','cancelled')", "tasks"),
                (f"SELECT 'agreement' as type, a.id, a.summary as title, a.due_at, a.status as priority, c.name as contact_name FROM agreements a JOIN contacts c ON a.contact_id=c.id WHERE a.due_at BETWEEN date('now') AND date('now','+{days} days') AND a.status NOT IN ('signed','completed','cancelled')", "agreements"),
                (f"SELECT 'invoice' as type, i.id, a.summary as title, i.due_at, i.status as priority, c.name as contact_name FROM invoices i JOIN agreements a ON i.agreement_id=a.id JOIN contacts c ON a.contact_id=c.id WHERE i.due_at BETWEEN date('now') AND date('now','+{days} days') AND i.status IN ('sent','overdue')", "invoices"),
            ]:
                try:
                    rows = conn.execute(sql).fetchall()
                    results.extend([dict(r) for r in rows])
                except Exception:
                    pass
            output(results)

    except Exception as e:
        output({"status": "error", "error": str(e)})
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Lenochka CRM — OpenClaw Skill")
    sub = parser.add_subparsers(dest="command", required=True)

    # contact
    p = sub.add_parser("contact")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", action="store_true")
    g.add_argument("--create", action="store_true")
    g.add_argument("--update", action="store_true")
    g.add_argument("--upsert", action="store_true")
    p.add_argument("--id", type=int)
    p.add_argument("--name")
    p.add_argument("--tg_username")
    p.add_argument("--tg_user_id")
    p.add_argument("--company")
    p.add_argument("--phones")
    p.add_argument("--notes")

    # deal
    p = sub.add_parser("deal")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", action="store_true")
    g.add_argument("--create", action="store_true")
    g.add_argument("--update", action="store_true")
    g.add_argument("--close", action="store_true")
    p.add_argument("--contact_id", type=int, required=True)
    p.add_argument("--amount", type=float)
    p.add_argument("--stage")
    p.add_argument("--notes")

    # task
    p = sub.add_parser("task")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", action="store_true")
    g.add_argument("--create", action="store_true")
    g.add_argument("--update", action="store_true")
    p.add_argument("--id", type=int)
    p.add_argument("--contact_id", type=int)
    p.add_argument("--description")
    p.add_argument("--due_date")
    p.add_argument("--priority")
    p.add_argument("--status")
    p.add_argument("--message_id", type=int)
    p.add_argument("--limit", type=int)

    # lead
    p = sub.add_parser("lead")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", action="store_true")
    g.add_argument("--create", action="store_true")
    g.add_argument("--update", action="store_true")
    p.add_argument("--contact_id", type=int)
    p.add_argument("--source")
    p.add_argument("--amount", type=float)
    p.add_argument("--probability", type=float)
    p.add_argument("--status")
    p.add_argument("--active", action="store_true")

    # agreement
    p = sub.add_parser("agreement")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--create", action="store_true")
    g.add_argument("--update", action="store_true")
    p.add_argument("--id", type=int)
    p.add_argument("--contact_id", type=int)
    p.add_argument("--deal_id", type=int)
    p.add_argument("--summary")
    p.add_argument("--amount", type=float)
    p.add_argument("--due_date")
    p.add_argument("--status")

    # query
    p = sub.add_parser("query")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--active-deals", action="store_true")
    g.add_argument("--open-tasks", action="store_true")
    g.add_argument("--overdue-tasks", action="store_true")
    g.add_argument("--abandoned", action="store_true")
    g.add_argument("--active-leads", action="store_true")
    g.add_argument("--daily-summary", action="store_true")
    g.add_argument("--contact-full", action="store_true")
    g.add_argument("--upcoming", action="store_true")
    p.add_argument("--contact_id", type=int)
    p.add_argument("--date")
    p.add_argument("--hours", type=int)
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int)

    args = parser.parse_args()
    {
        "contact": cmd_contact,
        "deal": cmd_deal,
        "task": cmd_task,
        "lead": cmd_lead,
        "agreement": cmd_agreement,
        "query": cmd_query,
    }[args.command](args)


if __name__ == "__main__":
    main()
