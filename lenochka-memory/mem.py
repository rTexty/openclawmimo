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


def init():
    """Инициализация базы данных."""
    if DB_PATH.exists():
        print(f"БД уже существует: {DB_PATH}")
        return

    conn = get_db()
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)

    # Создать векторные таблицы через sqlite-vec
    if _load_vec(conn):
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[{EMBEDDING_DIM}])")
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chaos USING vec0(embedding float[{EMBEDDING_DIM}])")
        print("✅ Векторные таблицы (sqlite-vec) созданы")
    else:
        print("⚠️ sqlite-vec недоступен — векторный поиск отключён")

    conn.commit()
    conn.close()
    print(f"✅ БД создана: {DB_PATH}")


# === STORE (Capture) ===

def store(content, mem_type="episodic", importance=0.5, contact_id=None,
          chat_thread_id=None, deal_id=None, source_message_id=None, tags=None,
          auto_associate=True):
    """Записать memory в Agent Memory + векторный эмбеддинг."""
    conn = get_db()
    tags_json = json.dumps(tags) if tags else None
    conn.execute("""
        INSERT INTO memories (content, type, importance, strength, contact_id,
                             chat_thread_id, deal_id, source_message_id, tags)
        VALUES (?, ?, ?, 1.0, ?, ?, ?, ?, ?)
    """, (content, mem_type, importance, contact_id, chat_thread_id,
          deal_id, source_message_id, tags_json))
    conn.commit()
    mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Векторный эмбеддинг → vec_memories
    try:
        from brain import embed_text, vec_to_blob
        vec = embed_text(content)
        vec_blob = vec_to_blob(vec)
        if _load_vec(conn):
            conn.execute(
                "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                (mid, vec_blob)
            )
            conn.commit()
    except Exception as e:
        print(f"⚠️ Не удалось записать вектор: {e}", file=sys.stderr)

    conn.close()
    print(f"✅ Memory #{mid} записан [{mem_type}] importance={importance}")

    # Автосвязывание
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
        except Exception:
            pass

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

    conn.close()

    # Dedup + sort
    seen = set()
    unique = []
    for r in results:
        key = (r.get("source", ""), r["id"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    return unique[:limit]


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
    """Записать entry в CHAOS + векторный эмбеддинг."""
    conn = get_db()
    conn.execute("""
        INSERT INTO chaos_entries (content, category, priority, memory_id, contact_id)
        VALUES (?, ?, ?, ?, ?)
    """, (content, category, priority, memory_id, contact_id))
    conn.commit()
    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Векторный эмбеддинг → vec_chaos
    try:
        from brain import embed_text, vec_to_blob
        vec = embed_text(content)
        vec_blob = vec_to_blob(vec)
        if _load_vec(conn):
            conn.execute(
                "INSERT INTO vec_chaos(rowid, embedding) VALUES (?, ?)",
                (eid, vec_blob)
            )
            conn.commit()
    except Exception:
        pass

    conn.close()
    print(f"✅ CHAOS #{eid} записан [{category}] priority={priority}")
    return eid


def chaos_search(query, mode="index", limit=10):
    """Поиск в CHAOS."""
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
            for r in rows:
                conn.execute("""
                    UPDATE chaos_entries
                    SET access_count = access_count + 1,
                        last_accessed_at = datetime('now')
                    WHERE id = ?
                """, (r["id"],))
                results.append(dict(r))
            conn.commit()
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
            for r in rows:
                conn.execute("""
                    UPDATE chaos_entries
                    SET access_count = access_count + 1,
                        last_accessed_at = datetime('now')
                    WHERE id = ?
                """, (r["id"],))
                results.append(dict(r))
            conn.commit()

    elif mode == "full":
        rows = conn.execute("""
            SELECT *, (priority + 0.1 * access_count) as heat_score
            FROM chaos_entries
            WHERE content LIKE ?
            ORDER BY heat_score DESC
            LIMIT ?
        """, (f"%{query}%", limit)).fetchall()
        for r in rows:
            conn.execute("""
                UPDATE chaos_entries
                SET access_count = access_count + 1,
                    last_accessed_at = datetime('now')
                WHERE id = ?
            """, (r["id"],))
            results.append(dict(r))
        conn.commit()

    conn.close()
    return results


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

def ingest(text, contact_id=None, chat_thread_id=None):
    """Полный пайплайн: classify → extract → store → chaos."""
    try:
        from brain import classify_message, extract_entities
    except ImportError:
        print("Модуль brain.py не найден")
        return None

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
            auto_associate=True,
        )

        # 4. CHAOS store
        chaos_store(
            content=text[:200],
            category=label if label in ("decision", "risk") else "event",
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
    """Консолидация памяти: decay + cluster + merge + RAPTOR."""
    conn = get_db()

    # 1. DECAY
    conn.execute("""
        UPDATE memories
        SET strength = MAX(0.1, strength * 0.95)
        WHERE last_accessed_at < datetime('now', '-7 days')
    """)
    conn.commit()

    # 2. MERGE дублей
    memories = conn.execute("""
        SELECT id, content, type, importance, strength, contact_id
        FROM memories
        ORDER BY created_at DESC
        LIMIT 500
    """).fetchall()

    merged_count = 0
    try:
        from brain import similarity as _sim

        seen_pairs = set()
        for i, m1 in enumerate(memories):
            for m2 in memories[i+1:]:
                pair_key = (min(m1["id"], m2["id"]), max(m1["id"], m2["id"]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                sim = _sim(m1["content"], m2["content"])
                if sim > 0.85 and m1["type"] == m2["type"]:
                    if m1["importance"] >= m2["importance"]:
                        keep_id, drop_id = m1["id"], m2["id"]
                    else:
                        keep_id, drop_id = m2["id"], m1["id"]

                    conn.execute("UPDATE associations SET memory_id_from = ? WHERE memory_id_from = ? AND memory_id_to != ?",
                               (keep_id, drop_id, keep_id))
                    conn.execute("UPDATE associations SET memory_id_to = ? WHERE memory_id_to = ? AND memory_id_from != ?",
                               (keep_id, drop_id, keep_id))
                    conn.execute("DELETE FROM memories WHERE id = ?", (drop_id,))
                    merged_count += 1
    except ImportError:
        pass

    conn.commit()

    # 3. CLUSTER — ассоциации для свежих memories
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

    # 5. Cleanup слабых memories
    deleted = conn.execute("""
        DELETE FROM memories
        WHERE strength < 0.15 AND importance < 0.3
    """).rowcount
    conn.commit()
    conn.close()

    print(f"✅ Консолидация завершена:")
    print(f"   Decay: применён")
    print(f"   Merge: {merged_count} дублей объединено")
    print(f"   Cluster: {cluster_count} новых ассоциаций")
    print(f"   RAPTOR: {raptor_count} leaf-нод создано")
    print(f"   Cleanup: {deleted} слабых memories удалено")


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
            print('Usage: mem.py ingest "текст сообщения" [--contact-id N] [--chat-thread-id N]')
            sys.exit(1)
        text = " ".join(args)
        contact_id = int(get_arg("contact-id")) if get_arg("contact-id") else None
        chat_thread_id = int(get_arg("chat-thread-id")) if get_arg("chat-thread-id") else None
        result = ingest(text, contact_id=contact_id, chat_thread_id=chat_thread_id)
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
