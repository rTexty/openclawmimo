# Lenochka — Issues & Bugs Audit

> Дата: 2026-03-30 03:09 GMT+8
> Аудитор: Леночка (session 02:16—03:09)
> Статус: Все файлы прочитаны, код проанализирован

---

## 🔴 Критические (ломают данные или логику)

### ISSUE-01: `_embed_fallback` использует `hash()` — нодетерминированный

**Файл:** `lenochka-memory/brain.py:88`
```python
h = hash(gram) % EMBEDDING_DIM
```

**Что происходит:**
Python `hash()` начиная с 3.3 солится рандомным seed при старте процесса (защита от DoS). `hash("привет")` даёт разные числа при каждом запуске.

**Почему сломается:**
Fallback-эмбеддинги используются когда `sentence-transformers` недоступен (не установлен, OOM, ошибка загрузки модели). В этом случае:
- `embed_text("согласен на 150к")` → вектор A
- Перезапуск бота → тот же текст → вектор B (полностью другой)
- `vec_memories` хранит вектора от прошлого запуска
- `recall()` ищет новым вектором по старым → рандомный шум

**Сейчас работает потому:** sentence-transformers установлен и fallback не вызывается.

**Влияние при активации:** Полностью сломанный векторный поиск. Результаты recall — случайные.

**Фикс:** Заменить `hash(gram)` на `int(hashlib.sha256(gram.encode()).hexdigest()[:8], 16)` — детерминированный.

---

### ISSUE-02: noise/chit-chat content_hash не сохраняется → дубли LLM-вызовов

**Файлы:** `lenochka-bot/services/pipeline.py:_finalize_item`, `lenochka-memory/mem.py`

**Что происходит:**
1. Сообщение «привет» → classify → `chit-chat`
2. Pipeline: label = chit-chat → НЕ пишет в memories (только business-типы)
3. `content_hash` существует только в `dedup_check` (in-memory, не в БД)
4. Приходит тот же «привет» через час → `dedup_check` ищет `content_hash` в `memories` → не находит → пропускает дедуп
5. Опять classify (LLM-вызов) + опять extract (LLM-вызов) = 2 лишних вызова

**Масштаб:** 500 msg/day × 10% noise/chit-chat × 50% повторы = 25 дублей × 2 LLM = 50 лишних вызовов/день.

**Почему не пофикшен:** В BLUEPRINT.md записано как известная проблема ещё в первой сессии.

**Фикс:** Сохранять `content_hash` для ВСЕХ сообщений (не только важных). Либо в отдельной таблице `dedup_hashes`, либо в `messages.content_hash` column.

---

### ISSUE-03: Supersede re-process → дубли memories и chaos_entries

**Файлы:** `lenochka-bot/services/pipeline.py:_normalize_and_store`, `lenochka-bot/services/memory.py:supersede_message`

**Что происходит:**
1. Клиент пишет «120к» → pipeline: classify → `lead-signal` → memory #1 создан, chaos #1 создан
2. Клиент редактирует на «150к» → Telegram шлёт `edited_business_message`
3. `supersede_message()`:
   - Находит messages строку по `source_msg_id`
   - Обновляет `messages.text` на «150к»
   - Сбрасывает `analyzed=0`
   - Cascade: обновляет `memories.content` и `chaos_entries.content` ← работает
4. Pipeline переклассифицирует (analyzed=0):
   - `classify_batch` → `lead-signal`
   - `_finalize_item` → `store_memory()` → создаёт **новую** memory #2
   - `_finalize_item` → `chaos_store()` → создаёт **новый** chaos #2
5. Результат: memory #1 (обновлённый контент) + memory #2 (новый) = дубль

**Два механизма конфликтуют:** supersede cascade обновляет существующие записи, а pipeline создаёт новые при re-process. Не синхронизированы.

**Почему сломается:** Каждое редактирование = +1 memory +1 chaos_entry мусора. Со временем БД растёт быстрее чем нужно.

**Фикс:** В `_finalize_item` при re-process (source = business_edited) — НЕ создавать новые memory/chaos, а только обновить classification в messages. Или перед store_memory проверить: если уже есть memory с этим `source_message_id` — обновить, не создавать.

---

### ISSUE-04: JSON regex `\{[^}]+\}` ломается на вложенных объектах

**Файлы:** `lenochka-memory/brain.py:360` (classify_message), `brain.py:400` (extract_entities)
```python
json_match = re.search(r'\{[^}]+\}', result, re.DOTALL)
```

