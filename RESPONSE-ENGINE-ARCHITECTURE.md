# RESPONSE ENGINE — Полная архитектура

> Дата: 2026-03-30
> Версия: v3 (с полноценным LLM-участием)
> Сессия: Камиль + Леночка

---

## 0. ФИЛОСОФИЯ

### Бот — два существа в одном

```
МОД 1: Фактолог (клиенту)              МОД 2: Напоминалка (owner'у)
─────────────────────────────          ─────────────────────────────
Отвечает на основе данных из CRM.      Уведомляет owner'а когда:
LLM формулирует ответ естественно.     - Клиент ждёт ответа >30 мин
Ничего не решает — только факты.       - Нужно решение/цена/КП
                                       - Риск/жалоба
                                       - Follow-up срок наступил
```

### Главное правило

```
LLM понимает вопрос клиента.
SQL достаёт факты из БД.
LLM формулирует естественный ответ из фактов.
Если фактов нет или нужно решение owner'а → escalate.

При сомнениях → escalate к owner'у.
Молчание > плохой ответ.
```

### Роль LLM в response engine

```
Этап                          LLM нужен?   Зачем
──────────────────────────    ──────────   ─────────────────────────────
Понять что спрашивает         ✅ Да        Regex не поймёт "а что там
                                            с тем проектом про Ивана?"
Достать факты из БД           ❌ Нет        SQL, бесплатно
Решить: отвечать или          ✅ Да        Нужно суждение: это факт
  escalate?                                 или требует решения?
Сформулировать ответ          ✅ Да        Шаблоны звучат как робот
Понять что диалог             ✅ Да        "ну ладно, может позже"
  закончился                                — не всегда "ок"
```

---

## 1. ТРИГГЕРНАЯ МАТРИЦА

### 1.1 HARD SKIP (молча ingest, ничего не делать)

```
Условие                          Почему
─────────────────────────────    ──────────────────────────
sender_business_bot заполнен     Собственное сообщение бота
is_from_offline                  Автоответ/запланированное
Группа >3 без @mention           Не вмешиваться
Канал (read-only)                Нечего отвечать
Owner написал в бизнес-чате      Owner не ждёт ответа от бота
```

### 1.2 SKIP с помощью LLM (быстрая проверка, ~$0.0005)

```
Эти проверки НЕЛЬЗЯ делать regex — нужен LLM.

Условие                          Пример
─────────────────────────────    ──────────────────────────
label = noise/chit-chat          Уже из classify — бесплатно
Диалог закончился                "ну ладно, может позже" — не "ок"
Клиент пишет не по делу          "😂😂😂 хаах" — не ждёт ответа
Owner уже решает                 "подождите, думаю" — owner в процессе
```

### 1.3 FACT-BASED RESPONSE (LLM понимает → SQL достаёт → LLM отвечает)

```
Триггер                          Пример вопроса клиента
─────────────────────────────    ──────────────────────────
Вопрос о дедлайне                "Когда будет готово?"
Вопрос о статусе                 "Что там с КП?"
Вопрос о сумме                   "Сколько мы договорились?"
Запрос контекста                 "Напомни что мы говорили"
Уточнение по проекту             "А тот заказ для Ивана?"
Повторный вопрос                 "Вы так и не ответили когда"
```

### 1.4 ESCALATION (LLM определяет → notify owner'а)

```
Триггер                          Пример
─────────────────────────────    ──────────────────────────
Вопрос о цене/стоимости          "Сколько будет стоить?"
Запрос КП                        "Пришлите КП"
Договорённость о встрече         "Давайте встретимся"
Решение требующее суждения       "Что вы предлагаете?"
Жалоба/риск                      "Вы обещали вчера!"
Новый лид                        "Привет, хочу обсудить проект"
Запрос owner'а лично             "Передайте Камилю"
```

### 1.5 Decision Tree

```
message arrives
│
├─ [HARD SKIP] sender_business_bot / is_from_offline / group>3 / channel?
│   └─ SKIP
│
├─ [LLM: CLASSIFY] label = noise / chit-chat? (уже из pipeline)
│   └─ SKIP
│
├─ [LLM: DIALOG CHECK] диалог завершился? (intent="ended")
│   └─ SKIP
│
├─ [LLM: RESPONSE DECISION] можно ответить из данных в CRM?
│   │
│   ├─ intent="fact_answer" + есть данные в БД
│   │   → FACT RESPONSE (LLM формулирует из SQL-данных)
│   │
│   ├─ intent="needs_owner" или данных нет
│   │   → ESCALATION (30 мин → notify owner'а)
│   │
│   └─ intent="no_response_needed"
│       → SKIP
│
└─ DEFAULT → SKIP
```

---

## 2. LLM-IN-THE-LOOP RESPONSE ENGINE

### 2.1 Архитектура: 3 LLM-вызова (или меньше)

```
ВХОД: сообщение клиента + контекст (SQL: chat history, contact, deals, tasks)

ШАГ 1: RESPONSE DECISION (1 LLM-вызов, ~$0.001)
  system: "Определи что делать с сообщением клиента"
  input:  текст + контекст чата + данные CRM
  output: JSON {action, intent, fact_query, reason}
  
  action ∈ {"respond_fact", "escalate", "skip"}
  intent ∈ {"deadline", "status", "amount", "context_recall",
            "pricing", "proposal", "meeting", "complaint",
            "agreement", "chitchat", "ended", "other"}

  Если action="skip" → DONE. Нет второго вызова.
  Если action="escalate" → шаблонное уведомление owner'у. Нет второго вызова.
  Если action="respond_fact" → ШАГ 2.

ШАГ 2: SQL QUERY (бесплатно)
  По intent → соответствующая SQL-функция → данные из БД
  
  Если данные найдены → ШАГ 3.
  Если данных нет → ESCALATE (не выдумывать).

ШАГ 3: RESPONSE GENERATION (1 LLM-вызов, ~$0.001)
  system: "Сформулируй ответ клиенту на основе фактов"
  input:  вопрос клиента + факты из БД + имя контакта
  output: текст ответа (1-3 предложения)

ИТОГО на одно сообщение: 1-2 LLM-вызова ($0.001-$0.002)
                         Чаще всего 1 вызов (step 1 → skip/escalate)
```

### 2.2 Step 1: Response Decision (LLM)

```python
RESPONSE_DECISION_SYSTEM = """Ты решаешь как бот Lenochka должен отреагировать на сообщение клиента.

ВАЖНО: Бот — НЕ продавец, НЕ менеджер. Бот — помощник владельца бизнеса.
Бот НЕ договаривается о встречах, НЕ назначает цены, НЕ делает КП.
Бот может ответить ТОЛЬКО на основе данных из CRM.

Определи action:
- "respond_fact" — вопрос можно ответить из данных CRM (дедлайны, статусы, суммы, история)
- "escalate" — вопрос требует решения/суждения владельца (цены, КП, договоры, встречи, жалобы)  
- "skip" — не требует ответа (ок, согласен, спам, эмоции, междометия)

Определи intent (один из):
- "deadline" — когда будет готово? когда прислать?
- "status" — что там с проектом/задачей?
- "amount" — сколько договорились? какая сумма?
- "context_recall" — напомни о чём говорили, что решили
- "pricing" — сколько стоит? какая цена? (→ escalate)
- "proposal" — пришлите КП/предложение (→ escalate)
- "contract" — договор, подписание (→ escalate)
- "meeting" — встреча, созвон, когда удобно (→ escalate)
- "complaint" — жалоба, задержка, обещали (→ escalate)
- "ended" — ок, согласен, договорились, понял (→ skip)
- "chitchat" — привет, как дела, эмоции (→ skip)
- "other" — не удалось определить (→ escalate для безопасности)

Отвечай ТОЛЬКО JSON:
{"action":"respond_fact|escalate|skip","intent":"<тип>","query_hint":"<что искать в БД или null>","reason":"<1 фраза>"}"""


async def decide_response(text, chat_context, crm_context, brain) -> dict:
    """
    LLM решает: отвечать, escalate или skip.
    ОДИН вызов. ~$0.001.
    
    Возвращает {action, intent, query_hint, reason}.
    """
    user_prompt = (
        f"Сообщение клиента:\n{text}\n\n"
        f"Контекст чата (последние 5 сообщений):\n{chat_context}\n\n"
        f"Данные CRM по этому контакту:\n{crm_context}"
    )
    
    result = brain._call_llm(
        RESPONSE_DECISION_SYSTEM, user_prompt,
        temperature=0.0, max_tokens=300
    )
    
    if result:
        data = brain._extract_json(result)
        if isinstance(data, dict) and "action" in data:
            return data
    
    # Fallback: при ошибке LLM → escalate (безопаснее)
    return {"action": "escalate", "intent": "other", 
            "query_hint": None, "reason": "LLM unavailable"}
```

### 2.3 Step 2: SQL Query (бесплатно)

