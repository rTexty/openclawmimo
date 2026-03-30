# Lenochka Blueprint — Полная карта проекта

> Документ создан 2026-03-29. Описывает всё: что есть, что сломается, к чему стремиться.
> **Последнее обновление: 2026-03-30 02:59 GMT+8** — 6 critical fixes, entity expansion, sqlite-vec + sentence-transformers.

## СТАТУС РЕАЛИЗАЦИИ (актуально)

| Компонент | Статус | Файлы |
|-----------|--------|-------|
| CRM-БД (SQLite) | ✅ Готова | `lenochka-memory/schemas/init.sql` (15 таблиц + business_connections) |
| Agent Memory | ✅ Работает | `lenochka-memory/mem.py` + `brain.py` |
| CHAOS Search | ✅ Работает | `lenochka-memory/mem.py` |
| Telegram-бот | ✅ Реализован (16 файлов) | `lenochka-bot/` |
| Normalize Layer | ✅ Все типы сообщений | `lenochka-bot/services/normalizer.py` |
| CRM Upsert | ✅ contacts/deals/leads/tasks | `lenochka-bot/services/crm_upsert.py` |
| Brain Wrapper (daemon) | ✅ Модель один раз | `lenochka-bot/services/brain_wrapper.py` |
| Pipeline (async queue) | ✅ Batch classify + batch embed + supersede + entity-enriched extract | `lenochka-bot/services/pipeline.py` |
| Scheduler | ✅ Digest/weekly/consolidate | `lenochka-bot/services/scheduler.py` |
| Webhook mode | ✅ Polling + webhook (aiohttp) | `lenochka-bot/__main__.py` |
| Supersede (edited) | ✅ source_msg_id lookup + cascade to memories/chaos | `lenochka-bot/services/memory.py` |
| Soft-delete | ✅ source_msg_id lookup | `lenochka-bot/services/memory.py` |
| Business connections | ✅ DB table + CRUD | `lenochka-bot/services/memory.py` |
| Batch classify | ✅ N messages → 1 LLM call | `lenochka-memory/brain.py` (`classify_batch`) |
| Consolidate vec ANN | ✅ O(n·k) вместо O(n²) | `lenochka-memory/mem.py` |
| Архитектура | ✅ Задокументирована | `ARCHITECTURE-TELEGRAM-BOT.md` |
| Owner auth | ✅ OwnerMiddleware + is_owner checks | `lenochka-bot/middlewares/owner.py` |
| LLM config | ✅ Единый префикс LEN_LLM_* | `brain.py` + `config.py` |
| Transactional store | ✅ try/rollback для store + chaos_store | `lenochka-memory/mem.py` |
| sqlite-vec | ✅ Установлен, работает | v0.1.7 |
| sentence-transformers | ✅ Установлен, работает | v5.3.0, all-MiniLM-L6-v2, 384-dim |
| Entity expansion | ✅ FK-traversal chain в recall + context_packet + /find + pipeline | `mem.py` + `brain.py` |
| Response Engine | ❌ Не реализован (← запись до сессии 2026-03-30 14:50) | Phase 3 |
| Response Engine v2 | ✅ Реализован: combined classify+route, fact-based response, escalation, proactive, progress check-in, follow-up detection, ResponseGuard | см. `RESPONSE-ENGINE-ARCHITECTURE.md` + `lenochka-bot/services/response_engine.py` |
| Voice Transcription | ❌ Не реализован | Phase 4 |
| OCR | ❌ Не реализован | Phase 4 |
| Multi-user | ❌ Не реализован (infra готова: business_connections table) | Phase 4 |

---

## Содержание