**Что происходит:**
LLM возвращает:
```json
{"contact": {"name": "ООО «Ромашка»", "tg_username": "ivan"}, "deal": {"amount": 150000}}
```
Regex `\{[^}]+\}` ищет от `{` до первого `}` без вложенных `}`. Находит только внутренний объект:
```json
{"name": "ООО «Ромашка»", "tg_username": "ivan"}
```
Это не полный ответ — теряются `contact`, `deal`, amounts.

**Для batch classify** то же:
```python
json_match = re.search(r'\[.*\]', result, re.DOTALL)  # greedy через DOTALL
```
Если LLM добавит пояснение после JSON: `[...] Объяснение: ...` — greedy захватит лишнее.

**Почему сломается:** LLM иногда возвращает вложенные JSON-объекты. Особенно при извлечении contact (contact.name, contact.company) или deal (deal.amount, deal.stage). Regex найдёт неполный fragment → `json.loads` упадёт → fallback на heuristic (который не извлекает contact/deal).

**Фикс:** Использовать proper JSON extraction — найти matching braces по depth, или попросить LLM форматировать без вложенности, или использовать `json.JSONDecoder.raw_decode`.

---

## 🟡 Серьёзные (ухудшают качество, но не ломают критично)

### ISSUE-05: `_get_chat_context` deleted check хрупкий

**Файл:** `lenochka-bot/services/pipeline.py:340`
```sql
WHERE chat_thread_id = ? AND (meta_json IS NULL OR meta_json NOT LIKE '%"deleted": 1%')
```

**Проблема:** `json_set(meta_json, '$.deleted', 1)` записывает `"deleted": 1` (с пробелом). Проверка `LIKE '%"deleted": 1%'` ищет точную строку. Если SQLite/версия json_set запишет `"deleted":1` (без пробела) — check промахнется.

**Что произойдёт:** Удалённые сообщения вернутся в контекст классификации. LLM увидит удалённый текст и будет классифицировать несуществующие данные.

**Фикс:** Использовать `json_extract(meta_json, '$.deleted') IS NOT 1` вместо LIKE.

---

### ISSUE-06: `_call_llm` импортирует `requests` на каждый вызов

**Файл:** `lenochka-memory/brain.py:132`
```python
def _call_llm(system_prompt, user_prompt, ...):
    import requests  # ← внутри функции
```

**Проблема:** Python кэширует импорты, но lookup `sys.modules['requests']` на каждый вызов. Если `requests` не установлен — ImportError на каждом вызове (а не при старте).

**Влияние:** Не критично, но: 1) нет раннего предупреждения о зависимости, 2) traceback при ImportError будет внутри функции, а не при запуске.

**Фикс:** Вынести `import requests` на уровень модуля.

---

### ISSUE-07: Contact lookup по `notes LIKE` — fragile и медленный

**Файл:** `lenochka-bot/services/contact_resolver.py:55`
```python
existing = conn.execute(
    "SELECT id FROM contacts WHERE notes LIKE ?", (f"%tg_id:{tg_id}%",)
).fetchone()
```

**Проблемы:**
- Нет индекса на `notes` → full table scan на каждый message
- `%tg_id:123%` может матчить `tg_id:1234` (лишний символ после)
- `%tg_id:123%` может матчить `old_tg_id:123` (лишний символ перед)
- Если у клиента username=NULL — ищем только по notes, fragile

**Что произойдёт:** Неправильный contact_id → memories привяжутся не к тому клиенту. Или дубли contact при каждом сообщении.

**Фикс:** Добавить column `tg_user_id TEXT UNIQUE` в contacts. Хранить Telegram user_id явно, не в notes.

---

### ISSUE-08: `_upsert_deal` — `max(amounts)` не понимает контекст

**Файл:** `lenochka-bot/services/crm_upsert.py:65`
```python
amount = max(amounts)  # Берём максимальную
```

**Сценарии где ломается:**
| Сообщение | amounts | Что запишется | Реальный смысл |
|-----------|---------|---------------|----------------|
| «Снизить на 50к» | [50000] | deal 50,000₽ | Дельта — уменьшение |
| «Предоплата 50%» | [50] | deal 50₽ | Процент, не сумма |
| «150к за первый, 200к за второй» | [150000, 200000] | deal 200,000₽ | Два этапа, не макс |
| «Верни 50к» | [50000] | deal 50,000₽ | Возврат, не сделка |
| «Было 150, стало 120» | [150000, 120000] | deal 150,000₽ | 120 актуальна |

**Фикс:** Negation detection + delta handling в extract или в crm_upsert. Или LLM извлекает structured amount с контекстом.

---

### ISSUE-09: Digest использует `datetime.now()` — серверное время, не GMT+8

**Файл:** `lenochka-memory/brain.py` (generate_daily_digest)

