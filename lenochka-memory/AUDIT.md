# Lenochka Memory v2 — Аудит системы

_Дата: 2026-03-29_
_Статус: pre-publish review_

---

## Сводная таблица

| # | Категория | Проблема | Влияние | Сложность фикса |
|---|-----------|----------|---------|-----------------|
| 1 | 🔴 Крит | Холодный старт модели (6.6с на каждый вызов CLI) | UX / Latency | Средняя |
| 2 | 🔴 Крит | store() два отдельных COMMIT | Целостность данных | Лёгкая |
| 3 | 🔴 Крит | Нет дедупликации ingest | Дубли в памяти | Лёгкая |
| 4 | 🔴 Крит | consolidate() O(n²) = 46 мин на 500 записей | Performance | Средняя |
| 5 | 🟡 Средн | 2 LLM-вызова на сообщение (classify + extract) | Cost | Средняя |
| 6 | 🟡 Средн | ingest теряет тип в CHAOS (task/lead → event) | Поиск / Контекст | Лёгкая |
| 7 | 🟡 Средн | _call_llm нет retry / backoff | Надёжность | Лёгкая |
| 8 | 🟡 Средн | Нет batch-ингеста (100 сообщений = 200 LLM-вызовов) | Cost / Latency | Средняя |
| 9 | 🟡 Средн | ~400MB RAM заняты моделью постоянно | Ресурсы | Средняя |
| 10 | 🟢 Мелоч | auto_associate не закрывает conn при исключении | Утечки | Лёгкая |
| 11 | 🟢 Мелоч | context packet — нет cross-source re-ranking | Качество | Средняя |
| 12 | 🟢 Мелоч | Нет batch embed_texts() | Performance | Лёгкая |
| 13 | 🟢 Мелоч | Нет fingerprint/hash в messages | Дедуп на уровне CRM | Лёгкая |
| 14 | 🟢 Мелоч | FTS MATCH может кидать исключение (bad syntax) | Надёжность | Лёгкая |
| 15 | 🟢 Мелоч | Нет graceful shutdown / signal handling | Надёжность | Лёгкая |

---

## Детальный разбор

---

### 1. 🔴 Холодный старт модели эмбеддингов — 6.6 секунд

**Что происходит:**
Каждый вызов `mem.py` как CLI-команды — это отдельный процесс Python. При импорте `brain.py` вызывается `_get_embed_model()`, который загружает `sentence-transformers/all-MiniLM-L6-v2` (~90MB on disk, ~400MB в RAM). Это занимает 6.6 секунд.

**Примеры тормозов:**
```
python3 mem.py store "текст"      → 6.6с (модель) + 0.01с (эмбеддинг) = 6.61с
python3 mem.py recall "запрос"    → 6.6с (модель) + 0.001с (vec search) = 6.60с
python3 mem.py ingest "текст"     → 6.6с (модель) + 0.01с (эмбеддинг) = 6.61с
```

**В контексте Telegram-бота:** если Lenochka обрабатывает сообщения через CLI-вызовы (как планировалось), каждое сообщение = 6.6 секунды ожидания. При 10 сообщениях в минуту — бот будет постоянно отставать.

**После загрузки:** эмбеддинг одного текста = 11мс, что нормально.

**Решения:**
- **(A) Серверный режим (daemon):** `brain.py` работает как long-running процесс, принимает запросы через unix socket / HTTP. Модель загружается один раз. CLI `mem.py` общается с демоном.
- **(B) Кэширование модели:** Сохранять модель в постояннодоступном месте и использовать `torch.jit.script` или ONNX для быстрой загрузки (~1-2с).
- **(C) Лёгкая модель:** Заменить на `paraphrase-multilingual-MiniLM-L12-v2` или `all-MiniLM-L12-v2` с quantization (ONNX, 50MB, ~1с загрузка).

**Рекомендация:** (A) серверный режим — самое чистое решение. Модель живёт в памяти демона, CLI = тонкий клиент.

---

### 2. 🔴 store() два отдельных COMMIT — нет транзакционности

**Что происходит:**
```python
# mem.py, store()
conn.execute("INSERT INTO memories ...")
conn.commit()           # ← COMMIT #1

vec = embed_text(content)
conn.execute("INSERT INTO vec_memories ...")
conn.commit()           # ← COMMIT #2
```

