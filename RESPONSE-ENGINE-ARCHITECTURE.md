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
├── response_engine.py          (~300 строк)
│   ├── decide_response() — Step 1: LLM decision
│   ├── generate_fact_response() — Step 3: LLM generation
│   ├── RESPONSE_DECISION_SYSTEM prompt
│   ├── RESPONSE_GEN_SYSTEM prompt
│   ├── COMBINED_CLASSIFY_SYSTEM prompt (оптимизация)
│   ├── fast_dialog_ended()
│   └── fast_sticker_ended()
│
├── fact_queries.py             (~200 строк)
│   ├── query_fact() — роутер по intent
│   ├── query_deadline()
│   ├── query_status()
│   ├── query_amount()
│   └── query_context()
│
├── notifier.py                 (~200 строк)
│   ├── handle_escalation()
│   ├── check_and_notify_later()
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

## 9. ОГРАНИЧЕНИЯ

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

## 10. РЕШЕНИЯ

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
