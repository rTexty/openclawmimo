#!/usr/bin/env python3
"""
Lenochka Brain v2 — Интеллектуальный модуль
LLM-классификация, семантические эмбеддинги, RAPTOR, контекст-пакеты.

Эмбеддинги: sentence-transformers (all-MiniLM-L6-v2, 384-dim) с fallback на char n-grams.
Хранение векторов: sqlite-vec для быстрого ANN-поиска.
"""

import json
import re
import os
import sys
import sqlite3
import struct
from datetime import datetime, timedelta
from pathlib import Path

# === Конфигурация ===
DB_PATH = Path(__file__).parent / "db" / "lenochka.db"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

# LLM config (OpenAI-совместимый API)
LLM_BASE_URL = os.environ.get("LENOCHKA_LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LENOCHKA_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LENOCHKA_LLM_MODEL", "mimo-v2-pro")


# =========================================================
# 1. ЭМБЕДДИНГИ
# =========================================================

_embed_model = None

def _get_embed_model():
    """Ленивая загрузка sentence-transformers."""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer('all-MiniLM-L6-v2')
        return _embed_model
    except Exception as e:
        print(f"⚠️ sentence-transformers недоступен ({e}), использую char n-gram fallback", file=sys.stderr)
        return None


def embed_text(text):
    """
    Получить эмбеддинг текста.
    Возвращает list[float] длиной EMBEDDING_DIM.
    """
    model = _get_embed_model()
    if model is not None:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    # Fallback: char 3-gram TF, дополненный нулями до EMBEDDING_DIM
    return _embed_fallback(text)


def embed_texts_batch(texts):
    """
    Batch-эмбеддинг списка текстов.
    ~7x быстрее чем embed_text() в цикле для N>5.
    Возвращает list[list[float]].
    """
    if not texts:
        return []
    model = _get_embed_model()
    if model is not None:
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]
    # Fallback: поштучно
    return [_embed_fallback(t) for t in texts]


def _embed_fallback(text):
    """Char 3-gram TF embedding с fallback на EMBEDDING_DIM."""
    text = text.lower().strip()
    grams = {}
    for i in range(len(text) - 2):
        g = text[i:i+3]
        grams[g] = grams.get(g, 0) + 1
    if not grams:
        return [0.0] * EMBEDDING_DIM
    # Hash-проекция в EMBEDDING_DIM
    vec = [0.0] * EMBEDDING_DIM
    for gram, count in grams.items():
        h = hash(gram) % EMBEDDING_DIM
        vec[h] += count
    # Нормализация
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def vec_to_blob(vec):
    """Конвертировать list[float] в bytes для sqlite-vec."""
    return struct.pack(f'{len(vec)}f', *vec)


def blob_to_vec(blob):
    """Конвертировать bytes обратно в list[float]."""
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(vec1, vec2):
    """Косинусное сходство между двумя векторами."""
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def similarity(text1, text2):
    """
    Сравнение двух текстов: cosine similarity по эмбеддингам.
    """
    emb1 = embed_text(text1)
    emb2 = embed_text(text2)
    return cosine_similarity(emb1, emb2)


# =========================================================
# 2. LLM ИНТЕГРАЦИЯ
# =========================================================

