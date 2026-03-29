---
name: lenochka-memory
description: "Единая система памяти Lenochka AI: CRM-БД (SQLite) + Agent Memory (episodic/semantic/procedural + RAPTOR) + CHAOS (BM25 trigram + vector embeddings + граф + heat). Полный пайплайн: ingest → classify → extract → store → recall → context → digest. Векторный поиск через sqlite-vec + sentence-transformers."
metadata:
  openclaw:
    requires:
      bins: ["python3", "sqlite3"]
      pypi: ["sentence-transformers", "sqlite-vec"]
---

# Lenochka Memory — Единый Скилл Памяти 🔥

Полная система памяти для Lenochka AI. Объединяет три слоя данных и интеллектуальный слой:

| Слой | Компонент | Назначение |
|------|-----------|------------|
| **Данные** | CRM-БД (SQLite) | Контакты, компании, лиды, сделки, задачи, договоры, счета, платежи |
| **Когнитивная** | Agent Memory | Episodic / Semantic / Procedural + RAPTOR иерархия |
| **Поиск** | CHAOS | BM25 FTS5 (trigram) + vector embeddings (sqlite-vec) + граф + heat |
| **Интеллект** | Brain | LLM-классификация, семантические эмбеддинги (sentence-transformers), контекст-пакеты, дайджесты |

## Требования

- Python 3.10+
- `pip install sentence-transformers sqlite-vec`
- SQLite 3.40+ (для trigram tokenizer в FTS5)
- Опционально: LLM API (OpenAI-совместимый) для классификации и экстракции

## Инициализация

```bash
cd skills/lenochka-memory
python3 mem.py init
```

Создаёт SQLite-БД с 14 таблицами + FTS5 индексами (trigram) + векторные таблицы sqlite-vec.

## Архитектура эмбеддингов

**Основной путь:** `sentence-transformers` (all-MiniLM-L6-v2, 384-dim)
**Fallback:** character n-gram hash projection в 384-dim
**Хранение:** sqlite-vec (`vec_memories`, `vec_chaos`) для ANN-поиска

## CLI — Полный справочник

### Ingest (Полный пайплайн)

```bash
python3 mem.py ingest "Клиент Иванов согласился на предоплату 30% до 5 мая" \
  --contact-id 42 --chat-thread-id 7
```

### Классификация

```bash
python3 mem.py classify "Привет, как дела?"
# → {"label": "chit-chat", "confidence": 0.9, ...}

python3 mem.py classify "Сделай КП для ООО Ромашка, срочно"
# → {"label": "task", "confidence": 0.85, ...}
```

### Извлечение сущностей

```bash
python3 mem.py extract "Договорились на 150000 рублей, оплата до 15 апреля"
# → {"amounts": [150000], "dates": ["2026-04-15"], ...}
```

### Capture (Запись)

```bash
# Episodic + автосвязывание + векторный эмбеддинг
python3 mem.py store "Клиент X согласился на предоплату 30%" \
  --type episodic --importance 0.7 --contact-id 42

# Semantic (политика)
python3 mem.py store "Минимальный чек — 10k" --type semantic --importance 0.9

# Procedural (инструкция)
python3 mem.py store "Как оформлять возврат: ..." --type procedural

# CHAOS
python3 mem.py chaos-store "Решение по цене" --category decision --priority 0.8
```

### Recall (Поиск)

```bash
# Гибридный поиск (vector + BM25 + keyword)
python3 mem.py recall "оплата клиент X" --strategy hybrid --limit 20

# Только векторный (ANN через sqlite-vec)
python3 mem.py recall "договор" --strategy vector --limit 10

# Только BM25 (CHAOS FTS5 trigram)
python3 mem.py recall "договор" --strategy bm25 --limit 10

# Только keyword (LIKE)
python3 mem.py recall "условия доставки" --strategy keyword --limit 10

# Связанные воспоминания (graph, N hops)
python3 mem.py recall-assoc --from-memory-id 15 --hops 2
```

### Context-Packet (Для LLM)

```bash
python3 mem.py context "оплата по договору" --contact-id 42 --intent search

# Режимы intent:
#   core   — pinned/high-importance факты при старте диалога
#   search — простой вопрос по ключевым словам
#   recall — сложный вопрос про историю взаимодействия
```

### CRM-запросы

```bash
python3 mem.py crm contact --tg "@ivan"
python3 mem.py crm deals --contact-id 42
python3 mem.py crm overdue-tasks
python3 mem.py crm abandoned --hours 24
python3 mem.py crm leads --since "2026-03-20"
python3 mem.py crm daily-summary --date "2026-03-27"
```

### Дайджесты

```bash
python3 mem.py digest                    # Утренний дайджест за сегодня
python3 mem.py digest --date 2026-03-27  # За конкретный день
python3 mem.py weekly                    # Недельный дайджест
```

### RAPTOR

```bash
python3 mem.py raptor --level 0  # Построить leaf nodes
python3 mem.py raptor --level 1  # Построить upper level
```

### Maintenance

```bash
python3 mem.py consolidate       # Decay + cluster + merge + RAPTOR + cleanup
python3 mem.py chaos-reindex     # Пересобрать FTS5-индекс
python3 mem.py prune-messages --older-than 180
python3 mem.py stats
```

## Файловая структура

```
skills/lenochka-memory/
├── SKILL.md              ← документация
├── mem.py                ← CLI-утилита (точка входа)
├── brain.py              ← Интеллектуальный модуль
├── db/
│   └── lenochka.db       ← SQLite-база (создаётся при init)
└── schemas/
    └── init.sql          ← SQL-схема (14 таблиц + views + FTS5 + триггеры)
```

## Пайплайн ingest

```
Telegram message
       ↓
   [classify] → label (noise|chit-chat|task|decision|lead-signal|risk)
       ↓
   [extract] → entities (contact, amounts, dates, tasks, deals)
       ↓
   [store] → Agent Memory (episodic) + vector embedding + автосвязывание
       ↓
   [chaos-store] → CHAOS (BM25 searchable + vector)
       ↓
   [crm upsert] → CRM-БД (contacts, deals, tasks)
```

## Пайплайн recall (перед ответом LLM)

```
User query
       ↓
   [context packet]
       ├── facts ← CRM-БД (контакты, сделки, задачи)
       ├── episodes ← Agent Memory (vector ANN или FTS5 trigram)
       ├── related ← CHAOS (BM25 + heat)
       └── notes ← Associations (graph, 1 hop)
       ↓
   → System prompt для LLM
```

## Maintenance (ночной cron)

```
[consolidate]
  ├── Decay: strength * 0.95 для неиспользуемых
  ├── Merge: сливает дубли (sim > 0.85)
  ├── Cluster: создаёт ассоциации между похожими
  ├── RAPTOR: строит/обновляет иерархию
  └── Cleanup: удаляет слабые (strength < 0.15, importance < 0.3)
```

## LLM Integration

Brain использует OpenAI-совместимый API. Настройка через переменные окружения:

```bash
export LENOCHKA_LLM_BASE_URL="https://api.xiaomimimo.com/v1"
export LENOCHKA_LLM_API_KEY="your-key"
export LENOCHKA_LLM_MODEL="mimo-v2-pro"
```

При недоступности LLM автоматически используются эвристические fallback-и.

## Стратификация по времени

| Период | Хранение |
|--------|----------|
| 0–7 дней | Полные сообщения + все события + свежие memories |
| 1–4 недели | Daily summary + ключевые события |
| 1–6 месяцев | Weekly summary + агрегаты |
| 6+ месяцев | Monthly summary + RAPTOR L2/L3 |