1. [Что такое Lenochka](#1-что-такое-lenochka)
2. [Архитектура сегодня](#2-архитектура-сегодня)
3. [Полный user flow](#3-полный-user-flow)
4. [Все уязвимые места](#4-все-уязвимые-места)
5. [Непродуманные сценарии](#5-непродуманные-сценарии)
6. [4 этапа развития](#6-4-этапа-развития)

---

## 1. Что такое Lenochka

### Продукт

Lenochka — персональный AI-ассистент в Telegram, который работает как невидимая CRM поверх мессенджера. Ключевая идея: пользователь продолжает жить в Telegram, а Lenochka незаметно превращает поток сообщений в структурированные сущности (лиды, сделки, задачи, договорённости), следит за «хвостами» и помогает не терять деньги и время.

### Ключевая фишка

Пользователь не знает, что CRM существует. Он просто живёт в Telegram. CRM строится вокруг него автоматически.

### Основные сценарии

1. **Коммуникации** — кто что сказал, когда договорились, что обещали
2. **Задачи** — извлечение из разговоров, напоминания, отслеживание
3. **Лиды и продажи** — обнаружение интереса, суммы, сроки
4. **Документы и финансы** — КП, договоры, счета, оплаты
5. **База знаний** — поиск по всей истории общения

### Метрики успеха

- % лидов, дошедших до «обработан»
- Количество брошенных диалогов
- Среднее время от лида до первого ответа
- Количество задач со статусом done
- NPS/CSAT пользователя

---

## 2. Архитектура сегодня

### Схема

```
┌─────────────────────────────────────────────────┐
│                Telegram API                     │  ← нет кода
├─────────────────────────────────────────────────┤
│              Normalize Layer                    │  ← нет кода
├─────────────────────────────────────────────────┤
│            Brain (brain.py, 846 строк)          │
│  classify → extract → embed → LLM → RAPTOR     │
├─────────┬───────────────────┬───────────────────┤
│ CRM-БД  │   Agent Memory    │    CHAOS Memory   │
│ (SQLite) │   (sqlite-vec)    │  (FTS5 + vec)     │
│ 14 табл  │  memories + vec   │  chaos + vec      │
├─────────┴───────────────────┴───────────────────┤
│          mem.py CLI (920 строк)                 │  ← единственная точка входа
└─────────────────────────────────────────────────┘
```

### Файлы

```
openclawmimo/
├── AGENTS.md                     # Инструкции для агента
├── SOUL.md                       # Личность агента
├── IDENTITY.md                   # Леночка, AI-ассистент
├── USER.md                       # Камиль, GMT+8
├── HANDOFF.md                    # Лог предыдущей сессии
├── BLUEPRINT.md                  # ← этот файл
│
├── lenochka-context/             # Исходные файлы контекста от Камиля
│   ├── lenochka-1-context-goals.md
│   ├── lenochka-2-memory-implementation.md
│   └── lenochka-3-skills-implementation.md
│
└── lenochka-memory/              # Основной проект
    ├── SKILL.md                  # Документация скилла
    ├── mem.py                    # CLI-утилита (920 строк)
    ├── brain.py                  # Интеллектуальный модуль (846 строк)
    ├── AUDIT.md                  # Аудит: 15 проблем + решения
    ├── schemas/
    │   └── init.sql              # SQL-схема (14 таблиц + FTS5 + триггеры)
    └── db/
        └── lenochka.db           # Рабочая БД
```

### Технологии

| Компонент | Технология | Версия |
|-----------|-----------|--------|
| Язык | Python | 3.12 |
| БД | SQLite + WAL | 3.45.1 |
| Вектора | sqlite-vec | 0.1.7 |
| Эмбеддинги | sentence-transformers (all-MiniLM-L6-v2) | 5.3.0 |
| LLM | OpenAI-совместимый API (MiMo V2 Pro) | — |
| Полнотекстовый поиск | FTS5 trigram tokenizer | — |

### Что работает (после Phase 1 фиксов)

- ✅ `init` — создание БД с векторными таблицами
- ✅ `store` — запись + эмбеддинг в одной транзакции
- ✅ `recall` — гибридный поиск (vector + BM25 + keyword)
- ✅ `ingest` — classify → extract → store → chaos с дедупом
- ✅ `chaos-store/search` — FTS5 trigram с fallback LIKE
- ✅ `crm` — contacts, deals, tasks, leads, abandoned, daily_summary
- ✅ `context` — контекст-пакет для LLM
- ✅ `digest/weekly` — дайджесты
- ✅ `consolidate` — decay + merge + cluster + RAPTOR
- ✅ Fallback-ы — всё работает без LLM и без sentence-transformers

### Что НЕ существует (остаётся)

- ❌ Response engine (когда и как отвечать) — Phase 3
- ❌ Multi-user изоляция — Phase 4
- ❌ Миграции схемы БД
- ❌ Тесты
- ❌ Voice transcription, OCR — Phase 4

### Что РЕАЛИЗОВАНО (но не существовало на момент написания)

- ✅ Telegram-connector (приём сообщений) → `lenochka-bot/handlers/business.py`
- ✅ Normalize layer → `lenochka-bot/services/normalizer.py`
- ✅ CRM upsert → `lenochka-bot/services/crm_upsert.py`
- ✅ Daemon mode → `lenochka-bot/services/brain_wrapper.py`
- ✅ Logging framework → `lenochka-bot/middlewares/logging.py`
- ✅ Owner auth → `lenochka-bot/middlewares/owner.py`
- ✅ Entity expansion → `lenochka-memory/mem.py` (_expand_entity_context)

---

## 3. Полный user flow

### 3.1 Шаг 0: Клиент пишет в Telegram

#### Типы сообщений, которые приходят

| Тип | Пример | Текст доступен? | Сейчас обрабатывается? |
|-----|--------|----------------|----------------------|
| text | «Согласен на 150к» | ✅ `message.text` | ⚠️ Только если передать в ingest |
| photo + caption | [скриншот] «Вот счёт» | ⚠️ `message.caption` | ❌ |
| voice | [голосовое 30сек] | ❌ Нужна транскрипция | ❌ |
| video_note | [кружок] | ❌ Нет caption | ❌ |
| sticker | 👍 или 💰 | ❌ Нет текста | ❌ |
| document | [invoice.pdf] «Подпиши» | ⚠️ caption + парсинг PDF | ❌ |
| forwarded | [переслано от Ивана] | ⚠️ `forward_origin` | ❌ |
| reply | [ответ на сообщение] «Вот» | ⚠️ `reply_to_message` | ❌ |
| location | [геолокация] | ❌ | ❌ |
| contact | [контакт: Иван] | ⚠️ Структурированные данные | ❌ |
| edited | «120к» → «150к» | ⚠️ Обновление существующей записи | ❌ |
| deleted | — | ❌ Telegram не шлёт уведомлений | ❌ |
| group | Несколько людей пишут | ⚠️ Определение автора | ❌ |

#### Критические сценарии на входе

1. **Стикер 👍 как подтверждение сделки.** Клиенту написали «Значит, 150к до пятницы, так?», клиент отвечает 👍. Сейчас: `ingest(None)` → краш или heuristic chit-chat → сделка потеряна.

2. **Голосовое сообщение.** Клиент говорит «да, давай 150 тысяч, к пятнице». Ключевая бизнес-информация. Сейчас: невозможно обработать.

3. **Фото + подпись.** «Вот скрин переписки, он согласен» — подпись есть, но ingest её не видит. Скриншот содержит текст (сумма, имя) — нужен OCR.

4. **Отредактированное сообщение.** Клиент написал «120к», потом исправил на «150к». Telegram шлёт `edited_message`. Сейчас: ingest вызовется дважды, оба запишутся. Нет механизма обновления.

5. **Пересланное сообщение.** Камиль пересылает сообщение от клиента. `forward_origin` показывает исходного автора. Сейчас: текст приходит, но attribution теряется.

6. **Ответ (reply).** Клиент отвечает «да» на «Могу сделать за 150к?». Без контекста оригинала «да» = chit-chat. Нужен `reply_to_message.text`.

### 3.2 Шаг 1: Предобработка (не существует)

#### Должно происходить

```
raw_message
  → extract_text()        # text/caption/transcription/emoji-value
  → resolve_reply()        # если reply — получить контекст
  → resolve_forward()      # если forward — определить автора
  → resolve_contact()      # Telegram user → CRM contact
  → normalize()            # привести к единому формату
  → clean_text             # готовый текст для классификации
```

#### Что сейчас происходит

`ingest(text)` получает голую строку. Нет:
- Извлечения текста из caption/photo/voice
- Разрешения reply-контекста
- Определения автора forwarded-сообщения
- Склеивания multi-message контекста

### 3.3 Шаг 2: Классификация (brain.classify_message)

#### Как работает

LLM получает текст, возвращает label из 8 категорий: noise, chit-chat, business-small, task, decision, lead-signal, risk, other.

При недоступности LLM — эвристика по ключевым словам.

#### Где ломается

| Сценарий | Вход | Результат | Проблема |
|----------|------|-----------|----------|
| Стикер-подтверждение | 👍 | chit-chat | Это «да, согласен» = decision |
| Двузначное | «Подумаем» | chit-chat | Может быть «нужно время» = risk |
| Уточнение суммы | «Ок, 120 не 150» | task? | Это counter-offer = lead-signal или risk |
| Благодарность | «Спасибо, вечером напишу» | chit-chat | Это «я уведомлён» = business-small |
| Смешанный | «Как дела? Ладно, по делу — согласен на 120к» | chit-chat ИЛИ lead-signal | Нужно split на два сообщения |
| Абсолютный отказ | «Нет» | noise/chit-chat | Может быть отказ от сделки = risk |
| Подтверждение | «Понял» | chit-chat | Может быть подтверждение задачи |
| На английском | «Sounds good, let's do 150k» | другой результат | Промпт на русском, вход на английском |
| Сарказм | «Ну конечно, за 50к я ещё и танцую» | lead-signal (50к) | Это не предложение, а сарказм |
| Число без контекста | «150» | noise? | Может быть ответ на «сколько?» |

#### Критический сценарий

Клиент пишет 👍 на сообщение «Значит, 150к до пятницы, так?». Сейчас: ingest получит «👍» → heuristic → chit-chat → пропуск. **Сделка потеряна.**

### 3.4 Шаг 3: Извлечение сущностей (brain.extract_entities)

#### Как работает

LLM извлекает: contact, lead, deal, task, agreement, amounts, dates, products, risk_type.

При недоступности LLM — regex-эвристика для сумм, дат, @username.

#### Где ломается

1. **«150к за первый этап, 200к за второй»** — amounts = [150000, 200000]. Какой основной? Нет трекинга какой к чему этапу.

2. **«снизить на 50к»** — regex поймает 50000 как сумму. Но это дельта, не абсолютная сумма. Реальная сумма = 150000 - 50000 = 100000.

3. **«до пятницы»** — дата относительная. extract_heuristic не парсит относительные даты. LLM может вернуть «2026-04-03» если сегодня понедельник, но «пятница» может быть следующей.

4. **«Ивану из ООО Ромашка»** — contact.name = «Иван», company = «ООО Ромашка». Но в CRM может быть 5 Иванов из разных компаний.

5. **«Верни 50к»** — regex поймает 50000 как amounts. Это возврат, а не сделка. Нужна negation detection.

6. **«отсрочка до конца месяца»** — нет парсинга «конец месяца».

7. **Многоступенчатые суммы:** «Предоплата 50%, остальное через месяц» — 50% от чего? Сумма не указана явно.

8. **Смешанные валюты:** «$5000 или 450к руб» — extract не различает валюты.

### 3.5 Шаг 4: Запись в память (store)

#### Как работает

INSERT в memories + векторный эмбеддинг в vec_memories (одна транзакция) + auto_associate для поиска связей.

#### Где ломается

1. **Обрезка 200 символов.** `content=f"[{label}] {text[:200]}"` — если клиент написал длинное сообщение, теряется хвост. При recall LLM увидит обрубок без контекста.

2. **Content hash от полного текста, content в памяти = обрезанный.** Дедуп ищет по hash полного, а в memory хранится обрезанный. При поиске: memory вернёт `[lead-signal] Клиент согл...` — неполезно.

3. **Нет обратной ссылки на messages.** Memory говорит «Клиент согласился». Но из какого чата? Какого клиента? `chat_thread_id` и `contact_id` опциональны и могут быть NULL.

4. **auto_associate() открывает новое соединение.** Race condition: другой процесс может удалить memory пока auto_associate ищет похожие.

5. **embed_text при каждом store.** 10 сообщений подряд = 10 вызовов embed_text (11мс каждый = 110мс). При batch из 100 = 1.1с только на эмбеддинги.

6. **FK constraint крашит ingest.** Если contact_id не существует — `FOREIGN KEY constraint failed`. Нет try/except, нет graceful handling.

### 3.6 Шаг 5: Запись в CHAOS (chaos_store)

#### Как работает

INSERT в chaos_entries + векторный эмбеддинг в vec_chaos.

#### Где ломается

1. **Два хранилища — два разных контента.** Memory: `[lead-signal] Клиент согласился на 150к до пятницы`. CHAOS: `Клиент согласился на 150к до пятницы`. При recall: vector search вернёт и то, и другое. Ранжирование не знает, что это одно и то же.

2. **CHAOS vec_chaos может не создаться.** Если sqlite-vec упал — chaos_entry есть, вектора нет. При vector search запись невидима.

3. **Нет привязки chaos_entries к chat_thread.** Если два клиента сказали «согласен» — оба в CHAOS без привязки к чату.

4. **access_count растёт при каждом поиске.** chaos_search делает UPDATE при каждом чтении. При ночном consolidate поиск обновляет heat — искажает данные.

### 3.7 Шаг 6: CRM обновление (не существует)

#### Должно происходить

```
extract_entities()
  → contact_exists(tg_username)?
      YES → update contact (name, phones, company)
      NO  → create contact
  → deal_exists(contact_id, approximate_amount)?
      YES → update deal (stage, amount, notes)
      NO  → create deal
  → task_extracted?
      YES → create task (description, due_at, related_deal)
  → lead_extracted?
      YES → create/update lead (status, probability, amount)
```

#### Что сейчас

Ничего. Ingest пишет в memories и chaos, но НЕ создаёт контакты, сделки или задачи в CRM-таблицах.

Это **самая большая дыра**. Вся CRM-схема — декорация. Нет кода, который:
- Создаёт contact из extracted contact
- Создаёт deal из extracted lead/amount
- Создаёт task из extracted task
- Обновляет статус лида
- Связывает deal с messages

### 3.8 Шаг 7: Ответ клиенту (не существует)

#### Должно происходить

```
incoming_message
  → should_respond?(message, context)
      → YES → generate_response(context_packet)
      → NO  → silent processing only
  → send_response(chat_id, text)
```

#### Логика should_respond

| Ситуация | Отвечать? | Почему |
|----------|-----------|--------|
| Клиент задал вопрос | ✅ Да | Ожидает ответа |
| Клиент подтвердил сделку | ⚠️ maybe | Подтверждение «получил» |
| Клиент написал «привет» | ❌ Нет | Не нужно спамить |
| Клиент в группе, не обращается к боту | ❌ Нет | Не вмешиваться |
| Клиент написал задачу | ⚠️ maybe | «Принял, записал» |
| Клиент пожаловался | ✅ Да | Нужно реагировать |
| Неизвестный контакт | ⚠️ maybe | Возможно первый контакт |

#### Что сейчас

Нет ответов. Lenochka молча обрабатывает. Но продукт задуман как ассистент — он должен общаться.

### 3.9 Шаг 8: Утренний дайджест (digest)

#### Как работает

SQL-запросы к CRM-таблицам: новые лиды, просроченные задачи, брошенные диалоги, ключевые события.

#### Где ломается

1. **Timezone.** `datetime.now()` берёт локальное время сервера. Если сервер в UTC, а Камиль в GMT+8 — дайджест за 29 марта покажет данные с 29 марта 00:00 UTC = 08:00 GMT+8. Пропущены 8 часов.

2. **Нет данных в CRM-таблицах.** digest ищет leads, tasks, messages — но ingest туда не пишет. Дайджест будет пустым.

3. **v_overdue_tasks зависит от tasks.due_at.** Если task.due_at = NULL — не попадёт в overdue.

4. **Abandoned dialogues — m.from_user_id != 'self'.** Кто «self»? Нет определения owner_id.

5. **Нет отправки дайджеста.** generate_daily_digest() возвращает строку. Кто отправит её Камилю в Telegram?

### 3.10 Шаг 9: Консолидация (consolidate)

#### Как работает

Decay strength → merge дублей → cluster associations → build RAPTOR → cleanup слабых.

#### Где ломается

1. **O(n²) merge** — 500 записей × 250 пар = 125K вызовов similarity = 46 минут. При 2000 записей = 6 часов. При 10K = неделя.

2. **DELETE не чистит vec_memories.** После merge: vec_memories содержит мёртвые rowid. sqlite-vec при MATCH вернёт distance к удалённой строке. JOIN на memories вернёт NULL или ошибку.

3. **auto_associate вызывается для 100 записей.** Каждый вызов = новое соединение + 200 cosine comparisons. 100 × 200 × 11мс = 220с = 3.7 минуты.

4. **build_raptor — LLM-вызов на каждый batch.** При 100 memories / batch=8 = 13 вызовов LLM. При rate limit — 429.

5. **cleanup: `strength < 0.15 AND importance < 0.3`** — что если memory была importance=0.8 (решение), но 6 месяцев не использовалась → strength decay до 0.14? Решение удалится.

6. **Нет locking.** Если consolidate работает и в это время ingest пишет — SQLite WAL может дать stale read. Consolidate прочитает 500 записей, а пока мерджит — ingest добавил 10 новых. Новые не попадут в merge.

7. **Merge не обновляет vec_memories.** После удаления drop_id из memories — vec_memories.rowid = drop_id висит мёртвым. Нет `DELETE FROM vec_memories WHERE rowid = ?`.

---

## 4. Все уязвимые места

### 🔴 Критические (ломают данные или теряют информацию)

| # | Проблема | Где | Влияние | Сценарий |
|---|---------|-----|---------|----------|
| 1 | Нет concurrent access handling | store, chaos_store, auto_associate | Потеря данных | 5 сообщений одновременно → SQLITE_BUSY → потеря |
| 2 | Удаление memory не чистит vec + assoc | consolidate, prune | Мусор в поиске | После merge: vec вернёт удалённые записи |
| 3 | hash() не детерминирован | _embed_fallback | Сломан vector fallback | Разные эмбеддинги в разных процессах |
| 4 | ingest не пишет в CRM-таблицы | ingest | CRM пустая | 14 таблиц = декорация |
| 5 | messages и memories не связаны | ingest | Нет моста | Нельзя найти memory по message_id |
| 6 | Нет supersede для решений | store | Противоречивые данные | «150к» потом «120к» — оба в памяти |
| 7 | ingest не принимает не-text типы | ingest | Теряются данные | sticker, voice, photo, forward не обрабатываются |
| 8 | Emoji-подтверждения мисклассифицируются | classify | Потеря сделок | 👍 → chit-chat → пропуск |

### 🟡 Серьёзные (ухудшают качество, но не ломают)

| # | Проблема | Где | Влияние | Сценарий |
|---|---------|-----|---------|----------|
| 9 | O(n²) consolidate | consolidate | 46 мин / 500 записей | Ночной cron не успеет |
| 10 | auto_associate отдельное conn | brain.py | Race condition | Дубли ассоциаций |
| 11 | Несовместимые скоры в recall | recall | Нерелевантный ranking | BM25 rank=-2.5 > vector score=0.8 |
| 12 | Обрезка текста 200 символов | store, chaos_store | Потеря контекста | Long message → обрубок в memory |
| 13 | Timezone в дайджестах | digest, crm_daily_summary | Неправильные данные | UTC сервер + GMT+8 пользователь |
| 14 | Нет порога confidence | classify | Ложные лиды | conf=0.3 → всё равно пишет в memory |
| 15 | FK ошибки крашат ingest | store | Потеря сообщения | Несуществующий contact_id → краш |
| 16 | prune ломает FK | prune_messages | Неконсистентность | Удалённое сообщение → мёртвая ссылка |
| 17 | Дедуп не покрывает noise | ingest | Расход LLM | Одно noise 10 раз = 20 LLM-вызовов |
| 18 | Нет context window для classify | classify | Мисклассификация | «да» без контекста = chit-chat |

### 🟢 Непродуманности (ухудшают UX и операционность)

| # | Проблема | Где | Влияние |
|---|---------|-----|---------|
| 19 | Нет logging framework | везде | Нечитаемые логи |
| 20 | Хрупкий CLI парсер | mem.py main() | Multi-word текст ломается |
| 21 | Нет версии схемы БД | init.sql | Невозможные миграции |
| 22 | tags = JSON string | memories | Нечерезуемый столбец |
| 23 | Жёсткий importance | ingest | Все лиды = одинаковые |
| 24 | Нет лимита на размер текста | ingest | LLM API error на длинных |
| 25 | access_count растёт при поиске | chaos_search | Искажение heat-скора |
| 26 | Нет graceful shutdown | brain.py daemon | Коррупция WAL |
| 27 | Два хранилища — разный контент | store vs chaos_store | Дубли при recall |
| 28 | Нет rate limiting | ingest | Спам → OOM |

---

## 5. Непродуманные сценарии

### 5.1 Emoji-подтверждения

Эмодзи в Telegram — полноценные сообщения. Клиент может ответить 👍 вместо «да».

| Эмодзи | Значение в бизнесе | Классификация сейчас | Должна быть |
|--------|-------------------|---------------------|-------------|
| 👍 | «Да, согласен» | chit-chat | decision (подтверждение) |
| ✅ | «Выполнено, готово» | chit-chat | task-done (закрытие задачи) |
| ❌ | «Нет, отмена» | chit-chat | risk (отказ) |
| 💰 | «Деньги пришли» | chit-chat | payment-signal |
| 🔥 | «Срочно!» | chit-chat | priority-urgent |
| ❤️ | «Нравится, одобряю» | chit-chat | decision (положительный отклик) |
| 😂 | «Ха, шутишь» | chit-chat | НЕ подтверждение |
| 👌 | «Ок, понял» | chit-chat | business-small |
| 🤝 | «Договорились» | chit-chat | decision |
| 📅 | «Встреча» | chit-chat | task (запланировать) |
| ⏰ | «Напомни» | chit-chat | task (напоминание) |

**Решение:** Emoji-классификатор — отдельный слой перед LLM. Маппинг emoji → intent с учётом контекста предыдущего сообщения.

### 5.2 Групповые чаты

В групповом чате могут быть:
- Камиль (владелец бота)
- Клиент
- Бухгалтер клиента
- Партнёр Камиля
- Случайный участник

**Сценарии:**
1. Бухгалтер клиента пишет: «Я проверила, 120к ок» — это подтверждение от клиента? Или от третьего лица?
2. Камиль пишет клиенту, бот видит обе стороны — как определить, что Камиль не «клиент»?
3. В чате 5 человек, один пишет «согласен» — на что? На что он отвечает?
4. Клиент пересылает в общий чат сообщение от третьего лица — это его позиция?

**Решение:** Определение ролей участников (owner, client, third-party), трекинг message_thread, определение reply-target.

### 5.3 Переключение контекста

```
10:00 Клиент: «По поводу проекта А — согласен»
10:01 Клиент: «Кстати, по проекту Б — что с КП?»
10:02 Клиент: «Ладно, по А — пришли договор»
```

Три сообщения, два проекта. Ingest обработает их независимо. Нет трекинга «текущий разговор = проект А». Memory «согласен» не привяжется к проекту А, потому что проект не упомянут явно.

**Решение:** Session-level context — определение «о чём сейчас разговор» на основе последних N сообщений.

### 5.4 Многоступенчатые сделки

```
День 1: «Хочу обсудить проект» → lead-signal → lead created
День 3: «Согласен на концепцию» → decision → deal stage: proposal
День 5: «150к, предоплата 50%» → lead-signal → deal stage: negotiation
День 7: «Подпишем в четверг» → task → deal stage: contract
День 10: «Подписали!» → decision → deal stage: closed_won
```

Это один deal, но 5 разных memories. Нет механизма «эти 5 memories = один deal». Каждая memory создаётся независимо.

**Решение:** Deal linking — memories автоматически привязываются к active deal по contact_id + chat_thread_id. Stage detection по ключевым словам.

### 5.5 Неполные данные

| Сообщение | Проблема | Что нужно |
|-----------|----------|-----------|
| «Согласен» | На что? | Reply-контекст |
| «150к» | За что? С кем? | Предыдущее сообщение |
| «Пятница» | Какая пятница? | Today's date + relative parser |
| «Он сказал да» | Кто «он»? | Forward-контекст или предыдущее сообщение |
| «Готово» | Что готово? | Последняя задача для этого contact |
| «Подожди» | Сколько ждать? | Временный hold на deal |
| «Передай бухгалтеру» | Кому? | Резолвинг contact по роли |

### 5.6 Языковой хаос

| Пример | Проблема |
|--------|----------|
| «Ок, deal is on, 150к до Friday» | Смешанный язык |
| «Иван said ok» | Имя на русском, глагол на английском |
| «Звони в 3pm» | Время на английском |
| «КП ready?» | Русское сокращение + английское слово |
| «ок» / «ОК» / «окей» / «ОКЕЙ» / «Окей» | Разные написания |
| «150т» / «150к» / «150 тыс» / «150.000» | Разные форматы сумм |
| «до 3/4» / «до 3.04» / «до 03/04» | Разные форматы дат |

**Решение:** Мультиязычный prompt или preprocessing normalization.

### 5.7 Negation и изменения

| Сообщение | Ложное извлечение | Реальный смысл |
|-----------|-------------------|----------------|
| «Не 150, а 120» | amounts=[150, 120] | Только 120 (отмена 150) |
| «Снизить на 50к» | amounts=[50000] | Дельта, не абсолютная сумма |
| «Отменяю» | — | Инвалидация предыдущего решения |
| «Не актуально» | — | Deal → closed_lost |
| «Это дорого, максимум 100» | amounts=[100] | Counter-offer, не согласие |
| «Было 150, стало 180» | amounts=[150, 180] | Обновление, 180 актуально |

**Решение:** Negation detection + supersede logic — новое значение заменяет старое для того же contact+deal.

### 5.8 Timing и таймзона

| Сценарий | Проблема |
|----------|----------|
| Клиент пишет в 23:59 GMT+8 | Дайджест запишет в следующий день (UTC) |
| «К пятнице» в четверг вечером | Это завтра или через неделю? |
| «Через час» — когда именно? | Нужен absolute time от момента сообщения |
| «До конца месяца» — 28 или 31? | Зависит от месяца |
| Праздничный день — «в понедельник» | Следующий рабочий день? |

### 5.9 Медиа-контент

| Тип | Сценарий | Что нужно |
|-----|----------|-----------|
| Голосовое | Клиент говорит «да, 150к, к пятнице» | Транскрипция (Whisper/groq) |
| Фото счёта | «Вот реквизиты» | OCR |
| Скриншот переписки | «Он согласился» | OCR + attribution |
| PDF договор | «Подпиши» | PDF parsing |
| Видео-демо | «Посмотри что сделали» | Video understanding |
| Геолокация | [точка на карте] | Геокодинг → адрес |
| Контакт | [карточка Ивана] | Извлечение полей |
| Пересланное сообщение | [от Ивана] «Ок» | Attribution + контекст |

### 5.10 Краевые случаи CRM

| Сценарий | Проблема |
|----------|----------|
| Клиент сменил username | tg_username уникален → ошибка при втором contact |
| Один человек — два Telegram-аккаунта | Два contact, одна личность |
| Клиент в двух группах | Два chat_thread, один contact |
| Группа переименована | title изменился, tg_chat_id тот же |
| Клиент заблокировал бота | Нет уведомления, диалог «брошенный» навсегда |
| Бот добавлен в канал (read-only) | Может читать, но не отвечать |
| Супергруппа с топиками | message_thread_id = отдельный контекст |
| Клиент — это компания, пишет менеджер | Кто contact — компания или человек? |

---

## 6. 4 этапа развития

### ЭТАП 1: Крепкий фундамент (2 недели)

**Цель:** Сообщение из Telegram корректно доходит до CRM. Ни одно сообщение не теряется.

#### 1.1 Telegram-connector
- Принимает все типы сообщений: text, caption, voice, sticker, photo, video, document, contact, location, forward, reply
- Обрабатывает edited_message → обновляет существующую запись
- Обрабатывает message_thread_id (topics в супергруппах)
- Long polling или webhook
- Retry при ошибках Telegram API (429, 500, timeout)
- Queue: сообщения ставятся в очередь, не обрабатываются inline

#### 1.2 Normalize layer
- `extract_text(message)` — извлекает текст из любого типа:
  - `message.text` → как есть
  - `message.caption` → caption
  - `message.sticker` → emoji mapping (👍 → "подтверждаю", ✅ → "выполнено")
  - `message.voice` → заглушка (транскрипция в этапе 4)
  - `message.photo` → caption или заглушка (OCR в этапе 4)
  - `message.document` → filename + caption
  - `message.contact` → structured: "Контакт: {first_name} {phone}"
  - `message.location` → structured: "Локация: {lat}, {lon}"
- `resolve_reply(message)` — если reply_to_message, добавляет контекст: "[Ответ на: {original_text}] {current_text}"
- `resolve_forward(message)` — если forward_origin, определяет автора: "[Переслано от {author}] {text}"
- `resolve_contact(message.from_user)` — Telegram user → CRM contact (upsert)
- `normalize(text)` — приведение к единому формату (trim, fix encoding, normalize unicode)

#### 1.3 Message pipeline
- Каждое сообщение пишется в таблицу `messages` с `analyzed=false`
- Поля: chat_thread_id, from_user_id, text, sent_at, content_type, reply_to_msg_id, forward_from, raw_json
- `source_message_id` = Telegram message_id (уникален в рамках чата)

#### 1.4 Привязка ingest → messages
- Ingest получает `source_message_id` и `chat_thread_id`
- Пишет в memories с `source_message_id`
- После успешной обработки → `UPDATE messages SET analyzed=true WHERE id=?`
- Дедуп: проверять BOTH content_hash И source_message_id

#### 1.5 CRM upsert из ingest
```
extract_entities() →
  IF contact extracted:
    INSERT OR UPDATE contacts (tg_username, name, company)
  IF amount + contact:
    INSERT OR UPDATE deals (contact_id, amount, stage='discovery')
  IF task extracted:
    INSERT INTO tasks (description, due_at, related_contact, related_deal)
  IF lead-signal:
    INSERT OR UPDATE leads (contact_id, source='telegram', status='new', amount)
```

#### 1.6 Исправить hash fallback
- Заменить `hash(gram)` на `int(hashlib.sha256(gram.encode()).hexdigest()[:8], 16)`
- Детерминированный между запусками

#### 1.7 Supersede для сообщений
- edited_message → ищем memory по source_message_id → обновляем content + content_hash → пересчитываем эмбеддинг
- Не создаём новую memory, обновляем существующую

#### 1.8 Версия схемы + миграции
```python
def _migrate_db(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        conn.execute("ALTER TABLE memories ADD COLUMN content_hash TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash)")
        conn.execute("PRAGMA user_version = 2")
```

#### 1.9 Emoji-классификатор
```python
EMOJI_INTENT = {
    '👍': 'confirm', '👌': 'confirm', '🤝': 'confirm',
    '✅': 'done', '☑️': 'done',
    '❌': 'cancel', '🚫': 'cancel',
    '🔥': 'urgent', '⏰': 'reminder',
    '❤️': 'approve', '💯': 'approve',
    '💰': 'payment', '💵': 'payment',
    '📅': 'schedule',
}
```
Отдельный слой: если текст = 1-2 emoji → маппинг через EMOJI_INTENT с учётом контекста (reply на что?).

#### 1.10 Порог confidence
- Если `conf < 0.5` → пишем в memories с importance=0.3 + флаг `needs_review=true`
- Отдельная команда `mem.py review` — показывает непроанализированные
- В дайджесте секция «Требует внимания»

#### Deliverables этапа 1
- [ ] Telegram-бот принимает и нормализует все типы сообщений
- [ ] Каждое сообщение в messages таблице
- [ ] Ingest создаёт contacts, deals, tasks в CRM
- [ ] Emoji обрабатываются корректно
- [ ] Edited messages обновляют существующие записи
- [ ] Low-confidence сообщения не теряются
- [ ] Миграции схемы работают
- [ ] Hash fallback детерминирован

---

### ЭТАП 2: Интеллект и контекст (2 недели)

**Цель:** Система понимает контекст разговора и даёт релевантные результаты поиска.

#### 2.1 Context window для classify
- При ingest: загружаем N=5 последних сообщений из того же chat_thread
- Передаём в classify_message как chat_context
- «да» на «150к?» → decision. «да» на «как дела?» → chit-chat
- Формат: `"[10:00 Клиент: Могу сделать за 150к?] [10:01 → Текущее: Да]"`

#### 2.2 Consolidate через vec ANN (критично)
```python
# Вместо O(n²):
for memory in memories:
    neighbors = vec_search(memory.embedding, k=10)  # 0.24ms каждый
    for neighbor in neighbors:
        if sim > 0.85:
            merge(memory, neighbor)
# 500 записей × 10 соседей × 0.24мс = 1.2 секунда вместо 46 минут
```

#### 2.3 Удаление memory → чистить vec + associations
```python
def delete_memory(conn, memory_id):
    conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (memory_id,))
    conn.execute("DELETE FROM associations WHERE memory_id_from = ? OR memory_id_to = ?", (memory_id, memory_id))
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
```

#### 2.4 RRF для recall (Reciprocal Rank Fusion)
```python
def reciprocal_rank_fusion(results_by_source, k=60):
    scores = {}
    for source, results in results_by_source.items():
        for rank, result in enumerate(results):
            key = (result['source'], result['id'])
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```
Убирает проблему несовместимых шкал (vector 0-1 vs BM25 -10..0 vs keyword arbitrary).

#### 2.5 Batch embed_texts в consolidate
- Заменить N вызовов `embed_text()` на один `embed_texts_batch(all_contents)`
- sentence-transformers: 100 текстов за ~50мс (один forward pass)

#### 2.6 Supersede для решений
- Новое decision от того же contact → ищем предыдущий active deal
- Если новое решение contradicts старое → помечаем старое как `superseded_by=new_id`
- При recall: не возвращаем superseded записи (или помечаем)

#### 2.7 Deal linking
- При ingest: если extracted entity содержит amount + contact → привязываем memory к active deal
- Логика: ищем deal WHERE contact_id=? AND stage NOT IN ('closed_won','closed_lost')
- Если deal нет → создаём новый
- Если deal есть → обновляем (amount, stage)

#### 2.8 Relative date parser
```python
def parse_relative_date(text, reference_date):
    # «пятница» → ближайшая пятница
    # «следующий понедельник» → +7 дней до понедельника
    # «конец месяца» → last day of month
    # «через 3 дня» → reference + 3
    # «до НГ» → 31 декабря
    # «в среду» → ближайшая среда
```

#### 2.9 Negation detection
```python
NEGATION_PATTERNS = [
    (r'не\s+(\d+)', 'cancel_amount'),       # «не 150»
    (r'отмен[яа]\w*', 'cancel'),            # «отменяю»
    (r'не актуальн\w*', 'invalidate'),       # «не актуально»
    (r'снизить на\s+(\d+)', 'reduce_by'),    # «снизить на 50к»
    (r'максимум\s+(\d+)', 'cap_at'),         # «максимум 100»
]
```

#### 2.10 FTS на memories в recall
- В `recall()` strategy="hybrid" — добавить memories_fts поиск как второй источник
- Сейчас memories_fts существует (триггеры работают), но recall не использует его

#### Deliverables этапа 2
- [ ] Context-aware классификация (reply-контекст)
- [ ] Consolidate за <5 секунд (vec ANN)
- [ ] Корректное удаление memory (vec + assoc)
- [ ] RRF-ранжирование в recall
- [ ] Supersede для решений
- [ ] Deal linking из ingest
- [ ] Relative date parser
- [ ] Negation detection
- [ ] Memories FTS в recall

---

### ЭТАП 3: Операционность (2 недели)

**Цель:** Система работает в production, Lenochka разговаривает с пользователями.

#### 3.1 Daemon mode
- brain.py как long-running процесс
- Модель загружается один раз при старте
- Communication через unix socket или HTTP API
- CLI mem.py → тонкий клиент к демону
- Hot reload модели без перезапуска

#### 3.2 Response engine
```python
def should_respond(message, context):
    # Отвечать если:
    #   - Прямой вопрос к боту
    #   - @mention бота
    #   - Клиент пожаловался (risk)
    #   - Новый лид (приветствие)
    #   - Просьба подтвердить
    # Не отвечать если:
    #   - Casual chat между людьми
    #   - Бот уже ответил недавно
    #   - Сообщение в канале (read-only)
    #   - Группа > 10 человек (не вмешиваться)

def generate_response(context_packet, intent):
    # Тон зависит от:
    #   - Кто пишет (клиент = формально, коллега = casual)
    #   - Что произошло (лид = приветствие, жалоба = сочувствие)
    #   - Контекст чата (личный = свободно, группа = кратко)
```

#### 3.3 Contact resolution
- `resolve_contact(tg_user)` → ищет по tg_username → обновляет если нашёл → создаёт если нет
- Маппинг: Telegram user_id ↔ CRM contact.id
- Обработка: сменил username, второй аккаунт, один на два чата

#### 3.4 Group chat policy
- Определение роли каждого участника (owner, client, third-party)
- Не обрабатывать сообщения от самого себя (бота)
- Не отвечать в группах > N человек без mention
- Трекинг message_thread_id для супергрупп с топиками

#### 3.5 Logging framework
```python
import logging
logger = logging.getLogger('lenochka')
# Levels: DEBUG (classify details), INFO (store/recall), WARN (fallback), ERROR (failures)
# Format: timestamp | level | component | message | context
# Output: stderr + rotating file handler
```

#### 3.6 Digest delivery
- Утренний дайджест → отправляется Камилю в Telegram
- Cron: daily 08:00 GMT+8
- Недельный: Sunday 18:00 GMT+8
- Формат: короткие секции, bullet points, actionable items
- Форматирование для Telegram (HTML/Markdown)

#### 3.7 Manual override команды
- `/reclassify <msg_id>` — переклассифицировать сообщение
- `/correct <field> <value>` — исправить извлечённую сущность
- `/status` — текущие активные сделки, задачи
- `/leads` — список новых лидов
- `/tasks` — список задач
- `/find <query>` — поиск по памяти

#### 3.8 Archived messages handling
- `prune_messages()` → soft delete (поле `archived=true`) вместо hard DELETE
- Cascade: memories и chaos_entries не ссылаются на архивные messages
- Отдельный cron для физического удаления старых архивов (>1 года)

#### 3.9 Transaction retry
```python
def get_db(max_retries=3):
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
            else:
                raise
```

#### 3.10 Alert system
- Low-confidence сообщения → уведомление Камилю
- Мисклассификация (пользователь исправил) → лог для retraining
- Ошибки LLM (timeout, 429) → alert если > N подряд
- DB corruption → emergency alert

#### Deliverables этапа 3
- [ ] Daemon mode работает
- [ ] Lenochka отвечает на сообщения
- [ ] Contact resolution работает
- [ ] Group chat policy
- [ ] Structured logging
- [ ] Дайджест отправляется в Telegram
- [ ] Manual override команды
- [ ] Soft delete вместо hard delete
- [ ] Transaction retry
- [ ] Alert system

---

### ЭТАП 4: Масштаб и надёжность (3 недели)

**Цель:** Система выдерживает реальную нагрузку, нескольких пользователей, медиа-контент.

#### 4.1 Multi-user
- Новое поле `user_id` в каждой таблице (owner бота)
- Изоляция данных: каждый пользователь видит только свои данные
- Таблица `bot_users` (user_id, tg_user_id, plan, settings)
- Multi-tenant: один бот, N пользователей, изолированные БД или row-level security

#### 4.2 Voice transcription
- Интеграция с Whisper API или Groq Whisper
- При получении voice message → download → transcribe → передать текст в ingest
- Кэширование транскрипции (не пересказывать одно и то же)
- Мультиязычный Whisper (auto-detect language)

#### 4.3 OCR
- Интеграция с Tesseract или cloud OCR (Google Vision, Mistral)
- При получении photo с caption или без → OCR → передать текст в ingest
- PDF: extract text через pymupdf
- Скриншоты переписок: OCR + attribution (кто говорит)

#### 4.4 Batch LLM classify
- Вместо N вызовов classify_message() → один вызов на N сообщений
- System prompt: "Классифицируй каждое сообщение. Верни JSON array."
- 10 сообщений в одном вызове: ~60% меньше токенов

#### 4.5 Connection pool / single-writer
- Один writer-процесс (daemon), N reader-процессов
- SQLite WAL: readers не блокируются
- Write queue: все записей через очередь в daemon

#### 4.6 Message queue
- Ingest через очередь (Redis, встроенный queue, или файловая)
- Telegram handler → queue → worker (daemon)
- Не блокировать Telegram webhook при LLM-вызове
- Retry с exponential backoff при ошибках ingest

#### 4.7 Monitoring / metrics
- Latency: ingest time (p50, p95, p99)
- Accuracy: % правильных классификаций (spot-check)
- Cost: LLM API расход в день
- Volume: сообщений в день, memories в день
- Health: DB size, vec table size, FTS index size

#### 4.8 Backup / export
- Ежедневный backup SQLite (file copy в WAL-mode)
- Export: JSON dump всех данных (для миграции, GDPR)
- Import: загрузка из backup
- Retention: auto-delete старые backups (>30 дней)

#### 4.9 Rate limiting
- Максимум N ingest в секунду (защита от спама в группе)
- LLM rate limit: queue + exponential backoff
- Telegram rate limit: respect 429 + Retry-After

#### 4.10 Integration tests
```python
# End-to-end тесты:
def test_ingest_creates_contact_and_deal():
    # 1. ingest("Клиент Иван согласился на 150к")
    # 2. assert contact exists with name="Иван"
    # 3. assert deal exists with amount=150000
    # 4. assert memory exists with label="lead-signal"
    # 5. assert chaos_entry exists with category="lead-signal"

def test_edited_message_updates_memory():
    # 1. ingest("120к") → memory_id=1
    # 2. ingest_edited("150к", source_message_id=...) → memory_id=1 обновлён
    # 3. assert memory.content contains "150к"
    # 4. assert memory.content NOT contains "120к"

def test_consolidate_doesnt_corrupt_vectors():
    # 1. store 10 memories
    # 2. consolidate()
    # 3. assert vec_memories count == memories count (after merge cleanup)
    # 4. assert recall returns valid results
```

#### Deliverables этапа 4
- [ ] Multi-user изоляция
- [ ] Voice transcription
- [ ] OCR для фото
- [ ] Batch LLM classify
- [ ] Connection pool
- [ ] Message queue
- [ ] Monitoring dashboard
- [ ] Backup system
- [ ] Rate limiting
- [ ] Integration tests

---

## Итого: критический путь

```
Сегодня ───────────────────────────────────────────────────
│ Библиотека: mem.py + brain.py умеют хранить и искать
│
▼
Этап 1 (2 недели) ────────────────────────────────────────
│ Telegram-бот + normalize + CRM upsert + emoji + supersede
│ Результат: сообщение из Telegram → данные в CRM
│
▼
Этап 2 (2 недели) ────────────────────────────────────────
│ Context window + vec ANN + RRF + deal linking + dates
│ Результат: система понимает контекст, поиск релевантный
│
▼
Этап 3 (2 недели) ────────────────────────────────────────
│ Daemon + ответы + команды + дайджест + логирование
│ Результат: Lenochka — работающий продукт
│
▼
Этап 4 (3 недели) ────────────────────────────────────────
│ Multi-user + voice + OCR + batch + queue + тесты
│ Результат: масштабируемая платформа
│
▼
Готово ────────────────────────────────────────────────────
```

**Общий срок: ~9 недель.**

**Самое острое непродуманное место сегодня:** Response engine — Lenochka молча обрабатывает, но не отвечает пользователям. CRM-upsert мост уже реализован (crm_upsert.py), но нет генерации ответов.

**Самый опасный баг сегодня:** concurrent access. Если два ingest() работают одновременно — SQLite locked → потеря сообщения. WAL помогает читать, но writer contention остаётся.

**Самый дорогой недочёт сегодня:** pipeline queue не персистентен — рестарт бота = потеря in-flight сообщений из очереди.

---

## 7. ОБНОВЛЕНИЯ (сессия 2026-03-30 02:16 — 02:59)

### Что сделано

1. **6 критических фиксов** (коммит 54aac7e):
   - LLM config unified: `brain.py` + `config.py` читают `LEN_LLM_*` (единый префикс)
   - `store()` и `chaos_store()` — транзакционные (try/rollback)
   - `OwnerMiddleware` — проверяет владение, инжектирует `is_owner`
   - Direct messages НЕ пишут в CRM pipeline
   - Supersede cascade — обновляет memories + chaos при edited messages
   - Установлены sqlite-vec 0.1.7 + sentence-transformers 5.3.0

2. **Анализ графового RAG:**
   - Проведён глубокий анализ: нужен ли graph RAG для масштаба Lenochka
   - Вывод: НЕ нужен. ~15K memories/год = крошечный масштаб
   - vec ANN + FTS5 + FK-кластеризация уже покрывают основные сценарии
   - Graph RAG (NetworkX, Neo4j) = overkill: +500 строк, +dependency, diminishing returns
   - Замена: entity-aware context expansion (FK traversal) — 80% пользы при 5% сложности

3. **Entity-aware context expansion** (коммиты c81a2f4, 54ec112):
   - `_expand_entity_context()` — traversal по реальным FK-связям:
     ```
     memory → contact (кто клиент) → deal (сумма, стадия) → tasks (что делать)
     contact/deal → другие memories (история) → chat_thread → сообщения (контекст)
     ```
   - Интегрировано в 4 точки:
     - `recall()` — добавляет `_expansion` в результаты
     - `build_context_packet()` — contacts/deals как facts, tasks/history как notes
     - `/find` command — показывает блок «Связанный контекст» (кто, что, задачи, история)
     - `pipeline._finalize_item()` — LLM при extract_entities получает enriched context

### Что изменилось в архитектуре

```
ДО (сессия 01:04):
  message → normalize → classify → extract(plain text) → store → CRM upsert

ПОСЛЕ (сессия 02:59):
  message → normalize → classify → extract(+entity context: existing contact/deals/tasks)
            → store (transactional) → CRM upsert
            → supersede (cascade: messages + memories + chaos)
            → /find shows entity chain (contact → deal → tasks → history → chat)
```

### Влияние на план (4 этапа)

| Что | Было в плане | Статус сейчас |
|-----|-------------|---------------|
| Phase 1: Telegram-connector | Не было кода | ✅ Реализован |
| Phase 1: Normalize layer | Не было кода | ✅ Реализован |
| Phase 1: CRM upsert | Не было кода | ✅ Реализован |
| Phase 1: Supersede | Не было кода | ✅ Реализован + cascade |
| Phase 1: Emoji classifier | Не было кода | ✅ Реализован (normalizer.py) |
| Phase 2: Context window classify | Не было | ✅ Реализован (pipeline._get_chat_context) |
| Phase 2: Consolidate vec ANN | Не было | ✅ Реализован |
| Phase 2: Deal linking | Не было | ⚠️ Частично (entity expansion показывает deal, но нет auto-linking) |
| Phase 2: Entity expansion | Не было | ✅ Реализован (NEW — не было в плане) |
| Phase 3: Daemon mode | Не было | ✅ Реализован (brain_wrapper) |
| Phase 3: Response engine | Не было | ❌ Не реализован (← запись до сессии 14:50) |
| Phase 3: Response engine v2 | Не было | ✅ Реализован: combined classify+route, fact-based response (11 SQL intents), escalation с timers + night mode, progress check-in LLM, follow-up detection, ResponseGuard anti-loop, proactive owner alerts + client reminders + progress check-in. Файлы: response_engine.py, fact_queries.py, notifier.py, proactive.py |
| Phase 3: Digest delivery | Не было | ⚠️ Генерация есть, отправки в Telegram нет |
| Phase 3: Logging | Не было | ✅ Реализован |
