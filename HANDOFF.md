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



# SESSION HANDOFF — Lenochka Project
# Сессия 2026-03-29 23:02 — 00:00 GMT+8
# Камиль + Леночка

## Что произошло

### Предыдущие сессии (из старого HANDOFF)
- Сессия 2026-03-29 16:21—17:08: созданы mem.py v2, brain.py v2, схема SQL, проведён аудит

### Эта сессия (2026-03-29 23:02 — 00:00)

1. **Переключение на OpenClaw:** Камиль перенёс проект из старого workspace в OpenClaw.
   - Удалены старые файлы, склонирован https://github.com/rTexty/openclawmimo
   - PAT для push: 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
   - Регулярные коммиты и пуши в репо

2. **Погружение в контекст:** Изучены ВСЕ файлы проекта:
   - BLUEPRINT.md (полный user flow, 28 уязвимостей, 4 этапа)
   - HANDOFF.md (история предыдущей сессии)
   - lenochka-context/*.md (3 файла контекста от Камиля)
   - lenochka-memory/ (mem.py 920 строк, brain.py 846 строк, init.sql, AUDIT.md)
   - SOUL.md, AGENTS.md, USER.md, IDENTITY.md

3. **Продуктовый контекст от Камиля:**
   - Леночка = невидимая CRM + самопишущаяся база знаний + операционный помощник
   - Главная проблема: хаос в Telegram, потеря задач/клиентов/договорённостей
   - Главный архитектурный вызов: память + сжатие контекста + шумоизоляция + быстрый поиск

4. **Исследование документации:**
   - Telegram Bot API v9.5 (март 2026) — Business API (business_connection, business_message, etc.)
   - Aiogram 3.26 — async framework, middleware pipeline, routers
   - Установлены зависимости: aiogram 3.26, pydantic-settings, apscheduler

5. **Архитектура Telegram-бота** (ARCHITECTURE-TELEGRAM-BOT.md):
   - 1644 строки, полный дизайн документ
   - Два режима: Direct Bot Chat (команды) + Business Account (невидимая CRM)
   - Normalize layer, CRM upsert, pipeline processor, brain wrapper
   - 15+ edge cases, 12 фаз реализации, ~12 часов

6. **Реализация бота** (lenochka-bot/ — 14 файлов, ~1700 строк):
   - Полный ingest pipeline: message → normalize → dedup → classify → extract → store → CRM
   - Brain wrapper: модель загружается ОДИН раз (решение cold start 6.6с)
   - Async pipeline processor с батчингом
   - CRM upsert: contacts, deals, leads, tasks из сообщений
   - Command handlers: /start, /status, /leads, /tasks, /digest, /weekly, /find, /help
   - Scheduler: daily digest, weekly report, consolidate, abandoned check
   - Middleware: Logging + Throttling (30 msg/min)

7. **Критическая правка — антипетля:**
   - ПЕРВОНАЧАЛЬНО: sender_business_bot → skip (не сохранять)
   - ИСПРАВЛЕНО: ВСЕ сообщения сохраняются в CRM (включая свои)
   - Антипетля работает ТОЛЬКО на уровне ОТВЕТА (should_respond), не записи
   - Причина: бот может написать «Встреча в среду в 10» от имени Камиля — CRM должна это видеть

## Структура файлов (текущая)

```
workspace/
├── .gitignore
├── AGENTS.md                         # Инструкции для агента
├── SOUL.md                           # Личность агента
├── IDENTITY.md                       # Леночка, AI-ассистент
├── USER.md                           # Камиль, GMT+8
├── HEARTBEAT.md                      # Пустой
├── TOOLS.md                          # Пустой шаблон
├── HANDOFF.md                        # ← этот файл
├── BLUEPRINT.md                      # Полная карта проекта (старая, не обновлена)
├── ARCHITECTURE-TELEGRAM-BOT.md      # Архитектура бота (1644 строки)
│
├── lenochka-context/                 # Исходные файлы контекста от Камиля
│   ├── lenochka-1-context-goals.md
│   ├── lenochka-2-memory-implementation.md
│   └── lenochka-3-skills-implementation.md
│
├── lenochka-memory/                  # Ядро: память + интеллект
│   ├── SKILL.md                      # Документация скилла
│   ├── mem.py                        # CLI (920 строк) — ТОЧКА ВХОДА ДЛЯ ПАМЯТИ
│   ├── brain.py                      # Интеллект (846 строк) — classify, embed, RAPTOR
│   ├── AUDIT.md                      # 15 проблем + решения
│   ├── schemas/init.sql              # 14 таблиц + FTS5 + триггеры
│   └── db/lenochka.db                # Рабочая БД
│
└── lenochka-bot/                     # Telegram-бот (НОВОЕ — эта сессия)
    ├── __main__.py                   # Entry point (polling, hooks)
    ├── config.py                     # pydantic-settings
    ├── requirements.txt              # aiogram, pydantic-settings, apscheduler
    ├── handlers/
    │   ├── business.py               # business_connection/message/edited/deleted
    │   ├── commands.py               # /start, /status, /leads, /tasks, etc.
    │   └── errors.py                 # Global error handler
    ├── middlewares/
    │   ├── throttling.py             # Anti-spam (30 msg/min)
    │   └── logging.py                # Structured logging
    ├── filters/
    │   └── business.py               # IsBusinessMessage filter
    └── services/
        ├── brain_wrapper.py          # Daemon mode brain (модель один раз)
        ├── pipeline.py               # Async ingest queue с батчингом
        ├── normalizer.py             # Text extraction (ALL message types)
        ├── contact_resolver.py       # Telegram user → CRM contact
        ├── crm_upsert.py             # Entities → CRM tables
        ├── memory.py                 # Dedup, store, status, search
        ├── digest.py                 # Генерация дайджестов
        └── scheduler.py              # Cron: digest, consolidate, abandoned
```

## Ключевые решения (эта сессия)

1. **Business API > Userbot** — легально, стабильно, нет риска бана
2. **Aiogram 3.x > python-telegram-bot** — async-first, лучше middleware
3. **Brain wrapper > CLI calls** — модель один раз в памяти, нет cold start
4. **Async pipeline > inline processing** — не блокирует Telegram API при LLM
5. **Save ALL messages** — включая свои (sender_business_bot). Антипетля только на ответ
6. **Heuristic-first classify** — сокращает LLM-вызовы на ~70%
7. **Skip extract для noise/chit-chat** — экономия LLM-вызовов
8. **auto_associate=False в pipeline** — defer на nightly consolidate

## Что работает (проверено)

- ✅ Все модули импортируются без ошибок
- ✅ Brain wrapper инициализируется
- ✅ Pipeline processor создаётся
- ✅ All 14 файлов компилируются
- ✅ aiogram 3.26.0 установлен
- ✅ Git: 3 коммита, все запушены

## Что НЕ сделано (следующие шаги)

### Phase 1 (базовая функциональность):
- [ ] `.env` файл с BOT_TOKEN и OWNER_ID
- [ ] End-to-end тест: запуск бота, получение сообщения, ingest
- [ ] Тест с mock business_message
- [ ] Проверка что SQLite БД корректно пишется из бота

### Phase 2 (улучшения памяти):
- [ ] Context window для classify (последние 5 сообщений из chat_thread)
- [ ] Supersede для edited messages (обновление memory по source_message_id)
- [ ] Batch classify (10 сообщений в одном LLM-вызове)
- [ ] RRF (Reciprocal Rank Fusion) для recall

### Phase 3 (продакшн):
- [ ] Response engine (should_respond + generate_response)
- [ ] Антипетля на уровне ответа (sender_business_bot → не отвечать)
- [ ] Voice transcription (Whisper/groq)
- [ ] OCR для фото
- [ ] Multi-user изоляция
- [ ] Tests

### Phase 4 (масштаб):
- [ ] Webhook mode (вместо polling)
- [ ] Connection pool / single-writer
- [ ] Message queue (Redis)
- [ ] Monitoring / metrics

## Известные проблемы

1. **sqlite-vec не установлен** — векторный поиск через fallback (char n-grams)
2. **sentence-transformers не установлен** — используется char n-gram fallback
3. **Нет .env файла** — нужно создать с BOT_TOKEN и OWNER_ID
4. **messages.analyzed не существует в схеме** — нужно добавить ALTER TABLE или обновить init.sql
5. **messages.meta_json для soft-delete** — работает, но нет отдельного поля deleted
6. **BLUEPRINT.md устарел** — не отражает реализацию бота, нужно обновить

## Технический стек (текущий)

- Python 3.12
- aiogram 3.26.0 (async Telegram framework)
- pydantic-settings 2.13 (config)
- apscheduler 3.11 (scheduler)
- SQLite 3.45.1 + WAL mode
- numpy 2.4.3
- sentence-transformers: НЕ установлен (fallback)
- sqlite-vec: НЕ установлен (fallback)

## Git log

```
9f696e5 Fix: save ALL messages including bot's own replies
cae1a85 Add .gitignore, remove cached files
028fd8c Bot implementation: 14 files, full ingest pipeline
fad2890 Architecture: Telegram Bot + Business API integration
1598eb8 BLUEPRINT.md: полный анализ user flow, уязвимости и 4-этапный план
c052d76 Phase 1 fixes (7 items):
8d5ea28 Lenochka Memory v2 — initial implementation
```

