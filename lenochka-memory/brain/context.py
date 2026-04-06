"""Context-packet для LLM — локальная версия без импорта mem.py."""

import json
import re
import sys

from brain._config import DB_PATH
from brain._db import _get_db
from brain.embed import embed_text, vec_to_blob


def _expand_entity_context_local(top_results, conn):
    """
    Entity-aware context expansion — локальная версия для brain.py.
    Использует переданный conn (не создаёт свой) — нет дублей соединений.
    Не импортирует mem.py — нет циклического импорта.
    """
    expansion = {
        "contacts": {},
        "deals": {},
        "tasks": [],
        "memories": [],
        "messages": [],
    }

    contact_ids = set()
    deal_ids = set()
    chat_thread_ids = set()
    source_memory_ids = set()

    # 1. Собираем FK-ссылки из top результатов
    for r in top_results:
        rid = r.get("id")
        if rid:
            source_memory_ids.add(rid)
        try:
            mrow = (
                conn.execute(
                    "SELECT contact_id, deal_id, chat_thread_id FROM memories WHERE id = ?",
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

    # 2. CONTACTS
    if contact_ids:
        ph = ",".join("?" * len(contact_ids))
        rows = conn.execute(
            f"""
            SELECT c.id, c.name, c.tg_username, c.phones, c.company_id,
                   comp.name as company_name, c.notes
            FROM contacts c LEFT JOIN companies comp ON c.company_id = comp.id
            WHERE c.id IN ({ph})
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

    # 3. DEALS
    if deal_ids:
        ph = ",".join("?" * len(deal_ids))
        rows = conn.execute(
            f"""
            SELECT d.id, d.amount, d.stage, d.expected_close_at, d.notes,
                   c.name as contact_name, l.source as lead_source
            FROM deals d JOIN contacts c ON d.contact_id = c.id
            LEFT JOIN leads l ON d.lead_id = l.id
            WHERE d.id IN ({ph})
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

    # 4. TASKS
    task_conds, task_params = [], []
    if deal_ids:
        ph = ",".join("?" * len(deal_ids))
        task_conds.append(f"(related_type = 'deal' AND related_id IN ({ph}))")
        task_params.extend(list(deal_ids))
    if contact_ids:
        ph = ",".join("?" * len(contact_ids))
        task_conds.append(f"(related_type = 'contact' AND related_id IN ({ph}))")
        task_params.extend(list(contact_ids))
    if task_conds:
        where = " OR ".join(task_conds)
        rows = conn.execute(
            f"""
            SELECT description, due_at, priority, status FROM tasks
            WHERE ({where}) AND status NOT IN ('done', 'cancelled')
            ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                     due_at ASC LIMIT 10
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

    # 5. MEMORIES — другие memories о тех же contacts/deals
    mem_conds, mem_params = [], []
    if contact_ids:
        ph = ",".join("?" * len(contact_ids))
        mem_conds.append(f"contact_id IN ({ph})")
        mem_params.extend(list(contact_ids))
    if deal_ids:
        ph = ",".join("?" * len(deal_ids))
        mem_conds.append(f"deal_id IN ({ph})")
        mem_params.extend(list(deal_ids))
    if mem_conds and source_memory_ids:
        where = " OR ".join(mem_conds)
        src_ph = ",".join("?" * len(source_memory_ids))
        rows = conn.execute(
            f"""
            SELECT id, content, type, importance, created_at FROM memories
            WHERE ({where}) AND id NOT IN ({src_ph})
            ORDER BY importance DESC, created_at DESC LIMIT 8
        """,
            mem_params + list(source_memory_ids),
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
        ph = ",".join("?" * len(chat_thread_ids))
        rows = conn.execute(
            f"""
            SELECT m.text, m.from_user_id, m.sent_at, ct.title as chat_title
            FROM messages m JOIN chat_threads ct ON m.chat_thread_id = ct.id
            WHERE m.chat_thread_id IN ({ph})
              AND (m.meta_json IS NULL OR json_extract(m.meta_json, '$.deleted') IS NULL)
            ORDER BY m.sent_at DESC LIMIT 5
        """,
            list(chat_thread_ids),
        ).fetchall()
        for r in rows:
            author = (
                "Я" if r["from_user_id"] == "self" else (r["chat_title"] or "Клиент")
            )
            expansion["messages"].append(
                {
                    "author": author,
                    "text": r["text"][:150] if r["text"] else "",
                    "sent_at": r["sent_at"],
                }
            )

    return expansion


def build_context_packet(
    query, contact_id=None, deal_id=None, chat_thread_id=None, intent="search", limit=15
):
    """
    Собрать контекст-пакет для LLM.
    Режимы: core | search | recall
    """
    conn = _get_db()
    packet = {
        "facts": [],
        "episodes": [],
        "related": [],
        "notes": [],
        "intent": intent,
    }

    # 1. CRM-данные
    if contact_id:
        contact = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        if contact:
            packet["facts"].append({"type": "contact", "data": dict(contact)})

        deals = conn.execute(
            """
            SELECT * FROM deals WHERE contact_id = ?
            ORDER BY updated_at DESC LIMIT 5
        """,
            (contact_id,),
        ).fetchall()
        for d in deals:
            packet["facts"].append({"type": "deal", "data": dict(d)})

        tasks = conn.execute(
            """
            SELECT * FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND status NOT IN ('done', 'cancelled')
            ORDER BY due_at ASC LIMIT 5
        """,
            (contact_id,),
        ).fetchall()
        for t in tasks:
            packet["facts"].append({"type": "task", "data": dict(t)})

    if deal_id:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal:
            packet["facts"].append({"type": "deal", "data": dict(deal)})

        agreements = conn.execute(
            """
            SELECT * FROM agreements WHERE deal_id = ?
            ORDER BY created_at DESC LIMIT 5
        """,
            (deal_id,),
        ).fetchall()
        for a in agreements:
            packet["facts"].append({"type": "agreement", "data": dict(a)})

    # 2. Memories — векторный поиск если доступен, иначе keyword
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        vec = embed_text(query)
        vec_blob = vec_to_blob(vec)
        mem_rows = conn.execute(
            """
            SELECT vm.rowid as id, m.content, m.type, m.importance, m.created_at,
                   distance
            FROM vec_memories vm
            JOIN memories m ON vm.rowid = m.id
            WHERE vm.embedding MATCH ? AND k = ?
            ORDER BY distance
        """,
            (vec_blob, limit),
        ).fetchall()

        for m in mem_rows:
            packet["episodes"].append(
                {
                    "id": m["id"],
                    "content": m["content"],
                    "type": m["type"],
                    "importance": m["importance"],
                    "created_at": m["created_at"],
                    "score": round(1.0 - m["distance"], 3)
                    if "distance" in m.keys()
                    else 0,
                }
            )
    except Exception:
        # Fallback: FTS trigram поиск
        try:
            mem_rows = conn.execute(
                """
                SELECT m.id, m.content, m.type, m.importance, m.created_at, rank
                FROM memories_fts
                JOIN memories m ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """,
                (query, limit),
            ).fetchall()
            for m in mem_rows:
                packet["episodes"].append(
                    {
                        "id": m["id"],
                        "content": m["content"],
                        "type": m["type"],
                        "importance": m["importance"],
                        "created_at": m["created_at"],
                        "score": abs(m["rank"]) if m["rank"] else 0,
                    }
                )
        except Exception:
            # Last fallback: LIKE
            keywords = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
            if keywords:
                like_conds = " OR ".join(["LOWER(content) LIKE ?"] * len(keywords))
                params = [f"%{kw}%" for kw in keywords]
            else:
                like_conds = "content LIKE ?"
                params = [f"%{query}%"]
            if contact_id:
                like_conds += " AND contact_id = ?"
                params.append(contact_id)
            params.append(limit)
            mem_rows = conn.execute(
                f"""
                SELECT id, content, type, importance, created_at
                FROM memories WHERE {like_conds}
                ORDER BY importance DESC, created_at DESC LIMIT ?
            """,
                params,
            ).fetchall()
            for m in mem_rows:
                packet["episodes"].append(
                    {
                        "id": m["id"],
                        "content": m["content"],
                        "type": m["type"],
                        "importance": m["importance"],
                        "created_at": m["created_at"],
                    }
                )

    # 3. Memories FTS — trigram поиск по category
    try:
        fts_rows = conn.execute(
            """
            SELECT m.id, m.content, m.category, m.importance, rank
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.id
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """,
            (query, limit),
        ).fetchall()
        for c in fts_rows:
            packet["related"].append(
                {
                    "id": c["id"],
                    "content": c["content"],
                    "category": c["category"],
                    "priority": c["importance"],
                    "score": abs(c["rank"]) if c["rank"] else 0,
                }
            )
    except Exception:
        pass

    # 4. Associations (1 hop) от top memories
    if packet["episodes"]:
        top_mem_ids = [e["id"] for e in packet["episodes"][:5]]
        placeholders = ",".join("?" * len(top_mem_ids))
        try:
            assoc_rows = conn.execute(
                f"""
                SELECT m.content, m.type, a.relation_type, a.weight
                FROM associations a
                JOIN memories m ON (
                    CASE WHEN a.memory_id_from IN ({placeholders})
                         THEN a.memory_id_to ELSE a.memory_id_from END = m.id
                )
                WHERE (a.memory_id_from IN ({placeholders})
                    OR a.memory_id_to IN ({placeholders}))
                  AND m.id NOT IN ({placeholders})
                ORDER BY a.weight DESC LIMIT 5
            """,
                top_mem_ids + top_mem_ids + top_mem_ids + top_mem_ids,
            ).fetchall()

            for a in assoc_rows:
                packet["notes"].append(
                    {
                        "type": "association",
                        "content": a["content"],
                        "relation": a["relation_type"],
                        "weight": a["weight"],
                    }
                )
        except Exception:
            pass

    # 5. Core mode: ключевые semantic/procedural memories
    if intent == "core":
        core_rows = conn.execute("""
            SELECT content, type, importance FROM memories
            WHERE type IN ('semantic', 'procedural') AND importance >= 0.7
            ORDER BY importance DESC LIMIT 10
        """).fetchall()
        for m in core_rows:
            packet["facts"].append(
                {
                    "type": f"memory_{m['type']}",
                    "data": {"content": m["content"], "importance": m["importance"]},
                }
            )

    # 6. Entity expansion — цепочка по FK-связям (contact → deal → tasks → history)
    #    Не импортируем mem.py (цикл!). Вызываем напрямую через тот же conn.
    try:
        top_for_expansion = packet["episodes"][:5] + [
            {"id": r.get("id"), "source": r.get("source")}
            for r in packet["related"][:3]
        ]
        expansion = _expand_entity_context_local(top_for_expansion, conn)
        if expansion:
            # Добавляем entity context как structured data
            if expansion["contacts"]:
                for cid, c in expansion["contacts"].items():
                    packet["facts"].append(
                        {
                            "type": "contact",
                            "data": c,
                        }
                    )
            if expansion["deals"]:
                for did, d in expansion["deals"].items():
                    packet["facts"].append(
                        {
                            "type": "deal",
                            "data": d,
                        }
                    )
            if expansion["tasks"]:
                packet["notes"].append(
                    {
                        "type": "tasks",
                        "content": "; ".join(
                            f"[{t['priority']}] {t['description'][:50]}"
                            for t in expansion["tasks"][:5]
                        ),
                    }
                )
            if expansion["memories"]:
                for m in expansion["memories"][:3]:
                    packet["notes"].append(
                        {
                            "type": "related_memory",
                            "content": m["content"][:100],
                        }
                    )
            if expansion["messages"]:
                msg_text = " | ".join(
                    f"{m['author']}: {m['text'][:60]}"
                    for m in reversed(expansion["messages"][-3:])
                )
                packet["notes"].append(
                    {
                        "type": "chat_context",
                        "content": msg_text,
                    }
                )
    except Exception:
        pass  # entity expansion — nice-to-have, не критично

    conn.close()
    return packet
