import sys

from mem._db import get_db, _load_vec
from mem.entity_expand import _expand_entity_context, _format_entity_context


def _rrf_rank(results: list[dict], limit: int, k: int = 60) -> list[dict]:
    """
    Reciprocal Rank Fusion — нормализует скоры из разных источников.

    Проблема: vector scores 0-1, BM25 ranks -10..0, keyword scores arbitrary.
    RRF решает: ранжирует по позиции в каждом списке, не по абсолютному скору.

    Formula: RRF_score(item) = sum(1 / (k + rank_in_source + 1))
    k=60 — стандартная константа (устойчива к выбросам).
    """
    # Группируем по source
    by_source: dict[str, list[dict]] = {}
    for r in results:
        src = r.get("source", "unknown")
        by_source.setdefault(src, []).append(r)

    # Сортируем каждый source по его native score (descending)
    for src, items in by_source.items():
        items.sort(key=lambda x: x.get("score", 0), reverse=True)

    # RRF: для каждого item считаем сумму 1/(k+rank+1) по всем source
    rrf_scores: dict[tuple, float] = {}
    item_map: dict[tuple, dict] = {}

    for src, items in by_source.items():
        for rank, item in enumerate(items):
            key = (src, item["id"])
            item_map[key] = item
            # Также ищем этот item в других sources по id (cross-source match)
            for other_src, other_items in by_source.items():
                if other_src == src:
                    continue
                for other_rank, other_item in enumerate(other_items):
                    if other_item["id"] == item["id"]:
                        # Same item in different source — combine ranks
                        key = _item_key(item)
                        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (k + rank + 1)
                        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (
                            k + other_rank + 1
                        )
                        item_map[key] = item
                        break
                else:
                    continue
                break
            else:
                # Item only in this source
                key = _item_key(item)
                rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (k + rank + 1)
                item_map[key] = item

    # Sort by RRF score
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)

    result = []
    seen = set()
    for key in sorted_keys:
        item = item_map[key]
        # Dedup: same memory id across agent_memory/agent_memory_fts/vector = one item
        # CHAOS items are separate data store — keep by (source, id)
        src = item.get("source", "")
        if src in ("agent_memory", "agent_memory_fts", "vector"):
            dedup_key = ("memory", item["id"])
        else:
            dedup_key = (src, item["id"])
        if dedup_key not in seen:
            seen.add(dedup_key)
            item["score"] = round(rrf_scores[key], 6)
            item["rrf_applied"] = True
            result.append(item)
        if len(result) >= limit:
            break

    return result


def _item_key(item: dict) -> tuple:
    """
    Уникальный ключ для item (для RRF dedup).
    Agent memory sources (vector, fts, like) ссылаются на одну таблицу —
    группируем по id без source.
    """
    src = item.get("source", "")
    if src in ("agent_memory", "agent_memory_fts", "vector"):
        return ("memory", item["id"])
    return (src, item["id"])


def recall(
    query, strategy="hybrid", contact_id=None, deal_id=None, mem_type=None, limit=20
):
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
                rows = conn.execute(
                    """
                    SELECT vm.rowid as id, m.content, m.type, m.importance,
                           m.strength, m.contact_id, m.created_at, distance
                    FROM vec_memories vm
                    JOIN memories m ON vm.rowid = m.id
                    WHERE vm.embedding MATCH ? AND k = ?
                    ORDER BY distance
                """,
                    (vec_blob, limit),
                ).fetchall()
                for r in rows:
                    score = 1.0 - r["distance"] if r["distance"] is not None else 0
                    results.append(
                        {
                            "id": r["id"],
                            "content": r["content"],
                            "type": r["type"],
                            "importance": r["importance"],
                            "strength": r["strength"],
                            "contact_id": r["contact_id"],
                            "created_at": r["created_at"],
                            "score": round(score, 4),
                            "source": "vector",
                        }
                    )
        except Exception as e:
            pass

    # 2. BM25 по memories FTS (trigram — лучше для кириллицы чем LIKE)
    if strategy in ("hybrid", "bm25"):
        try:
            fts_query = f'"{query}"' if " " in query else query
            rows = conn.execute(
                """
                SELECT m.id, m.content, m.type, m.category, m.importance, m.strength,
                       m.contact_id, m.created_at, rank, 'agent_memory_fts' as source
                FROM memories_fts
                JOIN memories m ON memories_fts.rowid = m.id
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """,
                (fts_query, limit),
            ).fetchall()
            for r in rows:
                results.append(
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "type": r["type"],
                        "importance": r["importance"],
                        "strength": r["strength"],
                        "contact_id": r["contact_id"],
                        "created_at": r["created_at"],
                        "score": abs(r["rank"]) if r["rank"] else 0,
                        "source": "agent_memory_fts",
                    }
                )
        except Exception as e:
            print(f"⚠️ FTS memories search error: {e}", file=sys.stderr)

    # 4. Keyword search по memories (LIKE fallback — для коротких запросов без trigram совпадений)
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
            results.append(
                {
                    "id": r["id"],
                    "content": r["content"],
                    "type": r["type"],
                    "importance": r["importance"],
                    "strength": r["strength"],
                    "contact_id": r["contact_id"],
                    "created_at": r["created_at"],
                    "score": score,
                    "source": "agent_memory",
                }
            )

    # Dedup + sort
    seen = set()
    unique = []
    for r in results:
        key = (r.get("source", ""), r["id"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    if strategy == "hybrid" and len(unique) > limit:
        # RRF (Reciprocal Rank Fusion) для cross-source re-ranking
        unique = _rrf_rank(unique, limit)
    else:
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Entity-aware expansion: расширяем контекст по FK-связям
    # Передаём существующий conn (одно соединение на весь recall)
    if unique:
        expansion = _expand_entity_context(unique[:5], conn=conn)
        # Добавляем расширенный контекст как отдельную секцию
        if expansion:
            unique.append(
                {
                    "id": 0,
                    "content": _format_entity_context(expansion),
                    "type": "entity_context",
                    "importance": 0.0,
                    "score": -1,  # всегда в конце
                    "source": "entity_expansion",
                    "_expansion": expansion,
                }
            )

    conn.close()
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
            rows = conn.execute(
                """
                SELECT m.*, a.relation_type, a.weight
                FROM associations a
                JOIN memories m ON (
                    CASE WHEN a.memory_id_from = ? THEN a.memory_id_to
                         ELSE a.memory_id_from END = m.id
                )
                WHERE (a.memory_id_from = ? OR a.memory_id_to = ?)
                  AND m.id NOT IN ({})
            """.format(",".join("?" * len(visited))),
                [mid, mid, mid] + list(visited),
            ).fetchall()

            for r in rows:
                visited.add(r["id"])
                next_level.append(r["id"])
                results.append(
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "type": r["type"],
                        "relation": r["relation_type"],
                        "weight": r["weight"],
                        "strength": r["strength"],
                    }
                )
        current = next_level

    conn.close()
    return results[:limit]
