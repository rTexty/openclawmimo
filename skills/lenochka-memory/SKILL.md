---
name: lenochka-memory
description: "Единая система памяти Lenochka: запись (store, ingest, chaos), поиск (recall, chaos-search), классификация, извлечение сущностей, контекст-пакеты, дайджесты, консолидация. Точка входа для ВСЕХ операций с памятью."
metadata.openclaw.requires:
  env:
    - LENOCHKA_DB_PATH
  bins: ["python3", "sqlite3"]
---

# Lenochka Memory — Полный скилл памяти

Ядро Lenochka. Объединяет CRM-БД, Agent Memory (векторная), CHAOS (полнотекстовая). Через этот скилл — запись, поиск, классификация, извлечение, дайджесты, maintenance.

---

## 🏗 АРХИТЕКТУРА ПАМЯТИ

```
┌──────────────────────────────────────────────────────────┐
│ СЛОЙ 1: CRM-БД (SQLite, WAL, FK=ON)                     │
│ contacts, companies, chat_threads, messages, leads,      │
│ deals, tasks, agreements, invoices, payments,            │
│ business_connections, pending_notifications              │
├──────────────────────────────────────────────────────────┤
│ СЛОЙ 2: Agent Memory (когнитивная)                       │
│ memories (episodic/semantic/procedural)                  │
│ + vec_memories (sqlite-vec ANN, 384-dim)                 │
│ + associations (graph, 1 hop)                            │
│ + raptor_nodes (иерархическая суммаризация)              │
│ + memories_fts (FTS5 trigram)                            │
├──────────────────────────────────────────────────────────┤
│ СЛОЙ 3: CHAOS (быстрый поиск)                            │
│ chaos_entries (категоризированные микро-события)         │
│ + vec_chaos (sqlite-vec ANN)                             │
│ + chaos_fts (FTS5 trigram BM25)                          │
└──────────────────────────────────────────────────────────┘
```

---

## 🛠 ИНСТРУМЕНТЫ

Все команды через:
```bash
python3 run_memory.py <команда> [аргументы]
```

---

### 1. INIT — Создание/миграция БД

```bash
python3 run_memory.py init
```

Создаёт SQLite-БД со всеми таблицами, FTS5-индексами, триггерами. Если БД уже существует — проверяет миграции.

---

### 2. CLASSIFY — Классификация сообщения

```bash
python3 run_memory.py classify --text "Клиент согласился на 150к до пятницы"
```

**Возвращает JSON:**
```json
{"label": "decision", "confidence": 0.92, "reasoning": "Согласие + сумма + срок"}
```

**Категории (7 штук):**

| Label | Значение | Порог importance | Писать в memory? |
|-------|---------|-----------------|-----------------|
| `noise` | Спам, мусор | — | ❌ |
| `chit-chat` | Привет, как дела | — | ❌ |
| `business-small` | "Получил файл" | — | ❌ (только messages) |
| `task` | "Сделай КП" | 0.6 | ✅ |
| `decision` | "Согласен на 150к" | 0.8 | ✅ |
| `lead-signal` | "Сколько стоит?" | 0.6 | ✅ |
| `risk` | "Где деньги?!" | 0.8 | ✅ |

**Fallback:** Если LLM недоступен — эвристика по ключевым словам (не идеально, но работает).

---

### 3. EXTRACT — Извлечение сущностей

```bash
python3 run_memory.py extract --text "Договорились на 150000 рублей, оплата до 15 апреля, @ivan"
```

**Возвращает JSON:**
```json
{
  "contact": {"name": null, "tg_username": "ivan", "company": null},
  "lead": null,
  "deal": null,
  "task": null,
  "agreement": null,
  "amounts": [150000],
  "dates": ["2026-04-15"],
  "products": [],
  "risk_type": null
}
```

**Правила:**
- Извлекай ТОЛЬКО явно указанное. Не выдумывай.
- Суммы: "150к" = 150000, "150 тыс" = 150000, "$5000" = 5000
- Даты: "до пятницы" → ближайшая пятница, "до 15.04" → 2026-04-15
- @username → contact.tg_username
- **НЕ извлекай** из noise/chit-chat — экономия LLM