Если между COMMIT #1 и COMMIT #2 процесс упадёт (OOM при эмбеддинге, segfault sqlite-vec, kill -9), в БД останется memory без вектора. При последующем vec-поиске эта запись будет невидимой.

**Масштаб проблемы:** Низкий в разработке, средний в production при высоком трафике. Особенно если сервер нестабилен (OОМ-killer, перезагрузки).

**Решение:** Обернуть в одну транзакцию:
```python
conn.execute("INSERT INTO memories ...")
vec = embed_text(content)
conn.execute("INSERT INTO vec_memories ...")
conn.commit()  # один COMMIT на обе операции
```

**Сложность:** Лёгкая. Нужно просто переставить `commit()`.

---

### 3. 🔴 Нет дедупликации ingest

**Что происходит:**
`ingest()` не проверяет, было ли уже обработано это сообщение. Если Telegram доставит сообщение дважды (retry, перезапуск long polling), в БД будут два одинаковых memory.

**Сценарии дублей:**
- Telegram retry при сбое доставки
- Перезапуск бота → повторная обработка батча
- Ручной вызов ingest дважды (отладка)
- Несколько чатов с одним клиентом → одно и то же сообщение в разных chat_thread

**Масштаб:** При 500 msg/day и 1% дублей = 5 лишних записей/день = 1800/год. Не критично, но засоряет память и искажает дайджесты.

**Решения:**
- **(A) Content fingerprint:** Хэш контента + chat_thread_id + sent_at. Проверять перед ingest.
- **(B) source_message_id:** Telegram message_id уникален в рамках чата. Использовать как естественный ключ.
- **(C) fuzzy dedup:** Если similarity(text, existing) > 0.95 за последние 24 часа — пропустить.

**Рекомендация:** (A) + (B). Content hash для общей дедупликации, source_message_id для Telegram-специфичной.

---

### 4. 🔴 consolidate() — O(n²) brute-force = 46 минут

**Что происходит:**
```python
for i, m1 in enumerate(memories):       # 500 memories
    for m2 in memories[i+1:]:            # ~250 в среднем
        sim = similarity(m1, m2)         # 2 эмбеддинга
```

500 × 250 / 2 = 62,500 пар × 2 эмбеддинга = 125,000 вызовов `embed_text`. При 11мс/вызов = **1,375 секунд = 23 минуты** (без учёта model load). С model load и overhead — ~46 минут.

**При 2000 memories:** O(n²) = 2M пар = ~6 часов. Полностью непрактично.

**Решения:**
- **(A) ANN через sqlite-vec:** Для каждого memory искать 10 ближайших соседей через векторный поиск. 500 × 1 vec query = 500 × 0.24мс = **0.12 секунд** вместо 46 минут.
- **(B) MinHash LSH:** Локально-чувствительное хэширование для быстрого поиска кандидатов.
- **(C) Clustering:** K-means или HDBSCAN на эмбеддингах → сравнивать только внутри кластеров.

**Рекомендация:** (A) — уже есть sqlite-vec, просто использовать его вместо brute-force. 200x быстрее.

---

### 5. 🟡 2 LLM-вызова на каждое сообщение

**Что происходит:**
```
ingest("Клиент согласился на 150к до пятницы")
  → classify_message()  → LLM call #1 (~250 tokens)
  → extract_entities()  → LLM call #2 (~400 tokens)
```

При 500 сообщений/день = 1,000 API-вызовов.

**Токены:**
- classify: system prompt (~200 tokens) + user message (~50 tokens) + response (~30 tokens) = ~280 tokens
- extract: system prompt (~250 tokens) + user message (~50 tokens + контекст классификации ~50) + response (~100 tokens) = ~450 tokens
- **Итого: ~730 tokens на сообщение**

**Стоимость при разных провайдерах (500 msg/day):**

| Провайдер | Input $/1M | Output $/1M | День | Месяц |
|-----------|-----------|-------------|------|-------|
| MiMo V2 Pro | бесплатно* | бесплатно* | $0 | $0 |
| GPT-4o-mini | $0.15 | $0.60 | $0.05 | $1.6 |
| GPT-4o | $2.50 | $10.00 | $0.49 | $14.6 |
| Claude 3.5 Sonnet | $3.00 | $15.00 | $0.59 | $17.6 |
| Claude 3.5 Haiku | $0.80 | $4.00 | $0.16 | $4.7 |

*Предполагается, что MiMo API бесплатный или очень дешёвый для внутреннего использования Xiaomi.

