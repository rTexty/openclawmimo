#!/usr/bin/env python3
"""
Lenochka Memory CLI v2 — Единый инструмент памяти
Объединяет CRM-БД + Agent Memory (vector) + CHAOS в одном CLI.

Использование:
    python3 mem.py init                          — создать БД
    python3 mem.py store "текст" [--type ...]    — записать memory
    python3 mem.py recall "запрос" [--strategy]  — поиск по памяти
    python3 mem.py recall-assoc --from-id N      — связанные memories
    python3 mem.py chaos-store "текст"           — запись в CHAOS
    python3 mem.py chaos-search "запрос"         — поиск в CHAOS
    python3 mem.py chaos-reindex                 — пересобрать FTS
    python3 mem.py crm <subcommand>              — CRM-запросы
    python3 mem.py ingest "текст" [--contact-id] — полный пайплайн
    python3 mem.py context "запрос"              — контекст-пакет для LLM
    python3 mem.py digest                        — дайджест
    python3 mem.py weekly                        — недельный дайджест
    python3 mem.py consolidate                   — консолидация
    python3 mem.py prune-messages --older-than N — архивация
    python3 mem.py stats                         — статистика
"""

import sqlite3
import sys
import os
import json
import hashlib
import struct
from datetime import datetime, timedelta
from pathlib import Path

# === CONFIG ===
DB_DIR = Path(__file__).parent / "db"
DB_PATH = DB_DIR / "lenochka.db"
SCHEMA_PATH = Path(__file__).parent / "schemas" / "init.sql"
EMBEDDING_DIM = 384


def get_db():
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _load_vec(conn):
    """Загрузить sqlite-vec расширение."""
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


# Current schema version — increment on every schema change
SCHEMA_VERSION = 2


def init():
    """Инициализация базы данных."""
    conn = get_db()

    if not DB_PATH.exists():
        # Новая БД — создаём полную схему
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)

        # Создать векторные таблицы через sqlite-vec
        if _load_vec(conn):
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[{EMBEDDING_DIM}])")
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chaos USING vec0(embedding float[{EMBEDDING_DIM}])")
            print("✅ Векторные таблицы (sqlite-vec) созданы")
        else:
            print("⚠️ sqlite-vec недоступен — векторный поиск отключён")

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        conn.close()
        print(f"✅ БД создана: {DB_PATH}")
    else:
        # БД существует — проверяем миграции
        _migrate_db(conn)
        conn.close()