**Fallback:** regex для сумм, дат, @username (без LLM).

---

### 4. STORE — Запись в память

```bash
python3 run_memory.py store \
  --text "[decision] Бюджет 150к согласован, оплата до 15 апреля" \
  --importance 0.8 \
  --label "decision" \
  --contact_id 42 \
  --chat_thread_id 7 \
  --message_id 1001
```

**Что происходит:**
1. INSERT в `memories` (content, type=episodic, importance, contact_id, chat_thread_id, source_message_id)
2. Вычисление эмбеддинга (sentence-transformers, 384-dim)
3. INSERT в `vec_memories` (тот же rowid)
4. Один COMMIT на обе операции (транзакционно)
5. Автосвязывание (auto_associate) — ищет похожие memories через vec ANN

**Когда вызывать:**
- label = `task` / `decision` / `lead-signal` / `risk` → ОБЯЗАТЕЛЬНО
- label = `noise` / `chit-chat` / `business-small` → НЕ вызывать

**Параметры importance:**
- `0.8` — decisions, risks (важные)
- `0.6` — tasks, lead-signals (средние)
- `0.3` — low-confidence (на проверку)

**Ключевой параметр `--text`:**
- Всегда начинай с префикса категории: `[decision] ...`, `[risk] ...`
- Максимум ~500 символов (длинные обрезаются, но content_hash от полного)

---

### 5. RECALL — Поиск по памяти

```bash
python3 run_memory.py recall --query "договор сроки оплата" --contact_id 42 --limit 10
```

**Что происходит:**
1. **Vector ANN** (sqlite-vec, cosine similarity) — семантический поиск
2. **CHAOS FTS5** (trigram BM25) — полнотекстовый поиск по chaos_entries
3. **Memories FTS5** (trigram BM25) — полнотекстовый поиск по memories
4. **Keyword LIKE** (fallback) — простой поиск
5. **RRF** (Reciprocal Rank Fusion, k=60) — объединение 4 источников
6. **Entity expansion** — расширение контекста по FK-связям

**Возвращает JSON array:**
```json
[
  {
    "id": 42,
    "content": "[decision] Договор подписан на 150к",
    "type": "episodic",
    "importance": 0.8,
    "score": 0.92,
    "source": "vector",
    "rrf_applied": true
  },
  ...
]
```

**Когда вызывать:**
- Клиент спрашивает что-то из истории → ОБЯЗАТЕЛЬНО (запрещено галлюцинировать)
- "Что мы решили?", "Когда дедлайн?", "Сколько договорились?"
- Формирование контекста для LLM

**Правила:**
- Всегда передавай `--contact_id` если знаешь — фильтрует по конкретному клиенту
- Не передавай `--contact_id` для общих вопросов (поиск по всем)
- Результаты содержат `_expansion` (entity context) — используй для обогащения

---

### 6. INGEST — Полный пайплайн (главная команда)

```bash
python3 run_memory.py ingest \
  --text "Клиент согласился на 150к до пятницы" \
  --contact_id 42 \
  --chat_thread_id 7 \
  --message_id 1001
```

**Что происходит:**
1. **Dedup** — проверка content_hash + source_message_id
2. **Classify** — LLM определяет label + confidence
3. **Extract** — LLM извлекает сущности (только для важных label)
4. **Store** — memories + vec (если не noise/chit-chat)
5. **CHAOS store** — chaos_entries + vec (если не noise/chit-chat)

**Возвращает JSON:**
```json
{
  "label": "decision",
  "confidence": 0.92,
  "entities": {"amounts": [150000], ...},
  "stored": true,
  "memory_id": 142
}
```

**ВАЖНО:** ingest НЕ делает CRM upsert. После ingest нужно:
1. Если stored=true → вызвать `lenochka-crm` для contacts/deals/tasks
2. Если есть entities → маппить в CRM

**Dedup:**
- content_hash (SHA-256 первого 200 символов) — общий дедуп
- source_message_id + chat_thread_id — Telegram-специфичный дедуп
- При дубликате → возвращает `{"label": "duplicate", "skipped": true}`

---

### 7. CHAOS-STORE — Запись в CHAOS