def _call_llm(system_prompt, user_prompt, temperature=0.0, max_tokens=2048):
    """Вызов LLM через OpenAI-совместимый API с retry/backoff."""
    import time

    if not LLM_BASE_URL or not LLM_API_KEY:
        return None

    max_retries = 3
    for attempt in range(max_retries):
        try:
            import requests
            url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            }
            payload = {
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt  # 1s, 2s, 4s
                print(f"LLM retry {attempt + 1}/{max_retries} after {delay}s: {e}", file=sys.stderr)
                time.sleep(delay)
            else:
                print(f"LLM error (all {max_retries} attempts failed): {e}", file=sys.stderr)
                return None


# =========================================================
# 3. КЛАССИФИКАЦИЯ СООБЩЕНИЙ
# =========================================================

CLASSIFY_SYSTEM = """Ты — классификатор Telegram-сообщений для CRM-системы.
Классифицируй сообщение в одну из категорий:
- noise: мусор, спам, боты, реклама
- chit-chat: личное общение, приветствия, эмодзи, не по делу
- business-small: короткие рабочие сообщения без конкретных задач
- task: явная или подразумеваемая задача, просьба, TODO
- decision: принятое решение, договорённость, согласование
- lead-signal: интерес клиента, запрос цены/условий, новый контакт
- risk: жалоба, угроза срыва, просрочка, конфликт
- other: не удалось классифицировать

Отвечай ТОЛЬКО JSON без форматирования:
{"label": "<категория>", "confidence": <0.0-1.0>, "reasoning": "<краткое пояснение>"}"""


def classify_message(text, chat_context=None):
    """Классифицировать сообщение. Возвращает (label, confidence, reasoning)."""
    context_str = f"\nКонтекст чата: {chat_context}" if chat_context else ""
    result = _call_llm(CLASSIFY_SYSTEM, f"Сообщение:\n{text}{context_str}")

    if result:
        try:
            json_match = re.search(r'\{[^}]+\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return (
                    data.get("label", "other"),
                    data.get("confidence", 0.5),
                    data.get("reasoning", ""),
                )
        except (json.JSONDecodeError, KeyError):
            pass

    return _classify_heuristic(text)


def _classify_heuristic(text):
    """Эвристическая классификация без LLM."""
    text_lower = text.lower()

    # Noise
    noise_words = ["подписывайтесь", "реклама", "скидка", "акция", "бесплатно", "промокод"]
    if any(w in text_lower for w in noise_words):
        return ("noise", 0.6, "heuristic: noise keywords")

    # Risk
    risk_words = ["задержка", "просроч", "жалоб", "не могу дозвониться", "где деньги", "конфликт"]
    if any(w in text_lower for w in risk_words):
        return ("risk", 0.7, "heuristic: risk keywords")

    # Lead signal
    lead_words = ["сколько стоит", "какая цена", "хочу заказать", "интересует",
                  "можете сделать", "кп", "коммерческое", "предоплат", "соглас"]
    if any(w in text_lower for w in lead_words):
        return ("lead-signal", 0.7, "heuristic: lead keywords")

    # Decision
    decision_words = ["согласен", "договорились", "подтверждаю", "одобряю", "решили"]
    if any(w in text_lower for w in decision_words):
        return ("decision", 0.7, "heuristic: decision keywords")

    # Task
    task_words = ["сделай", "сделать", "нужно", "надо", "пришли", "отправь", "подготовь", "созвон"]
    if any(w in text_lower for w in task_words):
        return ("task", 0.6, "heuristic: task keywords")

    # Short messages usually chit-chat
    if len(text) < 20:
        return ("chit-chat", 0.5, "heuristic: short message")

    return ("business-small", 0.4, "heuristic: default")


# =========================================================
# 4. ИЗВЛЕЧЕНИЕ СУЩНОСТЕЙ
# =========================================================

EXTRACT_SYSTEM = """Ты — извлекатель сущностей из Telegram-сообщений для CRM.
Извлекай только то, что ЯВНО указано или сильно подразумевается.

Возвращай ТОЛЬКО JSON без форматирования:
{
  "contact": {"name": "...", "tg_username": "...", "company": "..."},
  "lead": {"source": "...", "amount": 0, "probability": 0.0-1.0},
  "deal": {"stage": "...", "amount": 0},
  "task": {"description": "...", "due_date": "YYYY-MM-DD или null", "priority": "low|normal|high|urgent"},
  "agreement": {"summary": "...", "amount": 0, "due_date": "YYYY-MM-DD или null"},
  "amounts": [0],
  "dates": ["YYYY-MM-DD"],
  "products": ["..."],
  "risk_type": "..." или null
}

Если сущность не найдена, ставь null. Не выдумывай данные."""


def extract_entities(text, label=None, chat_context=None):
    """Извлечь сущности из текста сообщения."""
    context_str = f"\nКлассификация: {label}" if label else ""
    context_str += f"\nКонтекст чата: {chat_context}" if chat_context else ""

    result = _call_llm(EXTRACT_SYSTEM, f"Сообщение:\n{text}{context_str}")

    if result:
        try:
            json_match = re.search(r'\{[^}]+\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, KeyError):
            pass

    return _extract_heuristic(text)


def _extract_heuristic(text):
    """Простое извлечение без LLM."""
    entities = {
        "contact": None, "lead": None, "deal": None, "task": None,
        "agreement": None, "amounts": [], "dates": [], "products": [],
        "risk_type": None,
    }

    # Суммы: "150000 рублей", "150 000 руб", "150к", "150 тыс"
    amounts_raw = re.findall(r'(\d[\d\s,.]*)\s*(тыс|к)?\s*(?:руб|₽|рублей)?', text, re.IGNORECASE)
    for num_str, multiplier, *_ in amounts_raw:
        if not num_str.strip():
            continue
        cleaned = num_str.replace(" ", "").replace(",", ".")
        try:
            val = float(cleaned)
            if multiplier and multiplier.lower() in ("тыс", "к"):
                val *= 1000
            if val > 0 and val not in entities["amounts"]:
                entities["amounts"].append(val)
        except ValueError:
            pass

    # Даты
    dates = re.findall(r'(\d{1,2}[./]\d{1,2}[./]?\d{0,4})', text)
    for d in dates:
        parts = re.split(r'[./]', d)
        if len(parts) >= 2:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) > 2 and parts[2] else datetime.now().year
            if 1 <= day <= 31 and 1 <= month <= 12:
                entities["dates"].append(f"{year}-{month:02d}-{day:02d}")

    # @username
    usernames = re.findall(r'@(\w+)', text)
    if usernames:
        entities["contact"] = {"name": None, "tg_username": usernames[0], "company": None}

    return entities


# =========================================================
# 5. АВТОСВЯЗЫВАНИЕ MEMORIES
# =========================================================

def _get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def auto_associate(memory_id, content, threshold=0.35):
    """
    Найти похожие memories и создать ассоциации.
    Использует векторные эмбеддинги через sqlite-vec если доступны,
    иначе fallback на прямой cosine по содержимому.
    """
    conn = _get_db()
    associations = []

    try:
        try:
            conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            # Векторный поиск через sqlite-vec
            vec = embed_text(content)
            vec_blob = vec_to_blob(vec)

            rows = conn.execute("""
                SELECT vm.rowid as memory_id, m.content, m.type, m.importance,
                       distance
                FROM vec_memories vm
                JOIN memories m ON vm.rowid = m.id
                WHERE vm.embedding MATCH ? AND k = 20 AND vm.rowid != ?
                ORDER BY distance
            """, (vec_blob, memory_id)).fetchall()

            for row in rows:
                sim = 1.0 - row["distance"] if "distance" in row.keys() else 0
                if sim < threshold:
                    continue
                rel_type = "supports" if sim > 0.8 else "related"

                existing = conn.execute("""
                    SELECT id FROM associations
                    WHERE (memory_id_from = ? AND memory_id_to = ?)
                       OR (memory_id_from = ? AND memory_id_to = ?)
                """, (memory_id, row["memory_id"], row["memory_id"], memory_id)).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO associations (memory_id_from, memory_id_to, relation_type, weight)
                        VALUES (?, ?, ?, ?)
                    """, (memory_id, row["memory_id"], rel_type, round(sim, 3)))
                    associations.append({
                        "target_id": row["memory_id"],
                        "relation": rel_type,
                        "weight": round(sim, 3),
                    })
        except Exception:
            # Fallback: прямое сравнение cosine similarity
            emb_source = embed_text(content)
            rows = conn.execute("""
                SELECT id, content FROM memories
                WHERE id != ?
                ORDER BY created_at DESC
                LIMIT 200
            """, (memory_id,)).fetchall()

            for row in rows:
                emb_target = embed_text(row["content"])
                sim = cosine_similarity(emb_source, emb_target)

                if sim >= threshold:
                    rel_type = "supports" if sim > 0.8 else "related"

                    existing = conn.execute("""
                        SELECT id FROM associations
                        WHERE (memory_id_from = ? AND memory_id_to = ?)
                           OR (memory_id_from = ? AND memory_id_to = ?)
                    """, (memory_id, row["id"], row["id"], memory_id)).fetchone()

                    if not existing:
                        conn.execute("""
                            INSERT INTO associations (memory_id_from, memory_id_to, relation_type, weight)
                            VALUES (?, ?, ?, ?)
                        """, (memory_id, row["id"], rel_type, round(sim, 3)))
                        associations.append({
                            "target_id": row["id"],
                            "relation": rel_type,
                            "weight": round(sim, 3),
                        })

        conn.commit()
    finally:
        conn.close()

    return associations


# =========================================================
# 6. RAPTOR — ИЕРАРХИЧЕСКАЯ СУММАРИЗАЦИЯ
# =========================================================

def build_raptor(level=0, batch_size=10):
    """Построить RAPTOR-дерево."""
    conn = _get_db()

    if level == 0:
        memories = conn.execute("""
            SELECT id, content, type, importance
            FROM memories
            WHERE id NOT IN (
                SELECT DISTINCT value FROM raptor_nodes, json_each(raptor_nodes.memory_ids)
                WHERE raptor_nodes.memory_ids IS NOT NULL
            )
            ORDER BY importance DESC, created_at DESC
            LIMIT 100
        """).fetchall()

        if not memories:
            conn.close()
            return 0

        created = 0
        for i in range(0, len(memories), batch_size):
            batch = memories[i:i+batch_size]
            batch_ids = [m["id"] for m in batch]
            batch_contents = [m["content"] for m in batch]

            summary = _summarize_batch(batch_contents)
            conn.execute("""
                INSERT INTO raptor_nodes (level, summary, memory_ids)
                VALUES (?, ?, ?)
            """, (0, summary, json.dumps(batch_ids)))
            created += 1

        conn.commit()
        conn.close()
        return created
    else:
        children = conn.execute("""
            SELECT id, summary, memory_ids FROM raptor_nodes
            WHERE level = ?
            ORDER BY id
        """, (level - 1,)).fetchall()

        if len(children) < 2:
            conn.close()
            return 0

        created = 0
        for i in range(0, len(children), batch_size):
            batch = children[i:i+batch_size]
            child_ids = [c["id"] for c in batch]
            child_summaries = [c["summary"] for c in batch]

            all_memory_ids = []
            for c in batch:
                if c["memory_ids"]:
                    try:
                        all_memory_ids.extend(json.loads(c["memory_ids"]))
                    except (json.JSONDecodeError, TypeError):
                        pass

            summary = _summarize_batch(child_summaries)
            conn.execute("""
                INSERT INTO raptor_nodes (level, summary, memory_ids)
                VALUES (?, ?, ?)
            """, (level, summary, json.dumps(all_memory_ids) if all_memory_ids else None))

            parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for cid in child_ids:
                conn.execute("UPDATE raptor_nodes SET parent_id = ? WHERE id = ?",
                           (parent_id, cid))
            created += 1

        conn.commit()
        conn.close()
        return created


def _summarize_batch(texts):
    """Суммаризировать batch текстов."""
    combined = "\n".join(f"- {t}" for t in texts[:10])

    result = _call_llm(
        "Ты — система суммаризации. Сверни список фактов в 1-2 кратких предложения.",
        f"Факты:\n{combined}\n\nКраткая суммаризация:",
        max_tokens=200,
    )

    if result:
        return result
    return "; ".join(t[:80] for t in texts[:3])


# =========================================================
# 7. CONTEXT-PACKET (dynamic-context)
# =========================================================

def build_context_packet(query, contact_id=None, deal_id=None,
                         chat_thread_id=None, intent="search", limit=15):
    """
    Собрать контекст-пакет для LLM.
    Режимы: core | search | recall
    """
    conn = _get_db()
    packet = {
        "facts": [], "episodes": [], "related": [], "notes": [],
        "intent": intent,
    }

    # 1. CRM-данные
    if contact_id:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ?",
                              (contact_id,)).fetchone()
        if contact:
            packet["facts"].append({"type": "contact", "data": dict(contact)})

        deals = conn.execute("""
            SELECT * FROM deals WHERE contact_id = ?
            ORDER BY updated_at DESC LIMIT 5
        """, (contact_id,)).fetchall()
        for d in deals:
            packet["facts"].append({"type": "deal", "data": dict(d)})

        tasks = conn.execute("""
            SELECT * FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND status NOT IN ('done', 'cancelled')
            ORDER BY due_at ASC LIMIT 5
        """, (contact_id,)).fetchall()
        for t in tasks:
            packet["facts"].append({"type": "task", "data": dict(t)})

    if deal_id:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?",
                           (deal_id,)).fetchone()
        if deal:
            packet["facts"].append({"type": "deal", "data": dict(deal)})

        agreements = conn.execute("""
            SELECT * FROM agreements WHERE deal_id = ?
            ORDER BY created_at DESC LIMIT 5
        """, (deal_id,)).fetchall()
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
        mem_rows = conn.execute("""
            SELECT vm.rowid as id, m.content, m.type, m.importance, m.created_at,
                   distance
            FROM vec_memories vm
            JOIN memories m ON vm.rowid = m.id
            WHERE vm.embedding MATCH ? AND k = ?
            ORDER BY distance
        """, (vec_blob, limit)).fetchall()

        for m in mem_rows:
            packet["episodes"].append({
                "id": m["id"], "content": m["content"], "type": m["type"],
                "importance": m["importance"], "created_at": m["created_at"],
                "score": round(1.0 - m["distance"], 3) if "distance" in m.keys() else 0,
            })
    except Exception:
        # Fallback: FTS trigram поиск
        try:
            mem_rows = conn.execute("""
                SELECT m.id, m.content, m.type, m.importance, m.created_at, rank
                FROM memories_fts
                JOIN memories m ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
            for m in mem_rows:
                packet["episodes"].append({
                    "id": m["id"], "content": m["content"], "type": m["type"],
                    "importance": m["importance"], "created_at": m["created_at"],
                    "score": abs(m["rank"]) if m["rank"] else 0,
                })
        except Exception:
            # Last fallback: LIKE
            keywords = [w for w in re.findall(r'\w+', query.lower()) if len(w) > 2]
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
            mem_rows = conn.execute(f"""
                SELECT id, content, type, importance, created_at
                FROM memories WHERE {like_conds}
                ORDER BY importance DESC, created_at DESC LIMIT ?
            """, params).fetchall()
            for m in mem_rows:
                packet["episodes"].append({
                    "id": m["id"], "content": m["content"], "type": m["type"],
                    "importance": m["importance"], "created_at": m["created_at"],
                })

    # 3. CHAOS — FTS trigram поиск
    try:
        chaos_rows = conn.execute("""
            SELECT ce.id, ce.content, ce.category, ce.priority, rank
            FROM chaos_fts
            JOIN chaos_entries ce ON chaos_fts.rowid = ce.id
            WHERE chaos_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        for c in chaos_rows:
            packet["related"].append({
                "id": c["id"], "content": c["content"],
                "category": c["category"], "priority": c["priority"],
                "score": abs(c["rank"]) if c["rank"] else 0,
            })
    except Exception:
        pass

    # 4. Associations (1 hop) от top memories
    if packet["episodes"]:
        top_mem_ids = [e["id"] for e in packet["episodes"][:5]]
        placeholders = ",".join("?" * len(top_mem_ids))
        try:
            assoc_rows = conn.execute(f"""
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
            """, top_mem_ids + top_mem_ids + top_mem_ids + top_mem_ids).fetchall()

            for a in assoc_rows:
                packet["notes"].append({
                    "type": "association",
                    "content": a["content"],
                    "relation": a["relation_type"],
                    "weight": a["weight"],
                })
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
            packet["facts"].append({
                "type": f"memory_{m['type']}",
                "data": {"content": m["content"], "importance": m["importance"]},
            })

    conn.close()
    return packet


# =========================================================
# 8. ДАЙДЖЕСТЫ
# =========================================================

def generate_daily_digest(date=None):
    """Сгенерировать утренний дайджест."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    conn = _get_db()
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"
    sections = []

    # 1. Новые лиды
    leads = conn.execute("""
        SELECT l.*, c.name as contact_name FROM leads l
        JOIN contacts c ON l.contact_id = c.id
        WHERE l.created_at BETWEEN ? AND ?
    """, (start, end)).fetchall()
    if leads:
        lines = [f"• {l['contact_name']}: {l.get('source', '?')}, {l.get('amount', '?')} руб, статус: {l['status']}"
                 for l in leads]
        sections.append(f"🔥 Новые лиды ({len(leads)}):\n" + "\n".join(lines))

    # 2. Просроченные задачи
    overdue = conn.execute("SELECT * FROM v_overdue_tasks").fetchall()
    if overdue:
        lines = [f"• {t['description'][:60]} (просрочено)" for t in overdue]
        sections.append(f"⚠️ Просроченные задачи ({len(overdue)}):\n" + "\n".join(lines))

    # 3. Брошенные диалоги
    abandoned = conn.execute("""
        SELECT ct.title, c.name, MAX(m.sent_at) as last_at,
               (julianday('now') - julianday(MAX(m.sent_at))) * 24 as hours
        FROM chat_threads ct
        JOIN messages m ON m.chat_thread_id = ct.id
        LEFT JOIN contacts c ON ct.contact_id = c.id
        WHERE m.from_user_id != 'self'
        GROUP BY ct.id
        HAVING hours > 24
        ORDER BY hours DESC LIMIT 10
    """).fetchall()
    if abandoned:
        lines = [f"• {a['name'] or a['title']}: {int(a['hours'])}ч без ответа" for a in abandoned]
        sections.append(f"👻 Брошенные диалоги ({len(abandoned)}):\n" + "\n".join(lines))

    # 4. Ключевые события дня
    events = conn.execute("""
        SELECT content, type, importance FROM memories
        WHERE created_at BETWEEN ? AND ? AND importance >= 0.7
        ORDER BY importance DESC LIMIT 5
    """, (start, end)).fetchall()
    if events:
        lines = [f"• [{e['type']}] {e['content'][:80]}" for e in events]
        sections.append(f"📌 Ключевые события:\n" + "\n".join(lines))

    conn.close()

    if not sections:
        return f"📅 Дайджест за {date}\n\nТихий день — ничего важного."
    return f"📅 Дайджест за {date}\n\n" + "\n\n".join(sections)


def generate_weekly_digest(weeks_back=0):
    """Сгенерировать недельный дайджест."""
    now = datetime.now()
    end_date = now - timedelta(weeks=weeks_back)
    start_date = end_date - timedelta(days=7)
    start = start_date.strftime("%Y-%m-%d")
    end = end_date.strftime("%Y-%m-%d")

    conn = _get_db()
    stats = {}
    for key, query in [
        ("messages", "SELECT COUNT(*) as c FROM messages WHERE sent_at BETWEEN ? AND ?"),
        ("new_leads", "SELECT COUNT(*) as c FROM leads WHERE created_at BETWEEN ? AND ?"),
        ("new_tasks", "SELECT COUNT(*) as c FROM tasks WHERE created_at BETWEEN ? AND ?"),
        ("completed_tasks", "SELECT COUNT(*) as c FROM tasks WHERE status='done' AND updated_at BETWEEN ? AND ?"),
        ("memories", "SELECT COUNT(*) as c FROM memories WHERE created_at BETWEEN ? AND ?"),
    ]:
        stats[key] = conn.execute(query, (f"{start} 00:00:00", f"{end} 23:59:59")).fetchone()["c"]
    conn.close()

    conv = round(stats["completed_tasks"] / max(stats["new_tasks"], 1) * 100)
    return f"""📊 Недельный дайджест ({start} — {end})

📨 Сообщений: {stats['messages']}
🔥 Новых лидов: {stats['new_leads']}
📋 Новых задач: {stats['new_tasks']}
✅ Завершённых задач: {stats['completed_tasks']}
🧠 Записей в памяти: {stats['memories']}

Конверсия задач: {stats['completed_tasks']}/{stats['new_tasks']} ({conv}%)"""


# =========================================================
# CLI
# =========================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 brain.py <classify|extract|similarity|associate|raptor|context|digest|weekly> [args]")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "classify":
        text = " ".join(args)
        label, conf, reason = classify_message(text)
        print(json.dumps({"label": label, "confidence": conf, "reasoning": reason},
                        ensure_ascii=False, indent=2))

    elif cmd == "extract":
        text = " ".join(args)
        entities = extract_entities(text)
        print(json.dumps(entities, ensure_ascii=False, indent=2))

    elif cmd == "similarity":
        if len(args) < 2:
            print("Usage: brain.py similarity 'text1' 'text2'")
        else:
            sim = similarity(args[0], args[1])
            print(f"Similarity: {sim:.3f}")

    elif cmd == "associate":
        mid = int(args[0]) if args else 1
        conn = _get_db()
        row = conn.execute("SELECT content FROM memories WHERE id = ?", (mid,)).fetchone()
        conn.close()
        if row:
            assocs = auto_associate(mid, row["content"])
            print(json.dumps(assocs, ensure_ascii=False, indent=2))
        else:
            print(f"Memory #{mid} not found")

    elif cmd == "raptor":
        level = int(args[0]) if args else 0
        count = build_raptor(level=level)
        print(f"Created {count} RAPTOR nodes at level {level}")

    elif cmd == "context":
        query = " ".join(args) if args else "общий контекст"
        packet = build_context_packet(query, intent="search")
        print(json.dumps(packet, ensure_ascii=False, indent=2))

    elif cmd == "digest":
        date = args[0] if args else None
        print(generate_daily_digest(date))

    elif cmd == "weekly":
        print(generate_weekly_digest())

    elif cmd == "embed":
        text = " ".join(args)
        emb = embed_text(text)
        print(f"Embedding dim: {len(emb)}")
        print(f"First 5: {emb[:5]}")

    else:
        print(f"Неизвестная команда: {cmd}")
        sys.exit(1)