def _migrate_db(conn):
    """Миграции схемы по PRAGMA user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current >= SCHEMA_VERSION:
        print(f"✅ Схема актуальная (v{current})")
        return

    print(f"🔄 Миграция: v{current} → v{SCHEMA_VERSION}")

    if current < 2:
        # v2: content_hash в messages, tg_user_id в contacts
        _migrate_v2(conn)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    print(f"✅ Миграция завершена → v{SCHEMA_VERSION}")


def _migrate_v2(conn):
    """v1 → v2: content_hash + tg_user_id."""
    # messages.content_hash
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_content_hash ON messages(content_hash)")
        print("   + messages.content_hash")

    # contacts.tg_user_id
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
    if "tg_user_id" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN tg_user_id TEXT UNIQUE")
        print("   + contacts.tg_user_id")
    print(f"✅ БД создана: {DB_PATH}")


# === STORE (Capture) ===

def store(content, mem_type="episodic", importance=0.5, contact_id=None,
          chat_thread_id=None, deal_id=None, source_message_id=None, tags=None,
          content_hash=None, auto_associate=True):
    """Записать memory в Agent Memory + векторный эмбеддинг (одна транзакция)."""
    conn = get_db()
    tags_json = json.dumps(tags) if tags else None
    chash = content_hash or _content_hash(content)
    mid = None
    try:
        conn.execute("""
            INSERT INTO memories (content, content_hash, type, importance, strength, contact_id,
                                 chat_thread_id, deal_id, source_message_id, tags)
            VALUES (?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)
        """, (content, chash, mem_type, importance, contact_id, chat_thread_id,
              deal_id, source_message_id, tags_json))
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Векторный эмбеддинг → vec_memories (в той же транзакции)
        try:
            from brain import embed_text, vec_to_blob
            vec = embed_text(content)
            vec_blob = vec_to_blob(vec)
            if _load_vec(conn):
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (mid, vec_blob)
                )
        except Exception as e:
            print(f"⚠️ Не удалось записать вектор: {e}", file=sys.stderr)
            # Вектор не критичен — memory остаётся, fallback поиск работает

        # Один COMMIT на обе операции (memory + vector)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"❌ store() failed: {e}", file=sys.stderr)
        conn.close()
        raise
    conn.close()
    print(f"✅ Memory #{mid} записан [{mem_type}] importance={importance}")

    # Автосвязывание (вне транзакции — не критично)
    if auto_associate:
        try:
            from brain import auto_associate as _auto_assoc
            assocs = _auto_assoc(mid, content)
            if assocs:
                print(f"   🔗 Создано {len(assocs)} ассоциаций")
        except Exception:
            pass

    return mid


# === RECALL (Search) ===

def recall(query, strategy="hybrid", contact_id=None, deal_id=None,
           mem_type=None, limit=20):
    """
    Поиск по памяти.
    strategy: hybrid | vector | bm25 | keyword
    """
    conn = get_db()
    results = []

    # 1. Векторный поиск (sqlite-vec)
    if strategy in ("hybrid", "vector"):
        try:
            from brain import embed_text, vec_to_blob
            vec = embed_text(query)
            vec_blob = vec_to_blob(vec)
            if _load_vec(conn):
                rows = conn.execute("""
                    SELECT vm.rowid as id, m.content, m.type, m.importance,
                           m.strength, m.contact_id, m.created_at, distance
                    FROM vec_memories vm
                    JOIN memories m ON vm.rowid = m.id
                    WHERE vm.embedding MATCH ? AND k = ?
                    ORDER BY distance
                """, (vec_blob, limit)).fetchall()
                for r in rows:
                    score = 1.0 - r["distance"] if r["distance"] is not None else 0
                    results.append({
                        "id": r["id"], "content": r["content"], "type": r["type"],
                        "importance": r["importance"], "strength": r["strength"],
                        "contact_id": r["contact_id"], "created_at": r["created_at"],
                        "score": round(score, 4), "source": "vector",
                    })
        except Exception as e:
            pass

    # 2. BM25 по CHAOS FTS (trigram)
    if strategy in ("hybrid", "bm25"):
        try:
            rows = conn.execute("""
                SELECT ce.id, ce.content, ce.category, ce.priority, ce.contact_id,
                       rank, 'chaos' as source
                FROM chaos_fts
                JOIN chaos_entries ce ON chaos_fts.rowid = ce.id
                WHERE chaos_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
            for r in rows:
                results.append({
                    "id": r["id"], "content": r["content"],
                    "category": r["category"], "priority": r["priority"],
                    "contact_id": r["contact_id"],
                    "score": abs(r["rank"]) if r["rank"] else 0,
                    "source": "chaos",
                })
        except Exception as e:
            print(f"⚠️ FTS chaos search error: {e}", file=sys.stderr)

    # 3. Keyword search по memories (LIKE fallback)
    if strategy in ("hybrid", "keyword"):
        sql = """
            SELECT id, content, type, importance, strength, contact_id, chat_thread_id,
                   deal_id, created_at, 'agent_memory' as source
            FROM memories
            WHERE content LIKE ?
        """
        params = [f"%{query}%"]

        if contact_id:
            sql += " AND contact_id = ?"
            params.append(contact_id)
        if deal_id:
            sql += " AND deal_id = ?"
            params.append(deal_id)
        if mem_type:
            sql += " AND type = ?"
            params.append(mem_type)

        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            kw_count = r["content"].lower().count(query.lower())
            score = r["importance"] * r["strength"] + kw_count * 0.3
            results.append({
                "id": r["id"], "content": r["content"], "type": r["type"],
                "importance": r["importance"], "strength": r["strength"],
                "contact_id": r["contact_id"], "created_at": r["created_at"],
                "score": score, "source": "agent_memory",
            })

    # Dedup + sort (conn открыт — entity expansion ниже его использует)
    seen = set()
    unique = []
    for r in results:
        key = (r.get("source", ""), r["id"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Entity-aware expansion: расширяем контекст по FK-связям
    # Передаём существующий conn (одно соединение на весь recall)
    if unique:
        expansion = _expand_entity_context(unique[:5], conn=conn)
        # Добавляем расширенный контекст как отдельную секцию
        if expansion:
            unique.append({
                "id": 0,
                "content": _format_entity_context(expansion),
                "type": "entity_context",
                "importance": 0.0,
                "score": -1,  # всегда в конце
                "source": "entity_expansion",
                "_expansion": expansion,
            })

    conn.close()
    return unique[:limit]


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
        "contacts": {},    # contact_id → {name, tg_username, company, ...}
        "deals": {},       # deal_id → {amount, stage, contact_name, ...}
        "tasks": [],       # [{description, due_at, priority, related_deal}]
        "memories": [],    # другие memories о тех же contacts/deals
        "messages": [],    # последние сообщения из чатов
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
                mrow = conn.execute(
                    """SELECT contact_id, deal_id, chat_thread_id, source_message_id
                       FROM memories WHERE id = ?""",
                    (rid,),
                ).fetchone() if rid else None
            except Exception:
                mrow = None

            if mrow:
                if mrow["contact_id"]:
                    contact_ids.add(mrow["contact_id"])
                if mrow["deal_id"]:
                    deal_ids.add(mrow["deal_id"])
                if mrow["chat_thread_id"]:
                    chat_thread_ids.add(mrow["chat_thread_id"])

            # Также проверяем chaos_entries → memory_id → FK
            try:
                crow = conn.execute(
                    """SELECT m.contact_id, m.deal_id, m.chat_thread_id
                       FROM chaos_entries ce
                       LEFT JOIN memories m ON ce.memory_id = m.id
                       WHERE ce.id = ?""",
                    (rid,),
                ).fetchone() if r.get("source") == "chaos" else None
            except Exception:
                crow = None

            if crow:
                if crow["contact_id"]:
                    contact_ids.add(crow["contact_id"])
                if crow["deal_id"]:
                    deal_ids.add(crow["deal_id"])
                if crow["chat_thread_id"]:
                    chat_thread_ids.add(crow["chat_thread_id"])

        # 2. CONTACTS — кто клиенты
        if contact_ids:
            placeholders = ",".join("?" * len(contact_ids))
            rows = conn.execute(f"""
                SELECT c.id, c.name, c.tg_username, c.phones, c.company_id,
                       comp.name as company_name, c.notes
                FROM contacts c
                LEFT JOIN companies comp ON c.company_id = comp.id
                WHERE c.id IN ({placeholders})
            """, list(contact_ids)).fetchall()
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
            rows = conn.execute(f"""
                SELECT d.id, d.amount, d.stage, d.expected_close_at, d.notes,
                       c.name as contact_name, l.source as lead_source
                FROM deals d
                JOIN contacts c ON d.contact_id = c.id
                LEFT JOIN leads l ON d.lead_id = l.id
                WHERE d.id IN ({placeholders})
            """, list(deal_ids)).fetchall()
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
            task_conditions.append(f"(related_type = 'deal' AND related_id IN ({placeholders}))")
            task_params.extend(list(deal_ids))
        if contact_ids:
            placeholders = ",".join("?" * len(contact_ids))
            task_conditions.append(f"(related_type = 'contact' AND related_id IN ({placeholders}))")
            task_params.extend(list(contact_ids))

        if task_conditions:
            where = " OR ".join(task_conditions)
            rows = conn.execute(f"""
                SELECT description, due_at, priority, status, related_type, related_id
                FROM tasks
                WHERE ({where}) AND status NOT IN ('done', 'cancelled')
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                    due_at ASC
                LIMIT 10
            """, task_params).fetchall()
            for r in rows:
                expansion["tasks"].append({
                    "description": r["description"],
                    "due_at": r["due_at"],
                    "priority": r["priority"],
                    "status": r["status"],
                })

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
            rows = conn.execute(f"""
                SELECT id, content, type, importance, created_at
                FROM memories
                WHERE ({where}) AND id NOT IN ({src_placeholders})
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, mem_params + list(source_memory_ids) + [max_memories]).fetchall()
            for r in rows:
                expansion["memories"].append({
                    "content": r["content"],
                    "type": r["type"],
                    "importance": r["importance"],
                    "created_at": r["created_at"],
                })

        # 6. MESSAGES — последние сообщения из связанных чатов
        if chat_thread_ids:
            placeholders = ",".join("?" * len(chat_thread_ids))
            rows = conn.execute(f"""
                SELECT m.text, m.from_user_id, m.sent_at, ct.title as chat_title
                FROM messages m
                JOIN chat_threads ct ON m.chat_thread_id = ct.id
                WHERE m.chat_thread_id IN ({placeholders})
                  AND (m.meta_json IS NULL OR json_extract(m.meta_json, '$.deleted') IS NULL)
                ORDER BY m.sent_at DESC
                LIMIT ?
            """, list(chat_thread_ids) + [max_messages]).fetchall()
            for r in rows:
                author = "Я" if r["from_user_id"] == "self" else (r["chat_title"] or "Клиент")
                expansion["messages"].append({
                    "author": author,
                    "text": r["text"][:150] if r["text"] else "",
                    "sent_at": r["sent_at"],
                })

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
            icon = "🔴" if t["priority"] == "urgent" else "🟡" if t["priority"] == "high" else "⚪"
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


def recall_assoc(memory_id, hops=1, limit=10):
    """Найти связанные memories через graph."""
    conn = get_db()
    visited = {memory_id}
    current = [memory_id]
    results = []

    for _ in range(hops):
        next_level = []
        for mid in current:
            rows = conn.execute("""
                SELECT m.*, a.relation_type, a.weight
                FROM associations a
                JOIN memories m ON (
                    CASE WHEN a.memory_id_from = ? THEN a.memory_id_to
                         ELSE a.memory_id_from END = m.id
                )
                WHERE (a.memory_id_from = ? OR a.memory_id_to = ?)
                  AND m.id NOT IN ({})
            """.format(",".join("?" * len(visited))),
                [mid, mid, mid] + list(visited)
            ).fetchall()

            for r in rows:
                visited.add(r["id"])
                next_level.append(r["id"])
                results.append({
                    "id": r["id"], "content": r["content"], "type": r["type"],
                    "relation": r["relation_type"], "weight": r["weight"],
                    "strength": r["strength"],
                })
        current = next_level

    conn.close()
    return results[:limit]


# === CHAOS ===

def chaos_store(content, category="other", priority=0.5,
                memory_id=None, contact_id=None):
    """Записать entry в CHAOS + векторный эмбеддинг (одна транзакция)."""
    conn = get_db()
    eid = None
    try:
        conn.execute("""
            INSERT INTO chaos_entries (content, category, priority, memory_id, contact_id)
            VALUES (?, ?, ?, ?, ?)
        """, (content, category, priority, memory_id, contact_id))
        eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Векторный эмбеддинг → vec_chaos (в той же транзакции)
        try:
            from brain import embed_text, vec_to_blob
            vec = embed_text(content)
            vec_blob = vec_to_blob(vec)
            if _load_vec(conn):
                conn.execute(
                    "INSERT INTO vec_chaos(rowid, embedding) VALUES (?, ?)",
                    (eid, vec_blob)
                )
        except Exception as e:
            print(f"⚠️ Не удалось записать вектор chaos: {e}", file=sys.stderr)

        # Один COMMIT на обе операции
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"❌ chaos_store() failed: {e}", file=sys.stderr)
        conn.close()
        raise

    conn.close()
    print(f"✅ CHAOS #{eid} записан [{category}] priority={priority}")
    return eid


def chaos_search(query, mode="index", limit=10):
    """Поиск в CHAOS (read-only — НЕ обновляет heat/access_count)."""
    conn = get_db()
    results = []

    if mode == "index":
        # BM25 via FTS (trigram tokenizer)
        try:
            rows = conn.execute("""
                SELECT ce.*, rank
                FROM chaos_fts
                JOIN chaos_entries ce ON chaos_fts.rowid = ce.id
                WHERE chaos_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
            results = [dict(r) for r in rows]
        except Exception as e:
            print(f"FTS error: {e}", file=sys.stderr)

        # Fallback LIKE (Cyrillic workaround)
        if not results:
            rows = conn.execute("""
                SELECT *, (priority + 0.1 * access_count) as heat_score
                FROM chaos_entries
                WHERE content LIKE ? OR category LIKE ?
                ORDER BY heat_score DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()
            results = [dict(r) for r in rows]

    elif mode == "full":
        rows = conn.execute("""
            SELECT *, (priority + 0.1 * access_count) as heat_score
            FROM chaos_entries
            WHERE content LIKE ?
            ORDER BY heat_score DESC
            LIMIT ?
        """, (f"%{query}%", limit)).fetchall()
        results = [dict(r) for r in rows]

    conn.close()
    return results


def chaos_touch(chaos_id: int):
    """Обновить heat при явном доступе к CHAOS entry (не поиск, а использование)."""
    conn = get_db()
    conn.execute("""
        UPDATE chaos_entries
        SET access_count = access_count + 1, last_accessed_at = datetime('now')
        WHERE id = ?
    """, (chaos_id,))
    conn.commit()
    conn.close()


def chaos_reindex():
    """Пересобрать FTS-индекс CHAOS."""
    conn = get_db()
    conn.execute("INSERT INTO chaos_fts(chaos_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    print("✅ CHAOS FTS-индекс пересобран")


# === CRM ===

def crm_contact(tg=None, contact_id=None):
    """Получить контакт."""
    conn = get_db()
    if contact_id:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    elif tg:
        tg_clean = tg.lstrip("@")
        row = conn.execute("SELECT * FROM contacts WHERE tg_username = ?", (tg_clean,)).fetchone()
    else:
        print("Укажите --tg или --contact-id")
        return None
    conn.close()
    return dict(row) if row else None


def crm_deals(contact_id=None):
    """Сделки контакта."""
    conn = get_db()
    if contact_id:
        rows = conn.execute("""
            SELECT d.*, c.name as contact_name FROM deals d
            JOIN contacts c ON d.contact_id = c.id
            WHERE d.contact_id = ?
            ORDER BY d.created_at DESC
        """, (contact_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT d.*, c.name as contact_name FROM deals d
            JOIN contacts c ON d.contact_id = c.id
            WHERE d.stage NOT IN ('closed_won', 'closed_lost')
            ORDER BY d.expected_close_at ASC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_overdue_tasks():
    """Просроченные задачи."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM v_overdue_tasks").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_abandoned(hours=24):
    """Брошенные диалоги."""
    conn = get_db()
    rows = conn.execute("""
        SELECT ct.id, ct.title, ct.tg_chat_id,
               c.name as contact_name, c.tg_username,
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
    conn.close()
    return [dict(r) for r in rows]


def crm_leads(since=None):
    """Лиды."""
    conn = get_db()
    if since:
        rows = conn.execute("""
            SELECT l.*, c.name as contact_name FROM leads l
            JOIN contacts c ON l.contact_id = c.id
            WHERE l.created_at >= ?
            ORDER BY l.created_at DESC
        """, (since,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT l.*, c.name as contact_name FROM leads l
            JOIN contacts c ON l.contact_id = c.id
            WHERE l.status NOT IN ('won', 'lost')
            ORDER BY l.created_at DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_daily_summary(date=None):
    """Сводка за день."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"

    result = {
        "date": date,
        "messages": conn.execute("SELECT COUNT(*) as cnt FROM messages WHERE sent_at BETWEEN ? AND ?", (start, end)).fetchone()["cnt"],
        "new_leads": conn.execute("SELECT COUNT(*) as cnt FROM leads WHERE created_at BETWEEN ? AND ?", (start, end)).fetchone()["cnt"],
        "new_tasks": conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE created_at BETWEEN ? AND ?", (start, end)).fetchone()["cnt"],
        "completed_tasks": conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'done' AND updated_at BETWEEN ? AND ?", (start, end)).fetchone()["cnt"],
        "memories": conn.execute("SELECT COUNT(*) as cnt FROM memories WHERE created_at BETWEEN ? AND ?", (start, end)).fetchone()["cnt"],
    }
    conn.close()
    return result


# === INGEST (полный пайплайн) ===

def _content_hash(text):
    """SHA-256 хэш контента для дедупликации."""
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()[:16]


def ingest(text, contact_id=None, chat_thread_id=None, source_message_id=None):
    """Полный пайплайн: classify → extract → store → chaos."""
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
            (source_message_id, chat_thread_id)
        ).fetchone()
        if existing_msg:
            conn.close()
            print(f"⏭️ Дубликат (source_message_id={source_message_id}), пропускаю")
            return {"label": "duplicate", "skipped": True, "existing_id": existing_msg["id"]}
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
            contact_id=contact_id,
            chat_thread_id=chat_thread_id,
            source_message_id=source_message_id,
            content_hash=content_hash,
            auto_associate=True,
        )

        # 4. CHAOS store
        chaos_store(
            content=text[:200],
            category=label,
            priority=importance,
            memory_id=mid,
            contact_id=contact_id,
        )
        result["stored"] = True
        result["memory_id"] = mid
        print(f"✅ Обработано и записано в память")
    else:
        print(f"⏭️ Тип '{label}' — пропускаю запись в память")

    return result


# === CONTEXT ===

def context(query, contact_id=None, deal_id=None, intent="search"):
    """Собрать контекст-пакет для LLM."""
    try:
        from brain import build_context_packet
        packet = build_context_packet(query, contact_id=contact_id,
                                     deal_id=deal_id, intent=intent)
        return packet
    except ImportError:
        print("Модуль brain.py не найден")
        return None


# === DIGEST ===

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


# === MAINTENANCE ===

def consolidate():
    """
    Консолидация памяти: decay + cluster (vec ANN) + merge + RAPTOR + cleanup.
    
    Архитектурное решение: вместо O(n²) brute-force сравнения всех пар,
    используем sqlite-vec ANN для поиска top-K соседей каждого memory.
    500 записей × 10 соседей × 0.24мс = 1.2с вместо 46 минут.
    """
    conn = get_db()

    # 1. DECAY — ослабляем неиспользуемые memories (>7 дней)
    conn.execute("""
        UPDATE memories
        SET strength = MAX(0.1, strength * 0.95)
        WHERE last_accessed_at < datetime('now', '-7 days')
    """)
    conn.commit()

    # 2. MERGE через vec ANN (критический фикс: было O(n²), стало O(n·k))
    memories = conn.execute("""
        SELECT id, content, type, importance, strength
        FROM memories
        ORDER BY created_at DESC
        LIMIT 500
    """).fetchall()

    merged_count = 0
    vec_available = False

    try:
        if _load_vec(conn):
            vec_available = True
    except Exception:
        pass

    if vec_available and memories:
        try:
            from brain import embed_text, vec_to_blob, embed_texts_batch

            # Batch-эмбеддинг всех memories за один forward pass
            contents = [m["content"] for m in memories]
            ids = [m["id"] for m in memories]
            id_set = set(ids)

            try:
                vecs = embed_texts_batch(contents)
            except Exception:
                vecs = [embed_text(c) for c in contents]

            # Для каждого memory ищем top-10 ближайших через sqlite-vec
            # и мержим дубли (sim > 0.85)
            processed_pairs = set()

            for i, (mid, vec, mem_row) in enumerate(zip(ids, vecs, memories)):
                vec_blob = vec_to_blob(vec)

                try:
                    neighbors = conn.execute("""
                        SELECT vm.rowid as nid, m.content, m.type, m.importance,
                               distance
                        FROM vec_memories vm
                        JOIN memories m ON vm.rowid = m.id
                        WHERE vm.embedding MATCH ? AND k = 11
                        ORDER BY distance
                    """, (vec_blob,)).fetchall()
                except Exception:
                    continue

                for nb in neighbors:
                    nid = nb["nid"]
                    if nid == mid or nid not in id_set:
                        continue

                    pair_key = (min(mid, nid), max(mid, nid))
                    if pair_key in processed_pairs:
                        continue
                    processed_pairs.add(pair_key)

                    sim = 1.0 - nb["distance"] if nb["distance"] is not None else 0
                    if sim <= 0.85:
                        continue

                    if nb["type"] != mem_row["type"]:
                        continue

                    # Определяем что оставить (higher importance wins)
                    if mem_row["importance"] >= nb["importance"]:
                        keep_id, drop_id = mid, nid
                    else:
                        keep_id, drop_id = nid, mid

                    # Переносим ассоциации с drop на keep
                    conn.execute("""
                        UPDATE associations
                        SET memory_id_from = ?
                        WHERE memory_id_from = ? AND memory_id_to != ?
                    """, (keep_id, drop_id, keep_id))
                    conn.execute("""
                        UPDATE associations
                        SET memory_id_to = ?
                        WHERE memory_id_to = ? AND memory_id_from != ?
                    """, (keep_id, drop_id, keep_id))

                    # Удаляем memory
                    conn.execute("DELETE FROM memories WHERE id = ?", (drop_id,))
                    # Удаляем вектор (критично: иначе мусор в vec-поиске)
                    conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (drop_id,))

                    # Убираем из id_set чтобы не мержить уже удалённое
                    id_set.discard(drop_id)
                    merged_count += 1

            conn.commit()

        except Exception as e:
            print(f"⚠️ Vec ANN merge error: {e}", file=sys.stderr)
            conn.rollback()

    elif memories:
        # Fallback: если vec недоступен, используем similarity pairwise
        # но с лимитом — только последние 50 записей (чтобы не зависнуть)
        try:
            from brain import similarity as _sim
            small_set = memories[:50]
            processed_pairs = set()

            for i, m1 in enumerate(small_set):
                for m2 in small_set[i+1:]:
                    pair_key = (min(m1["id"], m2["id"]), max(m1["id"], m2["id"]))
                    if pair_key in processed_pairs:
                        continue
                    processed_pairs.add(pair_key)

                    sim = _sim(m1["content"], m2["content"])
                    if sim > 0.85 and m1["type"] == m2["type"]:
                        if m1["importance"] >= m2["importance"]:
                            keep_id, drop_id = m1["id"], m2["id"]
                        else:
                            keep_id, drop_id = m2["id"], m1["id"]

                        conn.execute(
                            "UPDATE associations SET memory_id_from = ? WHERE memory_id_from = ? AND memory_id_to != ?",
                            (keep_id, drop_id, keep_id))
                        conn.execute(
                            "UPDATE associations SET memory_id_to = ? WHERE memory_id_to = ? AND memory_id_from != ?",
                            (keep_id, drop_id, keep_id))
                        conn.execute("DELETE FROM memories WHERE id = ?", (drop_id,))
                        merged_count += 1

            conn.commit()
        except ImportError:
            pass

    # 3. CLUSTER — ассоциации для свежих memories через vec ANN
    cluster_count = 0
    try:
        from brain import auto_associate as _auto_assoc
        recent = conn.execute("""
            SELECT id, content FROM memories
            WHERE created_at > datetime('now', '-30 days')
            ORDER BY created_at DESC LIMIT 100
        """).fetchall()
        for m in recent:
            assocs = _auto_assoc(m["id"], m["content"])
            if assocs:
                cluster_count += len(assocs)
    except ImportError:
        pass

    # 4. RAPTOR rebuild
    raptor_count = 0
    try:
        from brain import build_raptor
        raptor_count = build_raptor(level=0, batch_size=8)
        build_raptor(level=1, batch_size=5)
    except ImportError:
        pass

    # 5. Cleanup слабых memories + чистим vec (критично: без этого мусор в поиске)
    weak_ids = conn.execute("""
        SELECT id FROM memories
        WHERE strength < 0.15 AND importance < 0.3
    """).fetchall()

    deleted = 0
    for row in weak_ids:
        conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (row["id"],))
        conn.execute("DELETE FROM associations WHERE memory_id_from = ? OR memory_id_to = ?",
                    (row["id"], row["id"]))
        conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
        deleted += 1

    conn.commit()
    conn.close()

    print(f"✅ Консолидация завершена:")
    print(f"   Decay: применён")
    print(f"   Merge: {merged_count} дублей объединено (vec ANN)")
    print(f"   Cluster: {cluster_count} новых ассоциаций")
    print(f"   RAPTOR: {raptor_count} leaf-нод создано")
    print(f"   Cleanup: {deleted} слабых memories удалено (vec+assoc очищены)")


