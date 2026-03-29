"""
CRM Upsert — превращает извлечённые сущности в CRM-записи.
Мост между ingest и CRM-таблицами.
"""
import sqlite3
import logging
from services.brain_wrapper import get_db

logger = logging.getLogger("lenochka.crm")


def crm_upsert(entities: dict, contact_id: int | None,
               chat_thread_id: int | None, message_id: int,
               db_path: str):
    """Записывает contacts, deals, leads, tasks, agreements из extracted entities."""
    if not entities:
        return

    conn = get_db(db_path)
    try:
        # CONTACT from extracted entity
        if entities.get("contact") and not contact_id:
            contact_id = _upsert_entity_contact(conn, entities["contact"], message_id)

        # DEAL (сумма + контакт)
        if entities.get("amounts") and contact_id:
            _upsert_deal(conn, entities["amounts"], contact_id, message_id)

        # TASK
        if entities.get("task"):
            _create_task(conn, entities["task"], contact_id, message_id)

        # LEAD
        if entities.get("lead") and contact_id:
            _upsert_lead(conn, entities["lead"], contact_id, message_id)

        # AGREEMENT
        if entities.get("agreement") and contact_id:
            _create_agreement(conn, entities["agreement"], contact_id, message_id)

        conn.commit()
    except Exception as e:
        logger.error(f"CRM upsert error: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()


def _upsert_entity_contact(conn: sqlite3.Connection, c: dict,
                            message_id: int) -> int | None:
    """Contact entity → contacts table. Ищем существующий перед созданием."""
    tg = c.get("tg_username")
    name = c.get("name") or "Unknown"

    # 1. Поиск по tg_username
    if tg:
        existing = conn.execute(
            "SELECT id FROM contacts WHERE tg_username = ?", (tg,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE contacts SET name = COALESCE(?, name), updated_at = datetime('now') WHERE id = ?",
                (name, existing["id"]),
            )
            return existing["id"]

    # 2. Поиск по имени (если имя уникальное — лучше чем дубль)
    if name and name != "Unknown":
        existing = conn.execute(
            "SELECT id FROM contacts WHERE name = ? AND tg_username IS NULL",
            (name,),
        ).fetchone()
        if existing:
            if tg:
                conn.execute(
                    "UPDATE contacts SET tg_username = ?, updated_at = datetime('now') WHERE id = ?",
                    (tg, existing["id"]),
                )
            return existing["id"]

    # 3. Создаём нового
    conn.execute(
        "INSERT INTO contacts (name, tg_username, notes) VALUES (?, ?, ?)",
        (name, tg, f"auto-created msg#{message_id}"),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    logger.info(f"CRM contact created: #{cid} {name}")
    return cid


def _upsert_deal(conn: sqlite3.Connection, amounts: list,
                  contact_id: int, message_id: int):
    """Amounts → deal (create or update)."""
    # Last amount (не max!) — «Было 150, стало 120» → 120 актуальна
    # Max ломает на скидках, дельтах, процентах
    amount = amounts[-1] if amounts else 0

    existing = conn.execute(
        """SELECT id, amount FROM deals
           WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
           ORDER BY created_at DESC LIMIT 1""",
        (contact_id,),
    ).fetchone()

    if existing:
        if amount > (existing["amount"] or 0):
            conn.execute(
                "UPDATE deals SET amount = ?, updated_at = datetime('now') WHERE id = ?",
                (amount, existing["id"]),
            )
    else:
        conn.execute(
            "INSERT INTO deals (contact_id, amount, stage, notes) VALUES (?, ?, 'discovery', ?)",
            (contact_id, amount, f"auto msg#{message_id}"),
        )
        logger.info(f"CRM deal created: {amount} for contact #{contact_id}")


def _create_task(conn: sqlite3.Connection, t: dict,
                  contact_id: int | None, message_id: int):
    """Task entity → tasks table."""
    conn.execute(
        """INSERT INTO tasks (description, related_type, related_id, due_at,
                              priority, source_message_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            t.get("description", "Untitled"),
            "contact" if contact_id else "other",
            contact_id,
            t.get("due_date"),
            t.get("priority", "normal"),
            message_id,
        ),
    )
    logger.info(f"CRM task created: {t.get('description', '?')[:40]}")


def _upsert_lead(conn: sqlite3.Connection, l: dict,
                  contact_id: int, message_id: int):
    """Lead entity → leads table."""
    existing = conn.execute(
        "SELECT id FROM leads WHERE contact_id = ? AND status NOT IN ('won', 'lost')",
        (contact_id,),
    ).fetchone()

    if not existing:
        conn.execute(
            """INSERT INTO leads (contact_id, source, amount, probability, status)
               VALUES (?, ?, ?, ?, 'new')""",
            (contact_id, l.get("source", "telegram"),
             l.get("amount"), l.get("probability", 0.5)),
        )
        logger.info(f"CRM lead created for contact #{contact_id}")


def _create_agreement(conn: sqlite3.Connection, a: dict,
                       contact_id: int, message_id: int):
    """Agreement entity → agreements table."""
    conn.execute(
        """INSERT INTO agreements (contact_id, summary, amount, due_at, source_message_id)
           VALUES (?, ?, ?, ?, ?)""",
        (contact_id, a.get("summary"), a.get("amount"),
         a.get("due_date"), message_id),
    )
    logger.info(f"CRM agreement created for contact #{contact_id}")