```python
def query_fact(intent: str, query_hint: str, contact_id: int, 
               chat_thread_id: int, db_path: str) -> str | None:
    """
    Достаёт факты из БД по intent.
    Возвращает текст фактов или None (нет данных → escalate).
    """
    queries = {
        "deadline": query_deadline,
        "status": query_status,
        "amount": query_amount,
        "context_recall": query_context,
    }
    
    fn = queries.get(intent)
    if not fn:
        return None
    
    return fn(contact_id, query_hint, chat_thread_id, db_path)


def query_deadline(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Когда договор? Когда КП? Когда оплата?"""
    conn = get_db(db_path)
    parts = []
    
    # Agreements
    rows = conn.execute("""
        SELECT summary, due_at, status, amount FROM agreements
        WHERE contact_id = ? AND status NOT IN ('completed', 'cancelled')
        ORDER BY created_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        if r["status"] == "signed":
            parts.append(f"Договор «{r['summary'] or '—'}» подписан ✅")
        elif r["due_at"]:
            parts.append(f"Договор «{r['summary'] or '—'}» — до {r['due_at'][:10]}")
    
    # Tasks with due dates
    rows = conn.execute("""
        SELECT description, due_at, status FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND status NOT IN ('done', 'cancelled') AND due_at IS NOT NULL
        ORDER BY due_at ASC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        icon = "✅" if r["status"] == "done" else "📋"
        parts.append(f"{icon} {r['description'][:60]} — до {r['due_at'][:10]}")
    
    # Deals with expected close
    rows = conn.execute("""
        SELECT amount, stage, expected_close_at FROM deals
        WHERE contact_id = ? AND expected_close_at IS NOT NULL
          AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY expected_close_at ASC LIMIT 2
    """, (contact_id,)).fetchall()
    for r in rows:
        amt = f"{r['amount']:,.0f}₽" if r.get("amount") else ""
        parts.append(f"Сделка {amt} — до {r['expected_close_at'][:10]} ({r['stage']})")
    
    conn.close()
    return "\n".join(parts) if parts else None


def query_status(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Что там с КП? Как дела с проектом?"""
    conn = get_db(db_path)
    parts = []
    
    # Active deals
    rows = conn.execute("""
        SELECT amount, stage, notes, updated_at FROM deals
        WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY updated_at DESC LIMIT 2
    """, (contact_id,)).fetchall()
    for r in rows:
        amt = f"{r['amount']:,.0f}₽" if r.get("amount") else ""
        parts.append(f"Сделка {amt}: стадия «{r['stage']}»")
    
    # Open tasks
    rows = conn.execute("""
        SELECT description, status, priority, due_at FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND status NOT IN ('done', 'cancelled')
        ORDER BY 
            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
            due_at ASC
        LIMIT 5
    """, (contact_id,)).fetchall()
    for r in rows:
        icon = {"open": "📋", "in_progress": "🔨"}.get(r["status"], "📋")
        due = f" (до {r['due_at'][:10]})" if r.get("due_at") else ""
        parts.append(f"{icon} {r['description'][:60]}{due}")
    
    # Recent memories
    rows = conn.execute("""
        SELECT content, created_at FROM memories
        WHERE contact_id = ?
        ORDER BY importance DESC, created_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"📝 {r['content'][:80]}")
    
    conn.close()
    return "\n".join(parts) if parts else None


def query_amount(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Сколько договорились?"""
    conn = get_db(db_path)
    
    row = conn.execute("""
        SELECT amount, stage, notes FROM deals
        WHERE contact_id = ? AND amount IS NOT NULL
          AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY updated_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    
    conn.close()
    if row:
        return f"Сумма: {row['amount']:,.0f}₽, стадия: {row['stage']}"
    return None


def query_context(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Напомни что мы говорили / что решили."""
    conn = get_db(db_path)
    parts = []
    
    # Recent messages in chat
    rows = conn.execute("""
        SELECT text, from_user_id, sent_at FROM messages
        WHERE chat_thread_id = ?
          AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
        ORDER BY sent_at DESC LIMIT 10
    """, (chat_thread_id,)).fetchall()
    for r in reversed(rows):
        author = "Я" if r["from_user_id"] == "self" else "Клиент"
        parts.append(f"{author}: {r['text'][:100]}")
    
    # Key memories
    rows = conn.execute("""
        SELECT content, type, created_at FROM memories
        WHERE contact_id = ? AND importance >= 0.6
        ORDER BY created_at DESC LIMIT 5
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"📌 {r['content'][:100]}")
    
    conn.close()
    return "\n".join(parts) if parts else None
```

### 2.4 Step 3: Response Generation (LLM)

```python
RESPONSE_GEN_SYSTEM = """Ты — Lenochka, AI-ассистент владельца бизнеса.
Клиент задал вопрос. У тебя есть факты из CRM.

ПРАВИЛА:
- Отвечай ТОЛЬКО на основе фактов. Не выдумывай.
- 1-3 предложения. Деловой тон.
- Если в фактах написано что-то — перескажи естественно.
- Не упоминай "CRM", "база данных", "система".
- Отвечай на языке клиента.
- Если фактов мало — кратко. Не растекайся."""

RESPONSE_GEN_USER = """Вопрос клиента: {question}

Факты из CRM:
{facts}

Сформулируй ответ."""


async def generate_fact_response(question: str, facts: str,
                                  contact_name: str, brain) -> str | None:
    """
    LLM формулирует естественный ответ на основе фактов.
    ОДИН вызов. ~$0.001.
    """
    result = brain._call_llm(
        RESPONSE_GEN_SYSTEM,
        RESPONSE_GEN_USER.format(question=question, facts=facts),
        temperature=0.3,
        max_tokens=200,
    )
    
    if result and len(result) > 5:
        return result[:500]
    
    return None  # Не удалось сгенерировать → escalate
```

---

## 3. ESCALATION ENGINE

### 3.1 Когда escalate

Step 1 (LLM decision) уже определил action="escalate" + intent. Дополнительная логика не нужна.

### 3.2 Таймер + Уведомление

```python
async def handle_escalation(item, decision, chat_state, pipeline):
    """
    Escalation: ставим таймер → проверяем через N минут → notify owner'у.
    """
    delay = ESCALATION_DELAY.get(decision["intent"], 1800)  # default 30 мин
    
    # Night mode: если 23-08 и НЕ urgent → schedule на 08:00
    hour = datetime.now(GMT8).hour
    if (23 <= hour or hour < 8) and decision["intent"] not in ("complaint",):
        delay = _seconds_until_8am()
    
    # Save pending notification
    await asyncio.to_thread(
        save_pending_notification,
        item.chat_thread_id, item.contact_id, item.message_id,
        item.normalized.text, decision["intent"], delay,
        pipeline.db_path,
    )
    
    # Schedule async check
    asyncio.create_task(
        check_and_notify_later(pipeline, item, decision, delay)
    )


ESCALATION_DELAY = {
    "complaint": 600,      # 10 мин — жалобы быстрее
    "pricing": 1800,       # 30 мин
    "proposal": 1800,
    "contract": 1800,
    "meeting": 1800,
    "other": 1800,
}


async def check_and_notify_later(pipeline, item, decision, delay):
    """Ждём delay секунд, проверяем, уведомляем owner'а."""
    await asyncio.sleep(delay)
    
    state = await asyncio.to_thread(
        get_dialog_state, pipeline.db_path,
        item.chat_thread_id, item.message_id
    )
    
    if state["owner_replied_after"]:
        cancel_notification(pipeline.db_path, item.message_id)
        return
    
    # Owner не ответил → notify
    ctx = await asyncio.to_thread(
        build_notification_context, pipeline.db_path,
        item.chat_thread_id, item.contact_id
    )
    
    text = format_notification(
        decision["intent"],
        ctx.get("contact_name", "Клиент"),
        item.normalized.text,
        _format_duration(delay),
        _format_context_block(ctx),
    )
    
    await pipeline.bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode="HTML",
    )
    
    mark_notified(pipeline.db_path, item.message_id)
```

### 3.3 Формат уведомления owner'у

```python
NOTIFY_TEMPLATES = {
    "pricing": """💰 {contact_name} спрашивает о цене ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}

💡 Напишите клиенту напрямую.""",

    "proposal": """📄 {contact_name} просит КП ({wait_time})

💬 «{message_text}»

📋 {context_block}""",

    "contract": """📝 {contact_name} спрашивает о договоре ({wait_time})

💬 «{message_text}»

📋 {context_block}""",

    "meeting": """📅 {contact_name} предлагает встречу ({wait_time})

💬 «{message_text}»

📋 {context_block}""",

    "complaint": """⚠️ {contact_name}: жалоба/риск ({wait_time})

💬 «{message_text}»

📋 {context_block}""",

    "other": """❓ {contact_name} спрашивает ({wait_time})

💬 «{message_text}»

📋 {context_block}""",
}
```

---

## 4. DIALOG ENDED DETECTION

### 4.1 LLM-based (не regex)

```
Regex поймёт: "ок", "согласен", "договорились"
Regex НЕ поймёт: "ну ладно", "может позже тогда", "ага понял",
                  "ну ок тогда", "короче ок давай так"

Решение: диалог-ended проверка — часть Step 1 (response decision).
LLM определяет intent="ended" → SKIP.
```

### 4.2 Но: fast path для очевидных случаев

```python
# Перед LLM-вызовом — быстрая проверка бесплатно
EXACT_END_PHRASES = {
    "ок", "окей", "ok", "okay", "хорошо", "ладно",
    "согласен", "согласна", "согласовано", "подтверждаю",
    "договорились", "принято", "отлично", "понял", "поняла",
    "понятно", "ясно", "逴", "逴", "逴", "逴",
}

def fast_dialog_ended(text: str) -> bool:
    """Бесплатная проверка: точные фразы. LLM не нужен."""
    normalized = text.lower().strip().rstrip(".!?,;:😴👍🤝✅👌")
    return normalized in EXACT_END_PHRASES

def fast_sticker_ended(msg) -> bool:
    """Стикер-подтверждение. Бесплатно."""
    if not msg.sticker:
        return False
    emoji = msg.sticker.emoji or ""
    return emoji in ("👍", "🤝", "✅", "👌", "👊")
```

---

## 5. EDGE CASES

### 5.1 Антипетли

```
Антипетли НЕТ в этой архитектуре. Бот пишет:
- Клиенту: через business API (Step 3 response)
- Owner'у: в личку бота (escalation notification)

Защита от петли с клиентом:
1. sender_business_bot → HARD SKIP (не обрабатывается)
2. Если бот отправил ответ → Telegram НЕ шлёт business_message
   на собственный ответ (sender_business_bot заполнен).
   Pipeline не видит свой ответ. Петли нет.

Защита от дублей ответов:
1. Anti-spam: не отвечать чаще раза в 5 мин на чат
2. Pending notification dedup по message_id
```

### 5.2 Owner ответил за 30 минут

```
t=0:   Клиент: "Сколько стоит?" → LLM: escalate, pricing → schedule 30 мин
t=5:   Owner: "500 тысяч"
t=30:  check_and_notify_later() → owner_replied_after=True → CANCEL
```

### 5.3 Клиент написал "ок" после вопроса

```
t=0:   Клиент: "Когда договор?" → LLM: respond_fact → Бот: "До 04.04"
t=1:   Клиент: "ок" → fast_dialog_ended() → SKIP
```

### 5.4 Факт-based ответ и данных нет

```
Клиент: "Когда договор?"
LLM: respond_fact, intent=deadline
SQL: query_deadline() → None (нет agreements)
→ ESCALATE (не выдумывать) → notify owner'у
```

### 5.5 LLM сломался (timeout, error)

```
LLM вернул None → fallback: {"action":"escalate","intent":"other"}
Безопаснее: не отвечать, а уведомить owner'а.
```

### 5.6 Ночное сообщение

```
t=23:30: Клиент: "Сколько стоит?" → LLM: escalate, pricing
  → delay = _seconds_until_8am() (8.5 часов)
  → 08:00: notify owner'у

t=23:30: Клиент: "ГДЕ ДЕНЬГИ?!" → LLM: escalate, complaint
  → delay = 600 (10 мин) — отправить СРАЗУ даже ночью
```

### 5.7 Owner написал "ок" клиенту (не ответил на вопрос)

```
Owner: "ок, подожди"
Клиент: "сколько ждать?"
LLM: читает chat_context = "Owner: ок, подожди" → intent=other
  → owner уже в процессе → ESCALATE (owner должен конкретизировать)
```

### 5.8 Несколько вопросов в одном сообщении