```bash
python3 run_memory.py chaos-store \
  --text "Клиент согласился на 150к" \
  --category "decision" \
  --priority 0.8 \
  --contact_id 42
```

**Категории CHAOS:** `decision`, `risk`, `policy`, `fact`, `event`, `task`, `lead-signal`, `other`

**Когда вызывать:** Вместе с store (или через ingest, который делает оба).

---

### 8. CHAOS-SEARCH — Поиск в CHAOS

```bash
python3 run_memory.py chaos-search --query "договор" --limit 10
```

Быстрый BM25-поиск по FTS5. Read-only (не обновляет heat/access_count).

---

### 9. CONTEXT — Контекст-пакет для LLM

```bash
python3 run_memory.py context --query "статус проекта Ивана" --contact_id 42 --intent search
```

**Собирает:**
- **facts** — CRM-данные (контакт, сделки, задачи)
- **episodes** — memories (vector ANN или FTS5)
- **related** — CHAOS entries (BM25)
- **notes** — associations + entity expansion (FK traversal)

**Режимы `--intent`:**
- `search` — простой вопрос по ключевым словам
- `recall` — сложный вопрос про историю
- `core` — pinned facts (стартовая загрузка контекста)

---

### 10. DIGEST — Утренний дайджест

```bash
python3 run_memory.py digest
python3 run_memory.py digest --date 2026-03-31
```

**Собирает:**
- Новые лиды за день
- Просроченные задачи (view v_overdue_tasks)
- Брошенные диалоги (>24ч, view v_abandoned_dialogues)
- Ключевые события (memories с importance ≥ 0.7)

---

### 11. WEEKLY — Недельный отчёт

```bash
python3 run_memory.py weekly
```

**Статистика за 7 дней:** сообщения, лиды, задачи (создано/завершено), memories. Конверсия задач.

---

### 12. CONSOLIDATE — Ночная консолидация

```bash
python3 run_memory.py consolidate
```

**Что делает:**
1. **Decay** — strength × 0.95 для неиспользуемых >7 дней
2. **Merge** — vec ANN ищет дубли (sim > 0.85), мержит, чистит vec + associations
3. **Cluster** — auto_associate для свежих memories
4. **RAPTOR** — иерархическая суммаризация (L0, L1)
5. **Cleanup** — удаляет слабые (strength < 0.15, importance < 0.3) + чистит vec

**Когда запускать:** Ночной cron, 03:00 GMT+8. Один раз в день.

---

### 13. STATS — Статистика

```bash
python3 run_memory.py stats
```

Показывает количество записей во всех таблицах + размер БД.

---

## 📋 ИНСТРУКЦИЯ: КАК ОБРАБАТЫВАТЬ СООБЩЕНИЕ

Для КАЖДОГО входящего сообщения:

```
1. Получить текст (см. скилл lenochka-pipeline: normalize)
2. Проверить дедуп:
   sqlite3 $DB "SELECT id FROM messages WHERE content_hash = ?"
   → Если есть → SKIP (дубликат)

3. Вызвать ingest:
   python3 run_memory.py ingest --text "..." --contact_id N --chat_thread_id N --message_id N

4. Если label ∈ {task, decision, lead-signal, risk}:
   → вызвать lenochka-crm: deal/task/contact по entities

5. Решить ответ (см. скилл lenochka-response)
```

---

## ⚠️ EDGE CASES

| Случай | Что делать |
|--------|-----------|
| LLM недоступен | fallback: heuristic classify + regex extract |
| Пустой ответ LLM | classify → "other", extract → пустой |
| Очень длинное сообщение | content_hash от полного, store от первого 200 символов |
| Отредактированное сообщение | Найти по source_message_id → UPDATE memories.content, chaos_entries.content |
| Удалённое сообщение | UPDATE messages SET meta_json = '{"deleted": true}' |
| Дубликат по content_hash | Вернуть `{"skipped": true, "existing_id": N}` |
| FK constraint failed | try/rollback, не падать |
| sqlite-vec недоступен | Fallback: keyword LIKE поиск |
| sentence-transformers недоступен | Fallback: char 3-gram TF hash (детерминированный) |
