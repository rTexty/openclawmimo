import sys

from mem._db import get_db


def _expand_entity_context(top_results, conn=None, max_memories=8, max_messages=5):
    """
    Entity-aware context expansion.

    Из top-N результатов recall строит полную цепочку контекста:
    - memory → contact (кто клиент, компания)
    - memory → deal (сумма, стадия, сроки)
    - deal → tasks (что нужно сделать)
    - contact → другие memories (история общения)
    - chat_thread → последние сообщения (живой контекст)

    Это НЕ graph RAG — это traversal по конкретным FK-связям.
    Каждое звено в цепочке = реальная сущность, не вероятность.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()

    expansion = {
        "contacts": {},  # contact_id → {name, tg_username, company, ...}
        "deals": {},  # deal_id → {amount, stage, contact_name, ...}
        "tasks": [],  # [{description, due_at, priority, related_deal}]
        "memories": [],  # другие memories о тех же contacts/deals
        "messages": [],  # последние сообщения из чатов
    }

    try:
        contact_ids = set()
        deal_ids = set()
        chat_thread_ids = set()
        source_memory_ids = set()

        # 1. Собираем FK-ссылки из top результатов
        for r in top_results:
            rid = r.get("id")
            if rid:
                source_memory_ids.add(rid)

            # Ищем memory в БД для получения FK
            try:
                mrow = (
                    conn.execute(
                        """SELECT contact_id, deal_id, chat_thread_id, source_message_id
                       FROM memories WHERE id = ?""",
                        (rid,),
                    ).fetchone()
                    if rid
                    else None
                )
            except Exception:
                mrow = None

            if mrow:
                if mrow["contact_id"]:
                    contact_ids.add(mrow["contact_id"])
                if mrow["deal_id"]:
                    deal_ids.add(mrow["deal_id"])
                if mrow["chat_thread_id"]:
                    chat_thread_ids.add(mrow["chat_thread_id"])

        # 2. CONTACTS — кто клиенты
        if contact_ids:
            placeholders = ",".join("?" * len(contact_ids))
            rows = conn.execute(
                f"""
                SELECT c.id, c.name, c.tg_username, c.phones, c.company_id,
                       comp.name as company_name, c.notes
                FROM contacts c
                LEFT JOIN companies comp ON c.company_id = comp.id
                WHERE c.id IN ({placeholders})
            """,
                list(contact_ids),
            ).fetchall()
            for r in rows:
                expansion["contacts"][r["id"]] = {
                    "name": r["name"],
                    "tg_username": r["tg_username"],
                    "phones": r["phones"],
                    "company": r["company_name"],
                    "notes": r["notes"],
                }

        # 3. DEALS — активные сделки с деталями
        if deal_ids:
            placeholders = ",".join("?" * len(deal_ids))
            rows = conn.execute(
                f"""
                SELECT d.id, d.amount, d.stage, d.expected_close_at, d.notes,
                       c.name as contact_name, l.source as lead_source
                FROM deals d
                JOIN contacts c ON d.contact_id = c.id
                LEFT JOIN leads l ON d.lead_id = l.id
                WHERE d.id IN ({placeholders})
            """,
                list(deal_ids),
            ).fetchall()
            for r in rows:
                expansion["deals"][r["id"]] = {
                    "amount": r["amount"],
                    "stage": r["stage"],
                    "expected_close_at": r["expected_close_at"],
                    "contact_name": r["contact_name"],
                    "lead_source": r["lead_source"],
                    "notes": r["notes"],
                }

        # 4. TASKS — задачи по связанным сделкам и контактам
        task_conditions = []
        task_params = []
        if deal_ids:
            placeholders = ",".join("?" * len(deal_ids))
            task_conditions.append(
                f"(related_type = 'deal' AND related_id IN ({placeholders}))"
            )
            task_params.extend(list(deal_ids))
        if contact_ids:
            placeholders = ",".join("?" * len(contact_ids))
            task_conditions.append(
                f"(related_type = 'contact' AND related_id IN ({placeholders}))"
            )
            task_params.extend(list(contact_ids))

        if task_conditions:
            where = " OR ".join(task_conditions)
            rows = conn.execute(
                f"""
                SELECT description, due_at, priority, status, related_type, related_id
                FROM tasks
                WHERE ({where}) AND status NOT IN ('done', 'cancelled')
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                    due_at ASC
                LIMIT 10
            """,
                task_params,
            ).fetchall()
            for r in rows:
                expansion["tasks"].append(
                    {
                        "description": r["description"],
                        "due_at": r["due_at"],
                        "priority": r["priority"],
                        "status": r["status"],
                    }
                )

        # 5. MEMORIES — другие memories о тех же контактах/сделках
        mem_conditions = []
        mem_params = []
        if contact_ids:
            placeholders = ",".join("?" * len(contact_ids))
            mem_conditions.append(f"contact_id IN ({placeholders})")
            mem_params.extend(list(contact_ids))
        if deal_ids:
            placeholders = ",".join("?" * len(deal_ids))
            mem_conditions.append(f"deal_id IN ({placeholders})")
            mem_params.extend(list(deal_ids))

        if mem_conditions and source_memory_ids:
            where = " OR ".join(mem_conditions)
            src_placeholders = ",".join("?" * len(source_memory_ids))
            rows = conn.execute(
                f"""
                SELECT id, content, type, importance, created_at
                FROM memories
                WHERE ({where}) AND id NOT IN ({src_placeholders})
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """,
                mem_params + list(source_memory_ids) + [max_memories],
            ).fetchall()
            for r in rows:
                expansion["memories"].append(
                    {
                        "content": r["content"],
                        "type": r["type"],
                        "importance": r["importance"],
                        "created_at": r["created_at"],
                    }
                )

        # 6. MESSAGES — последние сообщения из связанных чатов
        if chat_thread_ids:
            placeholders = ",".join("?" * len(chat_thread_ids))
            rows = conn.execute(
                f"""
                SELECT m.text, m.from_user_id, m.sent_at, ct.title as chat_title
                FROM messages m
                JOIN chat_threads ct ON m.chat_thread_id = ct.id
                WHERE m.chat_thread_id IN ({placeholders})
                  AND (m.meta_json IS NULL OR json_extract(m.meta_json, '$.deleted') IS NULL)
                ORDER BY m.sent_at DESC
                LIMIT ?
            """,
                list(chat_thread_ids) + [max_messages],
            ).fetchall()
            for r in rows:
                author = (
                    "Я"
                    if r["from_user_id"] == "self"
                    else (r["chat_title"] or "Клиент")
                )
                expansion["messages"].append(
                    {
                        "author": author,
                        "text": r["text"][:150] if r["text"] else "",
                        "sent_at": r["sent_at"],
                    }
                )

    except Exception as e:
        print(f"⚠️ Entity expansion error: {e}", file=sys.stderr)
    finally:
        if own_conn:
            conn.close()

    return expansion


def _format_entity_context(expansion):
    """Форматировать entity expansion в читаемый блок контекста."""
    parts = []

    if expansion["contacts"]:
        for cid, c in expansion["contacts"].items():
            line = f"👤 {c['name']}"
            if c.get("tg_username"):
                line += f" (@{c['tg_username']})"
            if c.get("company"):
                line += f", {c['company']}"
            parts.append(line)

    if expansion["deals"]:
        for did, d in expansion["deals"].items():
            amount = f"{d['amount']:,.0f}₽" if d.get("amount") else "?"
            line = f"💰 Сделка: {amount}, стадия: {d['stage']}"
            if d.get("expected_close_at"):
                line += f", до {d['expected_close_at'][:10]}"
            if d.get("contact_name"):
                line += f" ({d['contact_name']})"
            parts.append(line)

    if expansion["tasks"]:
        task_lines = []
        for t in expansion["tasks"][:5]:
            icon = (
                "🔴"
                if t["priority"] == "urgent"
                else "🟡"
                if t["priority"] == "high"
                else "⚪"
            )
            due = f" (до {t['due_at'][:10]})" if t.get("due_at") else ""
            task_lines.append(f"  {icon} {t['description'][:60]}{due}")
        if task_lines:
            parts.append("📋 Задачи:\n" + "\n".join(task_lines))

    if expansion["memories"]:
        mem_lines = []
        for m in expansion["memories"][:5]:
            mem_lines.append(f"  • {m['content'][:100]}")
        if mem_lines:
            parts.append("🧠 История:\n" + "\n".join(mem_lines))

    if expansion["messages"]:
        msg_lines = []
        for m in reversed(expansion["messages"]):
            msg_lines.append(f"  [{m['author']}: {m['text']}]")
        if msg_lines:
            parts.append("💬 Контекст чата:\n" + "\n".join(msg_lines))

    return "\n".join(parts) if parts else ""