```
Клиент: "Когда договор и сколько стоит?"
LLM: понимает что два вопроса → выбирает ДОМИНИРУЮЩИЙ intent
  pricing > deadline (escalate > fact) → intent=pricing → ESCALATE
```

---

## 6. COST ANALYSIS

### 6.1 Стоимость обработки одного сообщения

```
Сообщение (вход):
├── classify (batch 10)            → $0.0001  (уже есть)
├── extract (batch, 30% important) → $0.0003  (уже есть)
│
├── RESPONSE ENGINE (новое):
│   ├── Step 1: response decision  → $0.001
│   │   (skip для 60% сообщений — но LLM всё равно вызывается)
│   │
│   ├── Step 2: SQL query          → $0
│   │
│   └── Step 3: response gen       → $0.001 (только если respond_fact)
│                                     (40% от решений respond_fact)
│
└── ИТОГО за сообщение:
    ├── skip/escalate:  $0.0014 (step 1 только)
    ├── fact_response:  $0.0024 (step 1 + step 3)
    └── average:        ~$0.0018

При 500 сообщений/день:
├── Classify + extract:   $0.20
├── Response engine:      $0.90
├── ИТОГО:                $1.10/день = ~$33/месяц
```

### 6.2 Оптимизация (как снизить до $15/месяц)

```
Оптимизация 1: Fast path (бесплатно)
  Точные фразы ("ок", "согласен") → skip БЕЗ LLM
  Сэкономит ~30% Step 1 вызовов
  → $0.90 → $0.63

Оптимизация 2: Batch response decision
  10 сообщений → 1 LLM-вызов для решений (как classify_batch)
  → $0.63 → $0.30

Оптимизация 3: Комбинированный classify + response decision
  Один LLM-вызов: и классифицирует, И решает отвечать/escalate
  → $0.20 + $0.30 = $0.50 вместо $0.20 + $0.63 = $0.83

Оптимизация 4: Шаблоны для простых fact-response
  intent=deadline + данные есть → шаблон "До {date}" без LLM
  → $0.30 → $0.20

ИТОГО с оптимизациями:
  ~$0.50/день = ~$15/месяц
```

### 6.3 Оптимизация 3 — комбинированный промпт (детали)

```python
COMBINED_CLASSIFY_SYSTEM = """Ты — классификатор и роутер сообщений для CRM-системы Lenochka.

1. Классифицируй сообщение:
   noise / chit-chat / business-small / task / decision / lead-signal / risk / other

2. Определи что делать с сообщением:
   - "respond_fact" — клиент спрашивает, можно ответить из CRM (дедлайны, статусы, суммы)
   - "escalate" — клиенту нужен ответ от владельца (цены, КП, встречи, решения)
   - "skip" — не требует ответа (ок, согласен, эмоции, спам)

3. Если respond_fact — укажи что искать в CRM (query_hint).

Отвечай ТОЛЬКО JSON:
{"label":"<категория>","confidence":<0.0-1.0>,
 "action":"respond_fact|escalate|skip",
 "intent":"<тип>","query_hint":"<что искать или null>","reason":"<кратко>"}

Для batch: JSON array, ровно N элементов."""
```

**Преимущество:** 1 LLM-вызов вместо 2 (classify + response decision). Сокращение ~40% на response engine.

---

## 7. ФАЙЛЫ И РЕАЛИЗАЦИЯ

### 7.1 Новые файлы

```
lenochka-bot/services/
│
├── response_engine.py          (~400 строк) — расширен
│   ├── classify_and_route_batch() — COMBINED classify+route (primary)
│   ├── decide_response()          — single-message decision (fallback)
│   ├── generate_fact_response()   — Step 3: LLM из фактов
│   ├── detect_followups()         — LLM ищет implicit obligations
│   ├── ResponseGuard              — anti-loop + anti-spam per chat
│   ├── COMBINED_SYSTEM prompt
│   ├── RESPONSE_GEN_SYSTEM prompt
│   ├── FOLLOWUP_DETECT_SYSTEM prompt
│   ├── fast_dialog_ended()
│   └── fast_sticker_ended()
│
├── fact_queries.py             (~350 строк) — расширен (10+ интентов)
│   ├── query_fact() — роутер
│   ├── query_deadline() / query_status() / query_amount() / query_context()
│   ├── query_payment_status() — счета, платежи
│   ├── query_overdue() — просроченные задачи/счета
│   ├── query_tasks_today() — задачи на сегодня
│   ├── query_leads_summary() — сводка по лидам
│   ├── query_deal_details() — детали сделки
│   ├── query_contact_history() — memories контакта
│   └── query_last_interaction() — последнее сообщение
│
├── notifier.py                 (~250 строк) — расширен
│   ├── handle_escalation()
│   ├── check_and_notify_later()
│   ├── recover_pending_notifications()  — startup recovery
│   ├── _wait_and_notify()               — persistent timer
│   ├── ESCALATION_DELAY
│   ├── NOTIFY_TEMPLATES
│   ├── save_pending_notification()
│   ├── cancel_notification()
│   └── mark_notified()
│
├── dialog_state.py             (~100 строк)
│   ├── get_dialog_state()
│   ├── _is_owner_message()
│   └── _is_new_contact()
│
└── response_context.py         (~100 строк)
    ├── build_notification_context()
    ├── build_crm_context_for_llm()
    └── build_chat_context_for_llm()
```

### 7.2 Изменения в существующих

```
lenochka-bot/services/
├── pipeline.py
│   ├── _process_batch() — Phase 4: response decision + fact response + escalation
│   ├── _handle_response_decision()
│   └── _handle_fact_response()

├── scheduler.py
│   └── + _check_followups() — каждый час

lenochka-memory/
├── schemas/init.sql
│   └── + pending_notifications table
```

### 7.3 pending_notifications table

```sql
CREATE TABLE IF NOT EXISTS pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_thread_id INTEGER NOT NULL REFERENCES chat_threads(id),
    contact_id INTEGER REFERENCES contacts(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    message_text TEXT,
    escalation_type TEXT,
    notify_at DATETIME NOT NULL,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'cancelled')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pending_notify 
    ON pending_notifications(status, notify_at);
```

---

## 8. ПОШАГОВАЯ РЕАЛИЗАЦИЯ

### Phase 1: Fact-Based Response (~4 часа)

```
1.1 Создать response_engine.py
    - decide_response() — Step 1 LLM decision
    - generate_fact_response() — Step 3 LLM generation
    - System prompts (RESPONSE_DECISION, RESPONSE_GEN)
    - fast_dialog_ended() + fast_sticker_ended()

1.2 Создать fact_queries.py
    - query_fact() роутер
    - query_deadline() / query_status() / query_amount() / query_context()

1.3 Создать response_context.py
    - build_crm_context_for_llm() — собирает данные CRM для Step 1
    - build_chat_context_for_llm() — собирает chat history для Step 1

1.4 Интегрировать в pipeline._process_batch()
    - Phase 4: после classify, перед finalize
    - Fast path: fast_dialog_ended → skip (бесплатно)
    - Step 1: decide_response() (LLM)
    - Step 2: query_fact() (SQL) если action=respond_fact
    - Step 3: generate_fact_response() (LLM) если данные найдены
    - Skip если action=skip
    - Отправить ответ через bot.send_message если action=respond_fact

1.5 Anti-spam
    - Не отвечать чаще раза в 5 мин на чат
    - Хранить в memory: {chat_id: last_response_time}

1.6 Тесты
    - "Когда договор?" → LLM: deadline → SQL → ответ
    - "ок" → fast skip
    - "Сколько стоит?" → LLM: escalate (ещё не реализован)
```

### Phase 2: Escalation + Notifications (~3 часа)

```
2.1 Создать dialog_state.py
    - get_dialog_state() — owner_replied_after, contact_is_new
    - _is_owner_message()

2.2 Создать notifier.py
    - handle_escalation() — schedule notification
    - check_and_notify_later() — async recheck + send
    - ESCALATION_DELAY по типам
    - NOTIFY_TEMPLATES
    - Night mode (23-08)
    - Pending notifications (save/cancel/mark)

2.3 Добавить pending_notifications в init.sql

2.4 Интегрировать в pipeline
    - Если Step 1 → action=escalate → handle_escalation()
    - Если fact_query вернул None → escalate

2.5 Тесты
    - "Сколько стоит?" → escalate → 30 мин → owner уведомлён
    - Owner ответил за 10 мин → notification cancelled
    - Клиент написал "ок" → notification cancelled
```

### Phase 3: Proactive Reminders (~2 часа)

```
3.1 Добавить _check_followups() в scheduler (каждый час)
    - Tasks due today
    - Agreements due soon

3.2 Startup recovery
    - Проверить pending_notifications при запуске

3.3 Aggregate notifications
    - Несколько pending для одного чата → одно уведомление

3.4 Тесты
```

### Phase 4: Optimization (~1 час)

```
4.1 Combined classify + response decision (оптимизация 3)
    - Один LLM-вызов: классификация + роутинг
    - Batch: N сообщений → 1 вызов

4.2 Шаблоны для простых fact-response (оптимизация 4)
    - intent=deadline + данные есть → шаблон без LLM Step 3

4.3 Метрики: notification count, response count, escalation rate
```

**Итого: ~10 часов на всю реализацию.**

---

## 9. PROACTIVE ENGINE — Два предупреждения (owner + клиент)

### Философия

```
Дайджест = "что произошло".     Proactive = "что НАСТУПАЕТ".
Дайджест = ретроспектива.       Proactive = предвидение.
Дайджест = раз в день.          Proactive = за 2-3 дня до даты.
```

Response Engine (секции 1-8) — это **reactive**: клиент спросил → бот ответил.
Proactive Engine — это **anticipatory**: дата наступает → бот действует ДО того, как кто-то спросит.

Два направления:
1. **К owner'у** — напоминания о задачах, сделках, договорах за 2-3 дня до дедлайна
2. **К клиенту** — напоминания об оплате, договорах, deliverables за 2-3 дня до срока

---

### 9.1 PROACTIVE OWNER ALERTS — Напоминания owner'у за 2-3 дня

#### Что проверяем

```
Источник                    Поле              Когда напоминать
────────────────────────    ──────────────    ──────────────────
tasks.due_at                Дата сдачи        За 2 дня до due_at
agreements.due_at           Дата подписания   За 3 дня до due_at
deals.expected_close_at     Дата закрытия     За 3 дня до close_at
invoices.due_at             Дата оплаты       За 2 дня до due_at
```

#### Логика