**Дополнительные LLM-вызовы:**
- RAPTOR build: ~N/10 вызовов на суммаризацию (batch из 10 memories)
- consolidate merge: 0 (локальный cosine)
- nightly digest: 0 (SQL-запросы)

**Решения:**
- **(A) Батчинг:** Отправлять 5-10 сообщений в одном classify-вызове. Один system prompt, N сообщений = N результатов. Сокращает токены на ~60%.
- **(B) Комбинированный промпт:** classify + extract в одном вызове. Один system prompt, два задания.
- **(C) Кэширование результатов:** Если одно и то же сообщение прилетит повторно — не вызывать LLM.
- **(D) Локальная модель:** Использовать маленькую локальную модель (phi-3, tinyllama) для классификации.

**Рекомендация:** (A) + (B). Батч classify: 10 сообщений в одном вызове. Combine classify+extract в один промпт. Сократит до ~0.2 LLM-вызова на сообщение.

---

### 6. 🟡 ingest теряет тип в CHAOS

**Что происходит:**
```python
if label in ("task", "decision", "lead-signal", "risk"):
    chaos_store(
        category=label if label in ("decision", "risk") else "event",
        #                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        # task и lead-signal маппятся на "event"
    )
```

**Почему это плохо:**
- `chaos_entries.category` имеет CHECK constraint: `decision, risk, policy, fact, event, other`
- `task` и `lead-signal` — не в списке разрешённых категорий
- При поиске `chaos-search "задача"` — не найдёт задачи, потому что они в category='event'

**Решение:** Расширить CHECK constraint в schema:
```sql
CHECK(category IN ('decision', 'risk', 'policy', 'fact', 'event', 'task', 'lead-signal', 'other'))
```
И убрать маппинг:
```python
chaos_store(category=label, ...)
```

---

### 7. 🟡 _call_llm нет retry / backoff

**Что происходит:**
```python
resp = requests.post(url, headers=headers, json=payload, timeout=30)
resp.raise_for_status()  # ← мгновенный exception при 429/500/timeout
```

**Сценарии сбоя:**
- 429 Too Many Requests (rate limit) — частый при высоком трафике
- 500 Internal Server Error —_transient_, retry помогает
- Timeout (30с) — LLM загружается / перегружен
- Connection error — сеть

**Решение:** Exponential backoff:
```python
for attempt in range(3):
    try:
        resp = requests.post(...)
        resp.raise_for_status()
        return resp.json()...
    except (requests.exceptions.RequestException, HTTPError) as e:
        if attempt < 2:
            time.sleep(2 ** attempt)  # 1s, 2s, 4s
        else:
            raise
```

---

### 8. 🟡 Нет batch-ингеста

**Что происходит:**
Если Telegram-бот перезапустился и нужно обработать 100 накопленных сообщений:
```python
for msg in messages:
    ingest(msg.text)  # 100 × (6.6с model load + 2 LLM calls)
```
= 660 секунд только на загрузку модели (если CLI) + 200 LLM-вызовов.

**Решение:**
```python
def ingest_batch(messages):
    # 1. Batch classify (1 LLM call для N сообщений)
    labels = classify_batch([m.text for m in messages])  # 1 вызов
    
    # 2. Batch extract только для важных (1 LLM call)
    important = [m for m, l in zip(messages, labels) if l in IMPORTANT]
    entities = extract_batch([m.text for m in important])  # 1 вызов
    
    # 3. Batch embed (1 forward pass через sentence-transformers)
    vecs = model.encode([m.text for m in messages])  # ~50ms для 100 текстов
    
    # 4. Batch insert в SQLite
    ...
```

**Выигрыш:** 100 сообщений = 2-3 LLM-вызова вместо 200. ~100x сокращение.

---

### 9. 🟡 ~400MB RAM заняты моделью постоянно

**Замер:**
```
all-MiniLM-L6-v2: ~90MB on disk, ~380MB в RAM (PyTorch tensors)
```

**Проблема:** На VPS с 1GB RAM модель займёт 38%. Оставшиеся 620MB — на SQLite, Python, бота, ОС. Близко к OOM при пиках.

**Решения:**
- **(A) ONNX quantization:** Конвертировать модель в ONNX INT8 → ~50MB RAM, ~2с загрузка.
- **(B) Лёгкая модель:** `paraphrase-MiniLM-L3-v2` → ~60MB в RAM, чуть хуже качество.
- **(C) Вынос модели:** Модель на отдельном сервисе/GPU, Lenochka шлёт HTTP-запросы.

