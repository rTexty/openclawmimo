# SESSION HANDOFF — Lenochka Project
# Сессия 2026-03-29 16:21 — 17:08 GMT+8
# Камиль + Леночка

## Что произошло

1. **Знакомство:** Камиль (AI архитектор в стартапе), я — Леночка (AI-ассистент)
2. **Проект:** Lenochka — персональный AI-ассистент в Telegram, невидимая CRM поверх мессенджера
3. **Загружены 3 файла контекста** (архитектура, память, skills) — описывают продукт
4. **Загружены 6 файлов предыдущей попытки** (SKILL.md, mem.py, brain.py, init.sql, TOOLS.md, MEMORY.md) — старая версия "Клаудика"
5. **Переписаны и улучшены** mem.py (v2) и brain.py (v2) с реальными эмбеддингами
6. **Протестировано:** init, store, recall (vector), chaos-search, ingest, digest, stats — всё работает
7. **Проведён аудит** системы — 15 проблем найдено, записано в AUDIT.md
8. **Публикация в ClawHub** отложена — нужно залогиниться (`clawhub login`) + исправить проблемы из Phase 1

## Структура файлов

```
workspace/
├── AGENTS.md                    # Инструкции для агента
├── SOUL.md                      # Личность агента
├── IDENTITY.md                  # Леночка, AI-ассистент
├── USER.md                      # Камиль, GMT+8
├── BOOTSTRAP.md                 # Всё ещё тут (не удалялся — не обязательно)
├── TOOLS.md / HEARTBEAT.md      # Стандартные
│
├── lenochka-context/            # Исходные файлы контекста от Камиля
│   ├── lenochka-1-context-goals.md
│   ├── lenochka-2-memory-implementation.md
│   └── lenochka-3-skills-implementation.md
│
└── lenochka-memory/             # Основной проект
    ├── SKILL.md                 # Документация скилла (для ClawHub)
    ├── mem.py                   # CLI-утилита (920 строк)
    ├── brain.py                 # Интеллектуальный модуль (846 строк)
    ├── AUDIT.md                 # Аудит: 15 проблем + решения
    ├── schemas/
    │   └── init.sql             # SQL-схема (14 таблиц + FTS5 + vec)
    └── db/
        └── lenochka.db          # Рабочая БД (4 memories, 2 chaos, 3 vec, 1 vec_chaos)
```

## Что нужно сделать дальше (Phase 1 — до публикации)

1. Транзакционный store (один COMMIT) — 5 мин
2. Дедуп ingest по content hash — 15 мин
3. Retry в `_call_llm` — 10 мин
4. Расширить CHAOS category — 5 мин
5. auto_associate try/finally — 5 мин
6. FTS try/except — 5 мин
7. Batch embed_texts() — 15 мин

После Phase 1: `clawhub login` → `clawhub publish lenochka-memory`

## Ключевые решения

- **CRM-БД:** SQLite (не PostgreSQL) — проще для старта
- **Эмбеддинги:** sentence-transformers (all-MiniLM-L6-v2, 384-dim) с fallback на char n-grams
- **Векторный поиск:** sqlite-vec (ANN) — работает, быстрый (0.24ms)
- **FTS:** FTS5 с trigram tokenizer (лучше для кириллицы чем unicode61)
- **LLM:** OpenAI-совместимый API, MiMo V2 Pro по умолчанию, эвристический fallback
- **Cost:** ~$0/мес при MiMo, $1-20/мес при платных API (500 msg/day)

## Известные проблемы (из AUDIT.md)

### Критичные:
1. Холодный старт модели 6.6с (нужен daemon mode)
2. store() два COMMIT (нужна транзакционность)
3. Нет дедупликации ingest (дубли сообщений)
4. consolidate() O(n²) = 46 мин (нужен vec ANN вместо brute-force)

### Средние:
5. 2 LLM-вызова/сообщение (нужен батчинг)
6. task/lead маппятся на event в CHAOS
7. Нет retry в _call_llm
8. Нет batch-ингеста
9. 400MB RAM на модель

## Технические детали

- Python 3.12, SQLite 3.45.1
- sentence-transformers 5.3.0, sqlite-vec 0.1.7, numpy 2.4.3
- ClawHub CLI v0.7.0 (не залогинен)
- Git инициализирован в workspace
- Модель: all-MiniLM-L6-v2, 384-dim embeddings
- БД протестирована: init → store (3 memories) → chaos-store (1) → ingest (1) → recall (vector) → context → digest — всё работает