```
Каждый день в 08:00 GMT+8 (вместе с дайджестом):
│
├─ SELECT tasks WHERE due_at BETWEEN now AND now+2days
│  AND status NOT IN ('done', 'cancelled')
│  AND id NOT IN (SELECT entity_id FROM pending_notifications
│                 WHERE type='owner_task_due' AND status='sent'
│                 AND created_at > now-3days)
│  → Для каждой: создать pending_notification + отправить owner'у
│
├─ SELECT agreements WHERE due_at BETWEEN now AND now+3days
│  AND status NOT IN ('signed', 'completed', 'cancelled')
│  → То же самое
│
├─ SELECT deals WHERE expected_close_at BETWEEN now AND now+3days
│  AND stage NOT IN ('closed_won', 'closed_lost')
│  → То же самое
│
└─ SELECT invoices WHERE due_at BETWEEN now AND now+2days
   AND status NOT IN ('paid', 'cancelled')
   → То же самое
```

#### Dedup

Каждое напоминание отправляется ОДИН раз за цикл. Храним в `pending_notifications` с `type='owner_*'` и `status='sent'`. Проверка: `WHERE entity_id=? AND type=? AND created_at > now - 3 days`. Если уже отправлено — пропускаем.

Повтор: напоминание за 3 дня, потом в дайджесте за 1 день, потом в дайджесте «просрочено». Три точки касания.

#### Шаблоны уведомлений owner'у

```python
PROACTIVE_OWNER_TEMPLATES = {
    "task_due": """📋 Задача через {days_left}д: {description}

👤 {contact_name}
📅 Срок: {due_date}
🔴 Приоритет: {priority}

{context_block}""",

    "agreement_due": """📝 Договор через {days_left}д: {summary}

👤 {contact_name}
💰 {amount}
📅 Срок: {due_date}
📌 Статус: {status}

{context_block}""",

    "deal_closing": """💰 Сделка закрывается через {days_left}д

👤 {contact_name}
💵 {amount} ({stage})
📅 Ожидаемая дата: {close_at}

{context_block}""",

    "invoice_due": """🧾 Счёт через {days_left}д: {amount}

👤 {contact_name}
📅 Срок оплаты: {due_date}
📌 Статус: {status}""",
}
```

#### SQL-запросы

```python
def get_upcoming_tasks(days: int, db_path: str) -> list[dict]:
    """Задачи с due_at в ближайшие N дней."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT t.*, c.name as contact_name
        FROM tasks t
        LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
        WHERE t.due_at BETWEEN datetime('now') AND datetime('now', ? || ' days')
          AND t.status NOT IN ('done', 'cancelled')
        ORDER BY t.due_at ASC
    """, (str(days),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_upcoming_agreements(days: int, db_path: str) -> list[dict]:
    """Договоры с due_at в ближайшие N дней."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT a.*, c.name as contact_name
        FROM agreements a
        JOIN contacts c ON a.contact_id = c.id
        WHERE a.due_at BETWEEN date('now') AND date('now', ? || ' days')
          AND a.status NOT IN ('signed', 'completed', 'cancelled')
        ORDER BY a.due_at ASC
    """, (str(days),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_upcoming_deal_closures(days: int, db_path: str) -> list[dict]:
    """Сделки с expected_close_at в ближайшие N дней."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT d.*, c.name as contact_name
        FROM deals d
        JOIN contacts c ON d.contact_id = c.id
        WHERE d.expected_close_at BETWEEN date('now') AND date('now', ? || ' days')
          AND d.stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY d.expected_close_at ASC
    """, (str(days),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_upcoming_invoices(days: int, db_path: str) -> list[dict]:
    """Счета с due_at в ближайшие N дней."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT i.*, a.summary as agreement_summary,
               c.name as contact_name
        FROM invoices i
        JOIN agreements a ON i.agreement_id = a.id
        JOIN contacts c ON a.contact_id = c.id
        WHERE i.due_at BETWEEN date('now') AND date('now', ? || ' days')
          AND i.status NOT IN ('paid', 'cancelled')
        ORDER BY i.due_at ASC
    """, (str(days),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

#### Was this already sent? (Dedup)

```python
def was_proactive_sent(entity_type: str, entity_id: int,
                       alert_type: str, db_path: str) -> bool:
    """Проверить, отправляли ли уже это proactive-напоминание."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT id FROM pending_notifications
        WHERE entity_type = ? AND entity_id = ?
          AND escalation_type = ?
          AND status = 'sent'
          AND created_at > datetime('now', '-3 days')
    """, (entity_type, entity_id, alert_type)).fetchone()
    conn.close()
    return row is not None
```

#### Интеграция в scheduler

```python
# scheduler.py — добавить в create_scheduler():

# Proactive owner alerts — каждый день в 08:30 (после дайджеста)
scheduler.add_job(
    _proactive_owner_check,
    CronTrigger(hour=8, minute=30, timezone="Asia/Shanghai"),
    args=[bot, brain],
    id="proactive_owner",
)
```

```python
async def _proactive_owner_check(bot, brain):
    """Ежедневная проверка: что наступает в ближайшие 2-3 дня."""
    from services import memory as mem
    from services.proactive import send_owner_alerts
    await send_owner_alerts(bot, settings.db_path)
```

---

### 9.2 PROACTIVE CLIENT REMINDERS — Напоминания клиентам за 2-3 дня

#### Философия

Клиенту напоминаем **только о его obligations** — то, что КЛИЕНТ должен сделать:
- Оплатить счёт
- Подписать договор
- Предоставить материалы
- Подтвердить сроки

НЕ напоминаем о наших внутренних задачах (это owner'у).

#### Когда бот пишет клиенту

```
Сценарий                           Шаблон                      За сколько
──────────────────────────────     ──────────────────────────   ──────────
Счёт не оплачен                    "Напоминаем об оплате"       2 дня
Договор не подписан                "Напоминаем о подписании"    3 дня
Клиент обещал материалы до X       "Напоминаем о материалах"    2 дня
Дедлайн deliverables от клиента    "Подтверждаем сроки"         3 дня
```

#### Ключевое решение: КАК бот пишет клиенту

```
Вариант A: Через business API (can_reply=True)
  Бот пишет ОТ ИМЕНИ owner'а в тот же чат.
  Клиент видит сообщение от Камиля, не от бота.
  Нужно: can_reply=True в business_connections.

Вариант B: Owner'у → owner пишет сам
  Бот уведомляет owner'а: "Напомни клиенту X об оплате".
  Owner пишет сам. Безопаснее, но owner делает работу.