**Проблема:** `datetime.now()` берёт локальное время сервера. Если сервер в UTC:
- Дайджест за "сегодня" = данные с 00:00 UTC
- Камиль в GMT+8, его "сегодня" = данные с 16:00 UTC предыдущего дня
- 8 часов данных пропущены в дайджесте

**Фикс:** Использовать `datetime.now(timezone(timedelta(hours=8)))` или передавать timezone как параметр из config.

---

### ISSUE-010: Двойное создание contact — `_upsert_contact` + `_upsert_entity_contact`

**Файлы:** `lenochka-bot/services/contact_resolver.py`, `lenochka-bot/services/crm_upsert.py`

**Что происходит:**
1. Pipeline вызывает `resolve_contact(msg, ...)` → `_upsert_contact` создаёт contact из Telegram user: "Иван Иванов (@ivan_co)"
2. Pipeline вызывает `crm_upsert(entities, contact_id, ...)` → если `entities["contact"]` есть (LLM извлек контакт) → `_upsert_entity_contact` создаёт **ещё одного**: "ООО Ромашка, Иван"

Результат: два contact в БД на одно лицо.

**Проблема:** `crm_upsert` принимает `contact_id` от `resolve_contact`, но внутри проверяет `if entities.get("contact") and not contact_id` — если contact_id есть, не создаёт дубль. Но если `resolve_contact` вернул `None` (нет from_user) → `_upsert_entity_contact` создаст нового. При следующем сообщении `resolve_contact` создаст своего. Два contact.

**Фикс:** В `_upsert_entity_contact` — искать существующий contact по tg_username перед созданием. Или передавать уже существующий contact_id в crm_upsert всегда.

---

## 🟢 Мелочи / UX / Tech Debt

### ISSUE-011: Batch classify — LLM может вернуть меньше элементов

Если LLM вернёт `[{"label":"task"}]` на 5 сообщений — `len(data) != len(texts)` → fallback на heuristic для ВСЕХ 5. LLM-вызов потрачен зря.

**Фикс:** Парсить partial response — взять столько элементов сколько есть, остальные через heuristic.

---

### ISSUE-012: `chaos_search` обновляет `access_count` при каждом чтении

Nightly consolidate, digest, и прямой поиск — все обновляют `access_count` + `last_accessed_at`. Это искажает heat-скор. Поиск != доступ.

**Фикс:** Разделить "read" и "access". Обновлять heat только при явном доступе (чтение для ответа LLM), не при фоновом поиске.

---

### ISSUE-013: `get_open_tasks` и `get_active_leads` — hardcoded LIMIT 10

Нет пагинации. Если 15 срочных задач — 5 не покажутся. Нет параметра `limit` в функциях.

**Фикс:** Добавить параметр `limit`.

---

### ISSUE-014: `_expand_entity_context` — conn leak при ошибке до try

Если `conn = get_db()` вызван, но ошибка произойдёт между `get_db()` и `try:` — conn не закроется. Маловероятно (всего 1 строка между ними), но архитектурно неправильно.

**Фикс:** Обернуть `get_db()` в try.

---

### ISSUE-015: Нет миграций схемы для существующих БД

`mem.py:init()` проверяет `if DB_PATH.exists()` → "БД уже существует" и не обновляет. Если БД создана до добавления `analyzed`, `source_msg_id`, `business_connections` — новые колонки не появятся.

**Фикс:** Добавить `_migrate_db(conn)` с `PRAGMA user_version` и `ALTER TABLE`.

---

### ISSUE-016: Нет `requirements.txt` для `lenochka-memory/`

`lenochka-bot/requirements.txt` описывает зависимости бота. Но `lenochka-memory/` (mem.py, brain.py) зависит от `sentence-transformers`, `sqlite-vec`, `numpy`, `requests` — нигде не документировано.

**Фикс:** Создать `lenochka-memory/requirements.txt` или общий `requirements.txt` в корне.

---

## Сводка

| Серьёзность | # | ID |
|---|---|---|
| 🔴 Critical | 4 | ISSUE-01, ISSUE-02, ISSUE-03, ISSUE-04 |
| 🟡 Serious | 6 | ISSUE-05, ISSUE-06, ISSUE-07, ISSUE-08, ISSUE-09, ISSUE-010 |
| 🟢 Minor | 6 | ISSUE-011, ISSUE-012, ISSUE-013, ISSUE-014, ISSUE-015, ISSUE-016 |

**Самый опасный:** ISSUE-03 (supersede duplicates) — каждое редактирование = +2 мусорных записи в БД.

**Самый лёгкий фикс:** ISSUE-01 (hash fallback) — замена одной строки.

**Самый дорогой:** ISSUE-02 (noise dedup) — требует schema change (content_hash column в messages или новая таблица dedup).