---

### 10. 🟢 auto_associate не закрывает conn при исключении

**Что происходит:**
```python
def auto_associate(memory_id, content, threshold=0.35):
    conn = _get_db()
    try:
        # sqlite-vec path
        ...
    except Exception:
        # fallback path
        ...
    conn.commit()
    conn.close()
```

Если `conn.commit()` упадёт (locked table, I/O error), `conn.close()` не вызовется. Соединение висит до GC.

**Решение:** Использовать context manager:
```python
with _get_db() as conn:
    ...
```
Или `try/finally`.

---

### 11. 🟢 context packet — нет cross-source re-ranking

**Что происходит:**
Vector search возвращает скоры от 0 до ~1 (cosine similarity). FTS BM25 возвращает ранги от -10 до 0. Они просто конкатенируются в один список без нормализации.

**Пример:** Vector result score=0.15 может быть более релевантен, чем FTS result score=-2.5, но сортировка не учитывает разницу шкал.

**Решение:**
- Z-score нормализация скоров внутри каждого источника
- Reciprocal Rank Fusion (RRF) для объединения ранжированных списков
- Веса по источнику: vector=0.5, FTS=0.3, keyword=0.2

---

### 12. 🟢 Нет batch embed_texts()

**Что происходит:**
```python
# Сейчас:
for text in texts:
    vec = embed_text(text)  # 11ms каждый

# sentence-transformers поддерживает:
vecs = model.encode(texts)  # ~15ms для 10 текстов (batch)
```

Batch encode = 1 forward pass для N текстов вместо N forward passes. При 10 текстах: 15мс вместо 110мс = **7x быстрее**.

**Решение:** Добавить функцию `embed_texts_batch(texts)` в brain.py.

---

### 13. 🟢 Нет fingerprint/hash в messages

**Что происходит:**
Таблица `messages` не имеет поля для уникального идентификатора исходного сообщения (например, Telegram message_id или hash контента). Невозможно проверить дедуп на уровне CRM.

**Решение:** Добавить поля:
```sql
ALTER TABLE messages ADD COLUMN source_msg_id TEXT;  -- Telegram message_id
ALTER TABLE messages ADD COLUMN content_hash TEXT;    -- SHA-256 контента
CREATE UNIQUE INDEX idx_messages_dedup ON messages(chat_thread_id, source_msg_id);
```

---

### 14. 🟢 FTS MATCH может кидать исключение

**Что происходит:**
FTS5 может выбросить `sqlite3.OperationalError: syntax error in FTS5 MATCH expression` если запрос содержит спецсимволы: `"`, `'`, `*`, `(`, `)`, `:` и т.д.

Сейчас в `chaos_search` это обёрнуто в try/except, но в `recall` и `build_context_packet` — не всегда.

**Решение:** Обернуть все FTS MATCH вызовы в try/except с fallback на LIKE.

---

### 15. 🟢 Нет graceful shutdown / signal handling

**Что происходит:**
Если brain.py работает как daemon и его убивают (SIGTERM, SIGINT), нет корректного завершения: закрытия SQLite WAL, сохранения состояния.

**Решение:**
```python
import signal
def shutdown(signum, frame):
    conn.close()
    sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
```

---

## Рекомендуемый порядок исправления

### Phase 1 — До публикации (быстрые фиксы, ~2 часа)
1. Транзакционный store (один COMMIT) — 5 минут
2. Дедуп ingest по content hash — 15 минут
3. Retry в `_call_llm` — 10 минут
4. Расширить CHAOS category — 5 минут
5. auto_associate try/finally — 5 минут
6. FTS try/except — 5 минут
7. Batch embed_texts() — 15 минут

### Phase 2 — Сразу после публикации (средние фиксы, ~1 день)
8. Батчинг classify+extract (5-10 сообщений за вызов)
9. Consolidate через vec ANN вместо O(n²)
10. Batch ingest функция
11. context packet re-ranking (RRF)

### Phase 3 — Production readiness (~1 неделя)
12. Серверный режим brain (daemon с моделью в памяти)
13. ONNX quantization модели
14. source_msg_id / content_hash в messages
15. Graceful shutdown / signal handling
16. Telegram-бот интеграция