```

**Рекомендация:** Вариант A (по умолчанию) с fallback на B.

```
if can_reply:
    бот пишет клиенту через business API (от имени owner'а)
    + уведомляет owner'а что написал
else:
    бот уведомляет owner'а → owner пишет сам
```

#### Логика

```
Каждый день в 09:00 GMT+8:
│
├─ Найти obligations клиента с due_at в ближайшие 2-3 дня
│
├─ Для каждой:
│  ├─ Проверить dedup (не отправляли в последние 3 дня?)
│  ├─ Проверить can_reply в business_connections
│  │  ├─ can_reply=True → отправить клиенту через business API
│  │  │  + уведомить owner'а что напомнили
│  │  └─ can_reply=False → уведомить owner'а → owner пишет сам
│  └─ Записать в pending_notifications (status='sent')
│
└─ Night mode: если 23-08 → отложить до 08:00 (для owner-alerts)
   Для client reminders: отправлять только в 09:00-20:00 (не спамить ночью)
```

#### Что напоминаем клиенту (SQL)

```python
def get_client_obligations(days: int, db_path: str) -> list[dict]:
    """
    Obligations клиента — то, что КЛИЕНТ должен сделать:
    - Оплатить счёт
    - Подписать договор
    - Предоставить материалы (tasks с related=client obligation)
    """
    conn = get_db(db_path)
    results = []

    # 1. Неоплаченные счета
    rows = conn.execute("""
        SELECT 'invoice' as type, i.id as entity_id,
               i.amount, i.due_at, i.status,
               a.summary as agreement_summary,
               c.id as contact_id, c.name as contact_name,
               ct.tg_chat_id
        FROM invoices i
        JOIN agreements a ON i.agreement_id = a.id
        JOIN contacts c ON a.contact_id = c.id
        LEFT JOIN chat_threads ct ON ct.contact_id = c.id
        WHERE i.due_at BETWEEN date('now') AND date('now', ? || ' days')
          AND i.status IN ('sent', 'overdue')
        ORDER BY i.due_at ASC
    """, (str(days),)).fetchall()
    results.extend([dict(r) for r in rows])

    # 2. Неподписанные договоры
    rows = conn.execute("""
        SELECT 'agreement' as type, a.id as entity_id,
               a.amount, a.due_at, a.status, a.summary,
               c.id as contact_id, c.name as contact_name,
               ct.tg_chat_id
        FROM agreements a
        JOIN contacts c ON a.contact_id = c.id
        LEFT JOIN chat_threads ct ON ct.contact_id = c.id
        WHERE a.due_at BETWEEN date('now') AND date('now', ? || ' days')
          AND a.status IN ('sent', 'draft')
        ORDER BY a.due_at ASC
    """, (str(days),)).fetchall()
    results.extend([dict(r) for r in rows])

    # 3. Tasks-обязательства клиента (помечены как client-facing)
    #    Определяем по ключевым словам в description:
    #    "клиент должен", "предоставить", "прислать", "подтвердить", "оплатить"
    rows = conn.execute("""
        SELECT 'client_task' as type, t.id as entity_id,
               t.description, t.due_at, t.priority,
               c.id as contact_id, c.name as contact_name,
               ct.tg_chat_id
        FROM tasks t
        LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
        LEFT JOIN chat_threads ct ON ct.contact_id = c.id
        WHERE t.due_at BETWEEN datetime('now') AND datetime('now', ? || ' days')
          AND t.status NOT IN ('done', 'cancelled')
          AND (
              LOWER(t.description) LIKE '%клиент должен%'
              OR LOWER(t.description) LIKE '%предоставит%'
              OR LOWER(t.description) LIKE '%прислал%'
              OR LOWER(t.description) LIKE '%подтвердит%'
              OR LOWER(t.description) LIKE '%оплат%'
              OR LOWER(t.description) LIKE '%подписать%'
          )
        ORDER BY t.due_at ASC
    """, (str(days),)).fetchall()
    results.extend([dict(r) for r in rows])

    conn.close()
    return results
```

#### Шаблоны напоминаний клиенту

```python
CLIENT_REMINDER_TEMPLATES = {
    "invoice": (
        "Добрый день! 🧾\n\n"
        "Напоминаем об оплате по договору «{agreement_summary}».\n"
        "Сумма: {amount}\n"
        "Срок: {due_date}\n\n"
        "Если уже оплатили — проигнорируйте это сообщение."
    ),

    "agreement": (
        "Добрый день! 📝\n\n"
        "Напоминаем о подписании договора «{summary}».\n"
        "{amount_line}"
        "Срок: {due_date}\n\n"
        "Если есть вопросы — напишите, поможем."
    ),

    "client_task": (
        "Добрый день! 📋\n\n"
        "Напоминаем: {description}\n"
        "Срок: {due_date}\n\n"
        "Если уже сделали — проигнорируйте."
    ),
}
```

#### Отправка клиенту

```python
async def send_client_reminder(bot, obligation, biz_conn_id):
    """
    Отправить напоминание клиенту через business API.
    От имени owner'а (клиент видит сообщение от Камиля).
    """
    template = CLIENT_REMINDER_TEMPLATES.get(obligation["type"])
    if not template:
        return False

    # Формируем текст
    amount = f"{obligation.get('amount', 0):,.0f}₽" if obligation.get("amount") else ""
    text = template.format(
        agreement_summary=obligation.get("agreement_summary", ""),
        summary=obligation.get("summary", obligation.get("description", "")),
        amount=amount,
        amount_line=f"Сумма: {amount}\n" if amount else "",
        due_date=obligation.get("due_at", "")[:10],
        description=obligation.get("description", ""),
    )

    chat_id = obligation.get("tg_chat_id")
    if not chat_id:
        return False

    try:
        await bot.send_message(
            chat_id=int(chat_id),
            text=text,
            business_connection_id=biz_conn_id,
        )
        return True
    except Exception as e:
        logger.error(f"Client reminder failed: {e}")
        return False
```

#### Owner notification при client reminder

```python
# Когда бот напомнил клиенту → уведомляем owner'а

CLIENT_SENT_NOTIFY = """✅ Напомнил {contact_name}: {what}

💬 «{reminder_text_short}»"""

# Когда НЕ смог написать (can_reply=False) → просим owner'а

CLIENT_FALLBACK_NOTIFY = """📋 Напомни {contact_name} — {what}

📅 Срок: {due_date}
{amount_line}
⚠️ Не могу написать сам (нет прав на ответ). Напишите клиенту."""
```

#### Интеграция в scheduler

```python
# scheduler.py — добавить в create_scheduler():

# Proactive client reminders — каждый день в 09:00
scheduler.add_job(
    _proactive_client_check,
    CronTrigger(hour=9, minute=0, timezone="Asia/Shanghai"),
    args=[bot],
    id="proactive_client",
)
```

```python
async def _proactive_client_check(bot):
    """Ежедневная проверка: obligations клиента в ближайшие 2-3 дня."""
    from services.proactive import send_client_reminders
    await send_client_reminders(bot, settings.db_path)
```

---

### 9.3 PROGRESS CHECK-IN — Запрос подтверждения прогресса у owner'а

#### Проблема

Сейчас: задача создаётся → лежит в CRM → owner забывает → задача просрочена → дайджест показывает «⚠️ просрочено».

Нужно: бот периодически спрашивает owner'а «как дела с задачей X?» — ДО просрочки.

#### Логика

```
Каждый день в 10:00 GMT+8:
│
├─ Найти задачи:
│  status='open' или 'in_progress'
│  due_at > now (не просрочены)
│  due_at < now + 5 дней (скоро дедлайн)
│  last_progress_check < now - 2 дня (не спрашивали >2 дней)
│
├─ Owner в ночном режиме (23-08)?
│  └─ Да → отложить до 08:00
│
├─ Для каждой задачи:
│  ├─ Собрать контекст: кто contact, что за сделка, когда deadline
│  ├─ Отправить owner'у в личку бота
│  └─ Записать last_progress_check = now
│
└─ Dedup: не спрашивать одну задачу чаще раза в 2 дня
```

#### Что спрашиваем

Не просто «как дела?» — а с контекстом:

```
📋 Check-in: {description}

👤 Клиент: {contact_name}
💰 Сделка: {amount} ({stage})
📅 Дедлайн: {due_date} (осталось {days_left}д)
📌 Статус: {status}

Последнее в чате:
{last_messages}

Как дела? Напишите «готово», «в работе», или опишите статус.
```

#### Обработка ответа owner'а — LLM-based (НЕ regex)

Owner отвечает в личку бота. Ответ может быть любым:
- «готово, клиент подписал»
- «в процессе, жду от бухгалтера, будет в четверг»
- «не получится к сроку, перенеси на следующую неделю»
- «да ладно, забей на эту задачу»
- «хорошо, но нужно уточнить сумму — позвоню завтра»

Regex на это не способен. Нужен LLM.

```
Owner пишет:                         LLM понимает:
───────────────────────────────────   ─────────────────────────────
"готово, клиент подписал"            → action=done, notes="клиент подписал"
"в процессе, будет в четверг"       → action=in_progress, notes="срок: четверг"
"перенеси на следующую неделю"      → action=extend, new_date="2026-04-07"
"зависло, жду бухгалтерию"          → action=blocked, notes="блокер: бухгалтерия"
"забей, отменяем"                   → action=cancel
"напомни завтра"                    → action=remind_tomorrow
"сложная ситуация, клиент угрожает" → action=escalate, notes="риск: жалоба"
"не уверен, надо подумать"          → action=in_progress, notes="нужно решение"
```

```python
PROGRESS_REPLY_SYSTEM = """Ты — помощник владельца бизнеса в CRM-системе Lenochka.
Owner ответил на check-in сообщение о задаче. Определи что делать с задачей.

Задача: {task_description}
Дедлайн: {due_at}
Текущий статус: {status}

Ответ owner'а: {owner_reply}

Определи action (один из):
- "done" — задача выполнена
- "in_progress" — в работе, продвигается
- "extend" — нужно продлить срок (укажи new_date или extend_days)
- "blocked" — заблокирована, есть препятствие
- "cancel" — задача отменена
- "remind_tomorrow" — напомнить завтра
- "remind_date" — напомнить в конкретную дату
- "escalate" — проблема, нужно вмешательство
- "update" — просто обновление статуса, без изменения задачи

Отвечай ТОЛЬКО JSON:
{"action":"<тип>","new_date":"YYYY-MM-DD или null","extend_days":N или null,
 "notes":"<что сказал owner, кратко>","priority":"<low|normal|high|urgent или null>"}
"""


def parse_progress_reply_llm(text: str, task: dict, brain) -> dict:
    """
    LLM понимает ответ owner'а и определяет что делать с задачей.
    ОДИН LLM-вызов. ~$0.001.

    Возвращает {action, new_date, extend_days, notes, priority}.
    """
    user_prompt = ""  # всё в system prompt через format

    result = brain._call_llm(
        PROGRESS_REPLY_SYSTEM.format(
            task_description=task.get("description", "?"),
            due_at=task.get("due_at", "не указан"),
            status=task.get("status", "open"),
            owner_reply=text,
        ),
        user_prompt,
        temperature=0.0,
        max_tokens=300,
    )

    if result:
        data = brain._extract_json(result)
        if isinstance(data, dict) and "action" in data:
            return data

    # Fallback при ошибке LLM — записать как notes, не терять
    return {
        "action": "update",
        "new_date": None,
        "extend_days": None,
        "notes": text,
        "priority": None,
    }
```

#### Owner reply → task update SQL

```python
def apply_progress_update(task_id: int, decision: dict,
                           db_path: str) -> str:
    """Обновить задачу на основе LLM-решения."""
    conn = get_db(db_path)
    action = decision.get("action", "update")
    now_note = f"[{_now_gmt8().strftime('%m-%d %H:%M')}]"

    try:
        if action == "done":
            conn.execute(
                "UPDATE tasks SET status='done', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )

        elif action == "in_progress":
            conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )

        elif action == "extend":
            if decision.get("new_date"):
                conn.execute(
                    "UPDATE tasks SET due_at=?, updated_at=datetime('now') WHERE id=?",
                    (decision["new_date"], task_id),
                )
            elif decision.get("extend_days"):
                conn.execute(
                    "UPDATE tasks SET due_at=datetime('now', ? || ' days'), updated_at=datetime('now') WHERE id=?",
                    (str(decision["extend_days"]), task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET due_at=datetime('now', '+3 days'), updated_at=datetime('now') WHERE id=?",
                    (task_id,),
                )

        elif action == "blocked":
            conn.execute(
                "UPDATE tasks SET priority='urgent', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )

        elif action == "cancel":
            conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )

        # Всегда пишем notes если есть
        if decision.get("notes"):
            conn.execute(
                "UPDATE tasks SET notes = COALESCE(notes || '\n', '') || ? WHERE id=?",
                (f"{now_note} {decision['notes']}", task_id),
            )

        # Priority override
        if decision.get("priority"):
            conn.execute(
                "UPDATE tasks SET priority=? WHERE id=?",
                (decision["priority"], task_id),
            )

        conn.commit()
    finally:
        conn.close()

    return _format_progress_confirmation(action, decision)
```

#### Flow полного цикла

```
Day 0: Клиент пишет "сделаю КП до пятницы"
  → extract: task "сделать КП", due_at="2026-04-04"
  → crm_upsert: INSERT tasks

Day 2 (Tuesday 10:00): proactive check-in
  → "📋 Check-in: сделать КП для Ивана. Дедлайн через 2д. Как дела?"
  → Owner: "в работе"
  → UPDATE tasks SET status='in_progress'

Day 3 (Wednesday 10:00): proactive check-in
  → "📋 Check-in: сделать КП. Дедлайн завтра. Статус: in_progress."
  → Owner: "готово"
  → UPDATE tasks SET status='done'

Day 4 (Thursday): задача done — не проверяем
```

#### Owner reply → task update SQL

```python
def apply_progress_update(task_id: int, action: str, detail,
                           db_path: str) -> str:
    """Обновить задачу на основе ответа owner'а."""
    conn = get_db(db_path)

    if action == 'mark_done':
        conn.execute("""
            UPDATE tasks SET status='done', updated_at=datetime('now')
            WHERE id = ?
        """, (task_id,))
        conn.commit()
        conn.close()
        return "✅ Задача закрыта"

    elif action == 'mark_in_progress':
        conn.execute("""
            UPDATE tasks SET status='in_progress', updated_at=datetime('now')
            WHERE id = ?
        """, (task_id,))
        conn.commit()
        conn.close()
        return "🔨 Статус: в работе"

    elif action == 'extend_deadline':
        days = detail if isinstance(detail, int) else 3
        conn.execute("""
            UPDATE tasks
            SET due_at = datetime('now', ? || ' days'),
                updated_at = datetime('now')
            WHERE id = ?
        """, (str(days), task_id))
        conn.commit()
        conn.close()
        return f"📅 Дедлайн продлён на {days} дней"

    elif action == 'mark_blocked':
        conn.execute("""
            UPDATE tasks SET priority='urgent', updated_at=datetime('now')
            WHERE id = ?
        """, (task_id,))
        conn.commit()
        conn.close()
        return "⚠️ Приоритет повышен до urgent"

    elif action == 'update_notes':
        conn.execute("""
            UPDATE tasks
            SET notes = COALESCE(notes || '\n', '') || ?
            WHERE id = ?
        """, (f"[{_now_gmt8().strftime('%m-%d %H:%M')}] {detail}", task_id))
        conn.commit()
        conn.close()
        return "📝 Записал обновление"

    conn.close()
    return ""
```

#### Новый middleware: Progress Reply Handler

Progress check-in ответы owner'а — это НЕ обычные direct messages. Owner отвечает В ОТВЕТ на сообщение бота о задаче. Нужно распознать что это ответ на check-in.

```python
def _format_progress_confirmation(action: str, decision: dict) -> str:
    """Форматировать подтверждение обновления задачи для owner'а."""
    notes = decision.get("notes", "")
    if action == "done":
        return f"✅ Задача закрыта" + (f" — {notes}" if notes else "")
    elif action == "in_progress":
        return f"🔨 В работе" + (f" — {notes}" if notes else "")
    elif action == "extend":
        if decision.get("new_date"):
            return f"📅 Дедлайн → {decision['new_date']}" + (f" — {notes}" if notes else "")
        days = decision.get("extend_days", 3)
        return f"📅 Дедлайн продлён на {days}д" + (f" — {notes}" if notes else "")
    elif action == "blocked":
        return f"⚠️ Заблокировано (urgent)" + (f" — {notes}" if notes else "")
    elif action == "cancel":
        return f"❌ Задача отменена" + (f" — {notes}" if notes else "")
    elif action == "remind_tomorrow":
        return f"🔔 Напомню завтра"
    elif action == "remind_date":
        return f"🔔 Напомню {decision.get('new_date', '?')}"
    elif action == "escalate":
        return f"🚨 Эскалировано" + (f" — {notes}" if notes else "")
    return f"📝 Записал" + (f": {notes}" if notes else "")


# handlers/commands.py — новый обработчик:

@router.message(F.chat.type == "private", F.reply_to_message)
async def on_owner_reply_to_checkin(
    message: Message, brain, is_owner: bool = False, **kwargs
):
    """
    Owner ответил на сообщение бота в личке.
    Проверяем: это ответ на progress check-in?
    Если да — LLM понимает ответ, обновляет задачу.
    """
    if not is_owner:
        return

    original_text = message.reply_to_message.text or ""
    task_id = _extract_task_id_from_checkin(original_text)

    if not task_id:
        return  # Не check-in, пропускаем

    # Загружаем задачу для контекста
    task = _get_task_by_id(task_id, settings.db_path)
    if not task:
        await message.answer("⚠️ Задача не найдена (возможно удалена)")
        return

    # LLM понимает ответ owner'а
    decision = parse_progress_reply_llm(message.text, task, brain)

    # Применяем
    result = apply_progress_update(task_id, decision, settings.db_path)
    await message.answer(result)


def _extract_task_id_from_checkin(bot_message_text: str) -> int | None:
    """Извлечь task_id из маркера [task:ID] в check-in сообщении."""
    match = re.search(r'\[task:(\d+)\]', bot_message_text)
    return int(match.group(1)) if match else None


def _get_task_by_id(task_id: int, db_path: str) -> dict | None:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
```

#### Интеграция в scheduler

```python
# scheduler.py — добавить в create_scheduler():

# Progress check-in — каждый день в 10:00
scheduler.add_job(
    _progress_checkin,
    CronTrigger(hour=10, minute=0, timezone="Asia/Shanghai"),
    args=[bot],
    id="progress_checkin",
)
```

```python
async def _progress_checkin(bot):
    """Ежедневный check-in: задачи со сроком 1-5 дней, не проверяли >2 дня."""
    from services.proactive import send_progress_checkins
    await send_progress_checkins(bot, settings.db_path)
```

---

### 9.4 ОБЩИЙ РАСПОРЯДОК PROACTIVE CHECKS

```
Время (GMT+8)    Что                         Кому
──────────────    ──────────────────────────   ──────────────
08:00             Утренний дайджест            Owner (личка бота)
08:30             Proactive owner alerts       Owner (личка бота)
                  (задачи, договоры, сделки,
                   счета — за 2-3 дня)
09:00             Client reminders             Клиент (business API)
                  (оплата, подписание,          или Owner если нет прав
                   материалы — за 2-3 дня)
10:00             Progress check-in            Owner (личка бота)
                  (задачи due в 1-5 дней,
                   не проверяли >2 дня)
18:00 (Sunday)    Weekly report                Owner (личка бота)
──────────────    ──────────────────────────   ──────────────
Каждые 4ч         Abandoned dialogues          Owner (личка бота)
03:00             Consolidate (decay+merge)    Фоновый процесс
```

#### Night mode (23:00-08:00)

```
Proactive owner alerts:  → отложить до 08:00
Progress check-in:       → отложить до 08:00
Client reminders:        → отправлять только 09:00-20:00
                          (клиентам не пишем ночью)
Дайджест:                → по расписанию (08:00)
Complaint escalation:    → СРАЗУ, даже ночью (10 мин)
```

---

### 9.5 НОВЫЕ ФАЙЛЫ

```
lenochka-bot/services/
│
├── proactive.py              (~350 строк) — НОВЫЙ
│   ├── send_owner_alerts()         — проверка tasks/agreements/deals/invoices
│   ├── send_client_reminders()     — obligations клиента → business API
│   ├── send_progress_checkins()    — задачи due soon, check-in owner'у
│   ├── get_upcoming_tasks()
│   ├── get_upcoming_agreements()
│   ├── get_upcoming_deal_closures()
│   ├── get_upcoming_invoices()
│   ├── get_client_obligations()
│   ├── was_proactive_sent()        — dedup
│   ├── send_client_reminder()      — через business API
│   ├── format_owner_alert()        — шаблон для owner'а
│   ├── PROACTIVE_OWNER_TEMPLATES
│   ├── CLIENT_REMINDER_TEMPLATES
│   └── CLIENT_SENT_NOTIFY / CLIENT_FALLBACK_NOTIFY
│
└── progress.py               (~200 строк) — НОВЫЙ
    ├── parse_progress_reply_llm()  — LLM понимает ответ owner'а → action
    ├── PROGRESS_REPLY_SYSTEM prompt
    ├── apply_progress_update()     — action → SQL UPDATE tasks
    ├── _format_progress_confirmation()
    ├── _extract_task_id_from_checkin()
    ├── _get_task_by_id()
    └── CHECKIN_TEMPLATE            — шаблон сообщения для owner'а
```

#### Изменения в существующих

```
lenochka-bot/services/scheduler.py
  + _proactive_owner_check (08:30)
  + _proactive_client_check (09:00)
  + _progress_checkin (10:00)

lenochka-bot/handlers/commands.py
  + on_owner_reply_to_checkin — обработка ответов на progress check-in

lenochka-memory/schemas/init.sql
  + ALTER TABLE pending_notifications ADD COLUMN entity_type TEXT
  + ALTER TABLE pending_notifications ADD COLUMN entity_id INTEGER
  + tasks.last_progress_check DATETIME (или хранить в pending_notifications)
```

---

### 9.6 COST ANALYSIS (Proactive Engine)

```
Proactive Engine = 0 LLM-вызовов.

Всё на SQL + regex + шаблоны:
├── get_upcoming_*          → SQL (бесплатно)
├── get_client_obligations  → SQL (бесплатно)
├── parse_progress_reply    → regex (бесплатно)
├── apply_progress_update   → SQL (бесплатно)
├── send_client_reminder    → Telegram API (бесплатно)
└── format_owner_alert      → string format (бесплатно)

Итого: $0/мес дополнительных расходов.
Только Telegram API calls (free tier: 30 msg/sec).
```

---

### 9.7 EDGE CASES

#### Owner ответил "готово" на задачу, которую бот напомнил клиенту

```
t=09:00  Бот напомнил клиенту: "Напоминаем об оплате"
t=09:05  Owner: "готово" (ответ на check-in другой задачи)
→ Нет конфликта. Client reminder уже отправлен.
→ Если клиент оплатил — owner помечает invoice как paid.
→ Бот не напоминает повторно (dedup по entity_id + sent status).
```

#### Клиент уже оплатил, но бот напомнил

```
→ Owner должен обновить invoice.status = 'paid' когда узнает.
→ До этого бот напомнит (ложноположительное).
→ Решение: в шаблоне напоминания добавить "Если уже оплатили — проигнорируйте".
→ Owner может написать боту "оплатил" → обновить invoice status.
```

#### Несколько obligations у одного клиента

```
Клиент Иван: счёт на 100к (2 дня) + договор на 50к (3 дня)
→ Два отдельных напоминания?
→ Лучше: одно сообщение со списком.
→ Агрегируем obligations по contact перед отправкой.

CLIENT_REMINDER_AGGREGATED = """Добрый день! 📋

Напоминаем:
{items_list}

Если уже всё сделали — проигнорируйте."""
```

#### Client reminder → клиент ответил → что делать?

```
Клиент: "да, оплачу завтра"
→ Бот НЕ отвечает (response engine решит).
→ Если response engine: intent="chitchat" или "ended" → skip.
→ Если response engine: intent="complaint" → escalate.

Клиент: "я уже оплатил"
→ Бот НЕ отвечает.
→ Owner увидит в дайджесте / через ingest.
```

---

### 9.8 ИСПРАВЛЕНИЕ КРАСНЫХ И ЖЁЛТЫХ ПРОБЛЕМ

Эта секция — сводка всех критических и серьёзных проблем из аудита Response Engine + их решения.

---

#### 9.8.1 🔴 FOLLOW-UP DETECTION — «договорились в среду» → напомнить в среду

**Проблема:** extract_entities достаёт `agreement.due_date = "2026-04-03"`. Записывается в CRM. В четверг 3 апреля никто не напомнит.

**Решение:** Follow-up detector — LLM извлекает implicit obligations из текста и создаёт scheduled reminders.

```python
FOLLOWUP_DETECT_SYSTEM = """Ты — детектор договорённостей и follow-up обязательств.
Из сообщения извлеки все договорённости, сроки и обязательства.

Примеры:
- "Договорились, пришлю КП в среду" → obligation: "пришлёт КП", due: среда, who: owner
- "Клиент сказал оплатит до пятницы" → obligation: "клиент оплатит", due: пятница, who: client
- "Напишу ему завтра" → obligation: "написать клиенту", due: завтра, who: owner
- "Подожди до конца месяца" → obligation: "ждать", due: конец месяца, who: owner
- "Ладно, тогда в понедельник обсудим" → obligation: "обсудить", due: понедельник, who: both

Верни JSON array (может быть несколько):
[{"obligation":"<что сделать>","who":"owner|client|both",
  "due_hint":"<естественное описание срока>","due_date":"YYYY-MM-DD или null",
  "reminder_days_before":2}]

Если нет договорённостей — пустой массив []."""


def detect_followups(text: str, chat_context: str, brain) -> list[dict]:
    """
    LLM ищет implicit obligations в тексте.
    Вызывается из pipeline._finalize_item() после extract_entities.
    Не создаёт LLM-вызов для noise/chit-chat.
    """
    result = brain._call_llm(
        FOLLOWUP_DETECT_SYSTEM,
        f"Сообщение:\n{text}\n\nКонтекст чата:\n{chat_context}",
        temperature=0.0,
        max_tokens=500,
    )

    if result:
        data = brain._extract_json(result)
        if isinstance(data, list):
            return data

    return []
```

**Интеграция в pipeline:**

```python
# pipeline.py — в _finalize_item(), после extract_entities:

# Follow-up detection (только для бизнес-типов)
if label in ("task", "decision", "lead-signal", "risk", "business-small"):
    followups = await asyncio.to_thread(
        detect_followups, nm.full_text, chat_ctx, self.brain
    )
    for fu in followups:
        due_date = fu.get("due_date")
        if due_date:
            # Создаём task с due_at если его нет
            await asyncio.to_thread(
                _create_followup_task, fu, item.contact_id,
                item.chat_thread_id, self.db_path
            )
            # Scheduled proactive reminder через N дней до due_at
            remind_days = fu.get("reminder_days_before", 2)
            await asyncio.to_thread(
                _schedule_followup_reminder, fu, remind_days, self.db_path
            )
```

**Где напоминать:**
- `who="owner"` → proactive owner alert за 2 дня до due_date
- `who="client"` → client reminder за 2 дня до due_date
- `who="both"` → оба

Follow-up obligations записываются как tasks с `due_at`. Стандартный proactive engine (9.1 + 9.2) подхватит их через `get_upcoming_tasks()` и `get_client_obligations()`.

---

#### 9.8.2 🔴 TIMER PERSISTENCE — Escalation timers не теряются при рестарте

**Проблема:** `check_and_notify_later()` = `asyncio.create_task()` + `asyncio.sleep(delay)`. Рестарт бота → все in-flight timers lost. Клиент спросил «сколько стоит?» → restart → owner никогда не узнает.

**Решение:** Pending notifications table уже существует. При старте бота — recovery.

```python
# __main__.py — в on_startup:

async def on_startup(**kwargs):
    await pipeline.start()
    scheduler.start()

    # Startup recovery: восстановить pending notifications
    from services.notifier import recover_pending_notifications
    await recover_pending_notifications(bot, settings.db_path)

    logger.info("Lenochka started ✓")
```

```python
# notifier.py — новый:

async def recover_pending_notifications(bot, db_path: str):
    """
    При старте бота: проверить pending_notifications.
    - notify_at < now → отправить owner'у (просроченные)
    - notify_at > now → запланировать asyncio task
    """
    conn = get_db(db_path)
    try:
        pending = conn.execute("""
            SELECT * FROM pending_notifications
            WHERE status = 'pending'
            ORDER BY notify_at ASC
        """).fetchall()
    finally:
        conn.close()

    now = datetime.now(GMT8)
    recovered = 0

    for p in pending:
        notify_at = datetime.fromisoformat(p["notify_at"])
        remaining = (notify_at - now).total_seconds()

        if remaining <= 0:
            # Просроченное — отправить сразу
            await _send_pending_notification(bot, p, db_path)
            recovered += 1
        else:
            # Будущее — запланировать task
            asyncio.create_task(
                _wait_and_notify(bot, p, remaining, db_path)
            )
            recovered += 1

    if recovered:
        logger.info(f"Recovered {recovered} pending notifications")
```

```python
async def _wait_and_notify(bot, pending_row, delay, db_path):
    """Ждём delay секунд, проверяем статус, отправляем."""
    await asyncio.sleep(delay)

    # Проверяем что не отменили за время ожидания
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM pending_notifications WHERE id=?",
            (pending_row["id"],),
        ).fetchone()
    finally:
        conn.close()

    if row and row["status"] == "pending":
        await _send_pending_notification(bot, pending_row, db_path)
```

---

#### 9.8.3 🟡 EXPANDED FACT QUERIES — 10+ интентов вместо 4

**Проблема:** `query_fact()` обрабатывает только `deadline`, `status`, `amount`, `context_recall`. Всё остальное → escalate. Много вопросов, на которые есть данные в БД, уходят owner'у.

**Решение:** Расширяем до 10+ интентов с конкретными SQL-запросами.

```python
def query_fact(intent: str, query_hint: str, contact_id: int,
               chat_thread_id: int, db_path: str) -> str | None:
    """Роутер: intent → конкретная SQL-функция."""

    queries = {
        "deadline":         query_deadline,
        "status":           query_status,
        "amount":           query_amount,
        "context_recall":   query_context,
        # Новые:
        "payment_status":   query_payment_status,
        "overdue":          query_overdue,
        "tasks_today":      query_tasks_today,
        "active_leads":     query_leads_summary,
        "deal_details":     query_deal_details,
        "contact_history":  query_contact_history,
        "last_interaction": query_last_interaction,
    }

    fn = queries.get(intent)
    if not fn:
        return None

    return fn(contact_id, query_hint, chat_thread_id, db_path)


def query_payment_status(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Статус оплаты: неоплаченные счета, последние платежи."""
    conn = get_db(db_path)
    parts = []

    # Неоплаченные счета
    rows = conn.execute("""
        SELECT i.amount, i.due_at, i.status, a.summary
        FROM invoices i
        JOIN agreements a ON i.agreement_id = a.id
        WHERE a.contact_id = ? AND i.status IN ('sent', 'overdue')
        ORDER BY i.due_at ASC
    """, (contact_id,)).fetchall()
    for r in rows:
        overdue = " ⚠️ просрочен" if r["status"] == "overdue" else ""
        parts.append(f"Счёт {r['amount']:,.0f}₽ по «{r['summary']}» — до {r['due_at'][:10]}{overdue}")

    # Последние платежи
    rows = conn.execute("""
        SELECT p.amount, p.paid_at, p.method, a.summary
        FROM payments p
        JOIN invoices i ON p.invoice_id = i.id
        JOIN agreements a ON i.agreement_id = a.id
        WHERE a.contact_id = ? AND p.status = 'confirmed'
        ORDER BY p.paid_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"Оплата {r['amount']:,.0f}₽ ({r['method'] or '—'}) — {r['paid_at'][:10]}")

    conn.close()
    return "\n".join(parts) if parts else None


def query_overdue(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Просроченные задачи и счета."""
    conn = get_db(db_path)
    parts = []

    # Просроченные задачи
    rows = conn.execute("""
        SELECT description, due_at, priority
        FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND due_at < datetime('now')
          AND status NOT IN ('done', 'cancelled')
        ORDER BY due_at ASC
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"📋 {r['description'][:60]} — просрочено (было {r['due_at'][:10]})")

    # Просроченные счета
    rows = conn.execute("""
        SELECT i.amount, i.due_at, a.summary
        FROM invoices i
        JOIN agreements a ON i.agreement_id = a.id
        WHERE a.contact_id = ? AND i.status = 'overdue'
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"🧾 Счёт {r['amount']:,.0f}₽ — просрочен (было {r['due_at'][:10]})")

    conn.close()
    return "\n".join(parts) if parts else None


def query_tasks_today(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Задачи на сегодня."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT description, priority, status, due_at
        FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND date(due_at) = date('now')
          AND status NOT IN ('done', 'cancelled')
        ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
    """, (contact_id,)).fetchall()
    conn.close()

    if rows:
        return "\n".join(
            f"{'🔴' if r['priority']=='urgent' else '🟡' if r['priority']=='high' else '⚪'} {r['description'][:60]}"
            for r in rows
        )
    return None


def query_leads_summary(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Сводка по активным лидам."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT l.amount, l.probability, l.status, c.name
        FROM leads l
        JOIN contacts c ON l.contact_id = c.id
        WHERE l.status NOT IN ('won', 'lost')
        ORDER BY l.created_at DESC LIMIT 5
    """).fetchall()
    conn.close()

    if rows:
        return "\n".join(
            f"• {r['name']}: {r['amount'] or '?'}₽ ({r['status']}, {r['probability']*100:.0f}%)"
            for r in rows
        )
    return None


def query_deal_details(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Детали сделки: сумма, стадия, сроки, заметки."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT d.*, c.name as contact_name
        FROM deals d
        JOIN contacts c ON d.contact_id = c.id
        WHERE d.contact_id = ? AND d.stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY d.updated_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    conn.close()

    if row:
        amt = f"{row['amount']:,.0f}₽" if row.get("amount") else "сумма не указана"
        due = f", до {row['expected_close_at'][:10]}" if row.get("expected_close_at") else ""
        notes = f"\nЗаметки: {row['notes']}" if row.get("notes") else ""
        return f"Сделка с {row['contact_name']}: {amt}, стадия: {row['stage']}{due}{notes}"
    return None


def query_contact_history(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """История общения с контактом: ключевые memories."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT content, type, importance, created_at
        FROM memories
        WHERE contact_id = ?
        ORDER BY importance DESC, created_at DESC
        LIMIT 10
    """, (contact_id,)).fetchall()
    conn.close()

    if rows:
        return "\n".join(f"• [{r['type']}] {r['content'][:80]}" for r in rows)
    return None


def query_last_interaction(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Последнее взаимодействие с контактом."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT m.text, m.sent_at, ct.title
        FROM messages m
        JOIN chat_threads ct ON m.chat_thread_id = ct.id
        WHERE ct.contact_id = ?
          AND (m.meta_json IS NULL OR json_extract(m.meta_json, '$.deleted') IS NULL)
        ORDER BY m.sent_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    conn.close()

    if row:
        return f"Последнее сообщение ({row['sent_at'][:16]}): «{row['text'][:100]}»"
    return None
```

**Расширенный CLASSIFY_SYSTEM** — теперь LLM может выбрать один из 10+ интентов:

```python
# В RESPONSE_DECISION_SYSTEM — добавить интенты:

RESPONSE_DECISION_INTENTS = """
Определи intent (один из):
- "deadline" — когда будет готово? когда прислать?
- "status" — что там с проектом/задачей?
- "amount" — сколько договорились? какая сумма?
- "context_recall" — напомни о чём говорили, что решили
- "payment_status" — что с оплатой? когда оплатят? оплатил?
- "overdue" — что просрочено? что зависло?
- "tasks_today" — что на сегодня? какие задачи?
- "active_leads" — сколько лидов? что с лидами?
- "deal_details" — детали по сделке (сумма, стадия, сроки)
- "contact_history" — что мы знаем об этом клиенте?
- "last_interaction" — когда последний раз общались?
- "pricing" — сколько стоит? какая цена? (→ escalate)
- "proposal" — пришлите КП/предложение (→ escalate)
- "contract" — договор, подписание (→ escalate)
- "meeting" — встреча, созвон, когда удобно (→ escalate)
- "complaint" — жалоба, задержка, обещали (→ escalate)
- "ended" — ок, согласен, договорились, понял (→ skip)
- "chitchat" — привет, как дела, эмоции (→ skip)
- "other" — не удалось определить (→ escalate для безопасности)
"""
```

---

#### 9.8.4 🟡 COMBINED CLASSIFY + RESPONSE DECISION — Primary Architecture

**Проблема:** Pipeline делает batch classify (1 LLM-вызов). Потом отдельно response decision (ещё 1 LLM-вызов на каждое). Это 2 вызова. Combined prompt экономит 40%.

**Решение:** Combined classify + route = ОСНОВНАЯ реализация, не оптимизация.

```python
COMBINED_SYSTEM = """Ты — классификатор и роутер сообщений для CRM-системы Lenochka.

Для КАЖДОГО сообщения определи:

1. Классификация:
   noise / chit-chat / business-small / task / decision / lead-signal / risk / other

2. Действие:
   - "respond_fact" — клиент спрашивает, можно ответить из CRM
   - "escalate" — клиенту нужен ответ от владельца
   - "skip" — не требует ответа (ок, согласен, эмоции, спам)

3. Если respond_fact — укажи intent и query_hint:
   deadline / status / amount / context_recall / payment_status / overdue /
   tasks_today / active_leads / deal_details / contact_history / last_interaction

Отвечай СТРОГО JSON array, ровно N элементов:
[{
  "label":"<категория>",
  "confidence":0.0-1.0,
  "action":"respond_fact|escalate|skip",
  "intent":"<тип или null>",
  "query_hint":"<что искать или null>",
  "reason":"<кратко>"
}, ...]"""


def classify_and_route_batch(texts: list[str],
                              chat_contexts: list[str]) -> list[dict]:
    """
    ОДИН LLM-вызов: классификация + роутинг для N сообщений.
    Возвращает list[{label, confidence, action, intent, query_hint, reason}].
    Заменяет separate classify_batch + N * decide_response.
    """
    numbered = []
    for i, (text, ctx) in enumerate(zip(texts, chat_contexts), 1):
        ctx_str = f" [контекст: {ctx}]" if ctx else ""
        numbered.append(f"{i}. {text}{ctx_str}")

    messages_block = "\n".join(numbered)
    result = _call_llm(
        COMBINED_SYSTEM,
        f"Сообщения ({len(texts)} штук):\n{messages_block}",
        temperature=0.0,
        max_tokens=2048,
    )

    if result:
        data = _extract_json(result)
        if isinstance(data, list) and len(data) == len(texts):
            return data
        # Partial
        if isinstance(data, list) and 0 < len(data) < len(texts):
            fallback = [{"label": "other", "confidence": 0.3,
                         "action": "escalate", "intent": "other",
                         "query_hint": None, "reason": "partial"}] * (len(texts) - len(data))
            return data + fallback

    # Fallback: heuristic classify + escalate
    return [{"label": _classify_heuristic(t)[0],
             "confidence": 0.4, "action": "escalate",
             "intent": "other", "query_hint": None,
             "reason": "LLM unavailable"} for t in texts]
```

**Pipeline integration:**

```python
# pipeline.py — _process_batch() refactored:

async def _process_batch(self, batch: list[PipelineItem]):
    # Phase 1: Normalize + dedup + store
    valid_items = [...]
    if not valid_items:
        return

    # Phase 2: COMBINED classify + route — ОДИН LLM-вызов на батч
    texts = [item.normalized.full_text for item in valid_items]
    chat_contexts = [self._get_chat_context(item.chat_thread_id) for item in valid_items]

    combined = await asyncio.to_thread(
        classify_and_route_batch, texts, chat_contexts
    )

    # Phase 3: Batch embed (только для important)
    embeddings = await self._batch_embed(valid_items, combined)

    # Phase 4: Process each item
    for i, item in enumerate(valid_items):
        decision = combined[i]
        label = decision["label"]
        action = decision["action"]

        # Store memory + CHAOS (если не noise)
        if label not in ("noise", "chit-chat"):
            await self._store_item(item, label, embeddings.get(i))

        # Response handling
        if action == "respond_fact":
            await self._handle_fact_response(item, decision)
        elif action == "escalate":
            await self._handle_escalation(item, decision)
        # skip → ничего

        await self._mark_analyzed(item.message_id, label)
```

**Cost impact:**
```
ДО (separate):
  classify_batch (10 msgs) → $0.0003
  10 × decide_response     → $0.010
  ИТОГО: $0.0103 на батч

ПОСЛЕ (combined):
  combined_batch (10 msgs) → $0.001
  ИТОГО: $0.001 на батч

Экономия: 90% на classify+route.
```

---

#### 9.8.5 🟡 ANTI-LOOP + ANTI-SPAM — Защита от цепочек ответов

**Проблема:** бот отвечает клиенту → клиент отвечает → бот снова → цепочка без контроля.

**Решение:** Rate limiting per chat + max consecutive responses + circuit breaker.

```python
# response_engine.py — anti-spam state:

class ResponseGuard:
    """
    Защита от спам-ответов и петель.
    State per chat_thread: хранит последний ответ и счётчик.
    """

    def __init__(self):
        self._last_response: dict[int, float] = {}      # chat_id → timestamp
        self._consecutive: dict[int, int] = {}           # chat_id → count
        self._cooldown_until: dict[int, float] = {}      # chat_id → timestamp

    def can_respond(self, chat_thread_id: int) -> tuple[bool, str]:
        """Можно ли отвечать в этот чат?"""
        now = time.time()

        # 1. Circuit breaker: cooldown после N ответов подряд
        if chat_thread_id in self._cooldown_until:
            if now < self._cooldown_until[chat_thread_id]:
                return False, "cooldown"
            else:
                del self._cooldown_until[chat_thread_id]
                self._consecutive[chat_thread_id] = 0

        # 2. Min interval: не чаще раза в 3 минуты
        last = self._last_response.get(chat_thread_id, 0)
        if now - last < 180:  # 3 минуты
            return False, "too_soon"

        # 3. Max consecutive: после 3 ответов подряд → cooldown 15 минут
        consecutive = self._consecutive.get(chat_thread_id, 0)
        if consecutive >= 3:
            self._cooldown_until[chat_thread_id] = now + 900  # 15 мин
            return False, "max_consecutive"

        return True, "ok"

    def record_response(self, chat_thread_id: int):
        """Записать что бот ответил."""
        now = time.time()
        self._last_response[chat_thread_id] = now
        self._consecutive[chat_thread_id] = self._consecutive.get(chat_thread_id, 0) + 1

    def reset_consecutive(self, chat_thread_id: int):
        """Сбросить счётчик (клиент написал что-то новое после паузы)."""
        last = self._last_response.get(chat_thread_id, 0)
        if time.time() - last > 600:  # >10 минут паузы
            self._consecutive[chat_thread_id] = 0


# Pipeline integration:

response_guard = ResponseGuard()

# В _handle_fact_response():
allowed, reason = response_guard.can_respond(item.chat_thread_id)
if not allowed:
    logger.info(f"Response guard: {reason} for chat {item.chat_thread_id}")
    if reason == "max_consecutive":
        # После 3 ответов → всё остальное эскалируем
        await self._handle_escalation(item, decision)
    return

# Отправляем ответ
await self.bot.send_message(...)
response_guard.record_response(item.chat_thread_id)
```

---

#### 9.8.6 COST ANALYSIS (updated)

```
Обновлённые LLM-вызовы на сообщение:

Combined classify+route (batch 10):
  → 0.1 LLM-вызова на сообщение × $0.001 = $0.0001

Fact response generation (40% сообщений):
  → 0.4 LLM-вызова × $0.001 = $0.0004

Progress check-in reply (редко, ~5/день):
  → 5 вызовов × $0.001 / 500msg = $0.00001

Follow-up detection (только business-типы, ~30%):
  → 0.3 LLM-вызова × $0.0005 = $0.00015

ИТОГО на сообщение: ~$0.00065
При 500 msg/day: $0.33/день = ~$10/мес

Было: $33/мес → Стало: $10/мес (оптимизация combined + fast path)
```

---

## 10. ОГРАНИЧЕНИЯ

```
Что НЕ умеет:
├── Отвечать на сложные многоходовые вопросы
├── Генерировать КП/договоры
├── Вести переговоры
├── Понимать голосовые/фото (отдельная фаза)
└── Работать на нескольких языках одновременно

Что можно улучшить позже:
├── Fine-tune classify на реальных данных
├── Ответ owner'а через бота ("ответь ему что завтра")
├── Voice transcription → ingest
├── Multi-language detection
└── A/B testing response quality
```

---

## 11. РЕШЕНИЯ

| # | Решение | Причина |
|---|---------|---------|
| 1 | LLM для понимания вопроса клиента | Regex не покрывает реальную речь |
| 2 | SQL для фактов | Бесплатно, точно, быстро |
| 3 | LLM для формулировки ответа | Шаблоны звучат как робот |
| 4 | LLM решает: отвечать или escalate | Нужно суждение, regex не справится |
| 5 | Fast path для "ок/согласен" | Бесплатно, экономит LLM-вызовы |
| 6 | 30 мин таймер перед escalation | Owner может ответить сам |
| 7 | Night mode (23-08) | Не будить owner'а |
| 8 | Combined classify + routing | Один вызов вместо двух, -40% cost |
| 9 | Pending notifications table | Персистентность при рестарте |
| 10 | Бот пишет клиенту через business API, owner'у в личку | Чистое разделение |
| 11 | Proactive: owner'у за 2-3 дня до даты | Не только дайджест, а персональные напоминания |
| 12 | Proactive: клиенту за 2-3 дня до дедлайна | Через business API от имени owner'а |
| 13 | Progress check-in: owner → подтверждение прогресса | Не ждём пока спросят, сами спрашиваем |
| 14 | LLM для progress reply (НЕ regex) | Regex не поймёт "в процессе, жду бухгалтера, будет в четверг" |
| 15 | Follow-up detection через LLM | "Договорились в среду" → task + scheduled reminder |
| 16 | Timer persistence + startup recovery | Pending notifications table → recover при рестарте |
| 17 | Expanded fact queries (10+ интентов) | payment_status, overdue, tasks_today, leads, deal_details, etc. |
| 18 | Combined classify+route = primary | Один LLM-вызов вместо двух, -90% cost |
| 19 | ResponseGuard: anti-loop + anti-spam | max 3 consecutive + 3min interval + 15min cooldown |