def prune_messages(older_than_days=180):
    """Архивация/удаление старых сообщений."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    deleted = conn.execute("DELETE FROM messages WHERE sent_at < ?", (cutoff,)).rowcount
    conn.commit()
    conn.close()
    print(f"✅ Архивировано {deleted} сообщений старше {older_than_days} дней")


def stats():
    """Статистика базы данных."""
    conn = get_db()
    tables = [
        "contacts", "companies", "chat_threads", "messages",
        "leads", "deals", "tasks", "agreements", "invoices", "payments",
        "memories", "associations", "raptor_nodes", "chaos_entries"
    ]
    print("📊 Lenochka Memory v2 — Статистика:")
    print(f"   БД: {DB_PATH}")
    print(f"   Размер: {DB_PATH.stat().st_size / 1024:.1f} KB" if DB_PATH.exists() else "   (не создана)")
    print()
    for t in tables:
        try:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            print(f"   {t:25s} → {row['cnt']} записей")
        except Exception:
            print(f"   {t:25s} → (ошибка)")

    # Векторные таблицы
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for vt in ["vec_memories", "vec_chaos"]:
            try:
                row = conn.execute(f"SELECT COUNT(*) as cnt FROM {vt}").fetchone()
                print(f"   {vt:25s} → {row['cnt']} векторов")
            except Exception:
                print(f"   {vt:25s} → (не создана)")
    except Exception:
        print("   sqlite-vec → недоступен")
    conn.close()


# === CLI PARSER ===

def main():
    if len(sys.argv) < 2:
        print(__doc__)
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
            print('Usage: mem.py store "content" [--type episodic] [--importance 0.7] ...')
            sys.exit(1)
        content = args[0]
        store(
            content=content,
            mem_type=get_arg("type", "episodic"),
            importance=float(get_arg("importance", "0.5")),
            contact_id=int(get_arg("contact-id")) if get_arg("contact-id") else None,
            chat_thread_id=int(get_arg("chat-thread-id")) if get_arg("chat-thread-id") else None,
            deal_id=int(get_arg("deal-id")) if get_arg("deal-id") else None,
            source_message_id=int(get_arg("source-message-id")) if get_arg("source-message-id") else None,
            tags=json.loads(get_arg("tags")) if get_arg("tags") else None,
        )

    elif cmd == "recall":
        if not args:
            print('Usage: mem.py recall "query" [--strategy hybrid] [--contact-id N] ...')
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

    elif cmd == "chaos-store":
        if not args:
            print('Usage: mem.py chaos-store "content" [--category decision] [--priority 0.8]')
            sys.exit(1)
        content = args[0]
        chaos_store(
            content=content,
            category=get_arg("category", "other"),
            priority=float(get_arg("priority", "0.5")),
            memory_id=int(get_arg("memory-id")) if get_arg("memory-id") else None,
            contact_id=int(get_arg("contact-id")) if get_arg("contact-id") else None,
        )

    elif cmd == "chaos-search":
        if not args:
            print('Usage: mem.py chaos-search "query" [--mode index|full] [--limit 10]')
            sys.exit(1)
        query = args[0]
        results = chaos_search(
            query=query,
            mode=get_arg("mode", "index"),
            limit=int(get_arg("limit", "10")),
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif cmd == "chaos-reindex":
        chaos_reindex()

    elif cmd == "crm":
        if len(args) < 1:
            print("Usage: mem.py crm <contact|deals|overdue-tasks|abandoned|leads|daily-summary> [options]")
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
                contact_id=int(sub_arg("contact-id")) if sub_arg("contact-id") else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2) if result else "Не найден")
        elif sub == "deals":
            cid = int(sub_arg("contact-id")) if sub_arg("contact-id") else None
            print(json.dumps(crm_deals(contact_id=cid), ensure_ascii=False, indent=2))
        elif sub == "overdue-tasks":
            print(json.dumps(crm_overdue_tasks(), ensure_ascii=False, indent=2))
        elif sub == "abandoned":
            print(json.dumps(crm_abandoned(hours=int(sub_arg("hours", "24"))), ensure_ascii=False, indent=2))
        elif sub == "leads":
            print(json.dumps(crm_leads(since=sub_arg("since")), ensure_ascii=False, indent=2))
        elif sub == "daily-summary":
            print(json.dumps(crm_daily_summary(date=sub_arg("date")), ensure_ascii=False, indent=2))
        else:
            print(f"Неизвестная CRM-команда: {sub}")
            sys.exit(1)

    elif cmd == "ingest":
        if not args:
            print('Usage: mem.py ingest "текст сообщения" [--contact-id N] [--chat-thread-id N] [--source-message-id N]')
            sys.exit(1)
        text = " ".join(args)
        contact_id = int(get_arg("contact-id")) if get_arg("contact-id") else None
        chat_thread_id = int(get_arg("chat-thread-id")) if get_arg("chat-thread-id") else None
        source_message_id = get_arg("source-message-id")
        result = ingest(text, contact_id=contact_id, chat_thread_id=chat_thread_id,
                       source_message_id=source_message_id)
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
            print(json.dumps({"label": label, "confidence": conf, "reasoning": reason},
                           ensure_ascii=False, indent=2))
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
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
