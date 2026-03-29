# RESPONSE ENGINE — Полная архитектура

> Дата: 2026-03-30
> Версия: v2 (пересмотренная после саморефлексии)
> Сессия: Камиль + Леночка

---

## 0. ФИЛОСОФИЯ

### Бот — это два существа в одном

```
МОД 1: Фактолог (клиенту)           МОД 2: Напоминалка (owner'у)
─────────────────────────────       ─────────────────────────────
Отвечает ТОЛЬКО фактами из БД.      Уведомляет owner'а когда:
Ничего не выдумывает.               - Клиент ждёт ответа >30 мин
Ничего не решает.                   - Нужно решение/цена/КП
                                     - Риск/жалоба
                                     - Follow-up срок наступил
                                     - Брошенный диалог
```

### Главное правило

```
Если ответ можно дать ТОЛЬКО из данных в БД (без суждения человека) → бот отвечает.
Если ответ требует РЕШЕНИЯ owner'а → notify owner'а.
При сомнениях → escalate к owner'у.
Молчание > плохой ответ.
```

---

## 1. ТРИГГЕРНАЯ МАТРИЦА

### 1.1 HARD SKIP (молча ingest, ничего не делать)

```
Условие                          Почему
─────────────────────────────    ──────────────────────────
sender_business_bot заполнен     Собственное сообщение бота
is_from_offline                  Автоответ/запланированное
label = noise                    Спам/мусор
label = chit-chat                Не по делу
Группа >3 без @mention           Не вмешиваться
Канал (read-only)                Нечего отвечать
Owner написал в бизнес-чате      Owner не ждёт ответа от бота
```

### 1.2 DIALOG ENDED (молча ingest, клиент не ждёт)

```
Условие                          Почему
─────────────────────────────    ──────────────────────────
Текст = "ок/окей/согласен/       Финализирующая фраза,
  договорились/принято/          диалог завершён
  отлично/понятно/хорошо"
Sticker = 👍🤝✅👌               Подтверждение
Owner ответил ПОСЛЕ этого        Owner уже решил
  сообщения клиента
```

### 1.3 FACT-BASED RESPONSE (бот отвечает из БД)

```
Триггер                          Пример вопроса клиента
─────────────────────────────    ──────────────────────────
Вопрос о дедлайне                "Когда договор?"
Вопрос о статусе задачи          "Что там по КП?"
Вопрос о сумме сделки            "Сколько мы договорились?"
Вопрос о контексте               "Что мы обсуждали вчера?"
Запрос данных клиента            "Какие у меня реквизиты?"
Повторный вопрос — клиент        "Напомни что мы решили"
  забыл контекст
```

**КРИТИЧЕСКОЕ ПРАВИЛО:** Факт-based ответ = ТОЛЬКО данные из БД. Если в БД нет ответа → escalate, НЕ выдумывать.

### 1.4 ESCALATION (30 мин → notify owner'а)

```
Триггер                          Пример
─────────────────────────────    ──────────────────────────
Вопрос о цене/стоимости          "Сколько будет стоить?"
Запрос КП/коммерческого          "Пришлите КП"
Запрос договора                  "Когда договор?"
Договорённость о встрече         "Давайте встретимся в среду"
Решение требующее суждения       "Что вы предлагаете?"
Вопрос на который нет            "Как насчёт скидки?"
  данных в БД
Жалоба/риск                      "Вы обещали вчера!"
Новый лид (первое сообщение)     "Привет, хочу обсудить проект"
Запрос owner'а лично             "Передайте Камилю"
```

### 1.5 Decision Tree (полный порядок проверки)

```
message arrives
│
├─ [HARD SKIP] sender_business_bot / is_from_offline / noise / chit-chat?
│   └─ SKIP → done
│
├─ [HARD SKIP] group >3 без mention / channel?
│   └─ SKIP → done
│
├─ [DIALOG ENDED] текст = "ок/согласен/договорились" / sticker confirm / owner replied after?
│   └─ SKIP → done
│
├─ [FACT CHECK] вопрос можно ответить из БД? (дедлайн/статус/сумма/контекст)
│   └─ ДА → RESPOND (фактологический ответ, 0$ LLM, SQL only)
│
├─ [ESCALATION] вопрос требует решения owner'а? (цена/КП/встреча/суждение)
│   └─ ДА → SCHEDULE NOTIFY (30 мин → owner'у в личку)
│
├─ [ESCALATION] risk / жалоба?
│   └─ ДА → SCHEDULE NOTIFY (10 мин для рисков)
│
├─ [ESCALATION] новый лид / первое сообщение?
│   └─ ДА → SCHEDULE NOTIFY (15 мин для новых)
│
└─ DEFAULT → SKIP (молча ingest)
```

---

## 2. FACT-BASED RESPONSE ENGINE

### 2.1 Как работает (без LLM, бесплатно)

```
Клиент: "Когда договор?"
         │
         ▼
  _extract_question_intent()
  → intent = "deadline", entity = "договор"
         │
         ▼
  _query_fact(intent, entity, contact_id)
  → SQL: SELECT due_at FROM agreements 
         WHERE contact_id=? AND summary LIKE '%договор%'
  → result: "2026-04-04"
         │
         ▼
  _format_fact_response(intent, result)
  → "Договор запланирован на 04.04 📋"
         │
         ▼
  bot.send_message(chat_id, text)
```

### 2.2 Question Intent Detection (heuristic, без LLM)

```python
# question_patterns.py — паттерны для извлечения намерения из вопроса

QUESTION_INTENTS = {
    # === ДЕДЛАЙНЫ ===
    "deadline": {
        "patterns": [
            r"когда\b.*?(договор|кп|оплат|срок|дедлайн|встреч)",
            r"(договор|кп|оплат).{0,20}когда",
            r"во сколько",
            r"какого числа",
        ],
        "query_fn": "query_deadline",
    },
    
    # === СТАТУС ===
    "status": {
        "patterns": [
            r"что там\b.*?(кп|договор|проект|задач|дел)",
            r"как дела\b.*?(с |по )?.*",
            r"(кп|договор|задач).{0,20}(готов|статус|прогресс)",
            r"как продвигается",
            r"есть (новости|обновления|прогресс)",
        ],
        "query_fn": "query_status",
    },
    
    # === СУММА ===
    "amount": {
        "patterns": [
            r"сколько\b.*?(договорились|стоит|будет|сумм)",
            r"(сумм|цена|стоимост).{0,10}(сколько|какая)",
            r"за сколько",
        ],
        "query_fn": "query_amount",
    },
    
    # === КОНТЕКСТ ===
    "context_recall": {
        "patterns": [
            r"напомн[иа]\b",
            r"что (мы |)говорили",
            r"о чём (мы |)говорили",
            r"что (было|обсуждали|решили)",
            r"напомни что",
        ],
        "query_fn": "query_context",
    },
    
    # === ЗАДАЧИ ===
    "task_status": {
        "patterns": [
            r"(задач|todo).{0,10}(готов|выполн|статус)",
            r"сделал[иа]?\b.*\?",
        ],
        "query_fn": "query_task_status",
    },
}


def extract_question_intent(text: str) -> dict | None:
    """
    Определяет: можно ли ответить на вопрос из БД.
    Возвращает {intent, entity, query_fn} или None.
    
    None = вопрос требует суждения → escalate.
    """
    text_lower = text.lower()
    
    for intent_name, intent_cfg in QUESTION_INTENTS.items():
        for pattern in intent_cfg["patterns"]:
            if re.search(pattern, text_lower):
                # Извлекаем entity (что спрашивают)
                entity = _extract_entity(text_lower, intent_name)
                return {
                    "intent": intent_name,
                    "entity": entity,
                    "query_fn": intent_cfg["query_fn"],
                }
    
    return None  # Нет совпадений → escalate
```

### 2.3 Fact Queries (SQL, бесплатно)

```python
def query_deadline(contact_id, entity, db_path) -> str | None:
    """Клиент спрашивает 'когда договор?' → ищем дедлайн в БД."""
    conn = get_db(db_path)
    result = None
    
    if "договор" in entity or not entity:
        row = conn.execute("""
            SELECT due_at, summary, status FROM agreements
            WHERE contact_id = ? AND status NOT IN ('completed', 'cancelled')
            ORDER BY created_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
        if row:
            if row["status"] == "signed":
                result = f"Договор подписан ✅"
            elif row["due_at"]:
                result = f"Договор запланирован на {row['due_at'][:10]} 📋"
            else:
                result = f"Договор: {row['summary'][:50]} (срок не указан)"
    
    elif "кп" in entity:
        row = conn.execute("""
            SELECT due_at, description FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND LOWER(description) LIKE '%кп%'
              AND status NOT IN ('done', 'cancelled')
            ORDER BY created_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
        if row:
            if row["due_at"]:
                result = f"КП запланировано на {row['due_at'][:10]} 📋"
            else:
                result = f"КП в работе: {row['description'][:50]}"
    
    elif "оплат" in entity:
        row = conn.execute("""
            SELECT i.amount, i.due_at, i.status
            FROM invoices i
            JOIN agreements a ON i.agreement_id = a.id
            WHERE a.contact_id = ?
            ORDER BY i.created_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
        if row:
            amt = f"{row['amount']:,.0f}₽"
            if row["status"] == "paid":
                result = f"Оплата {amt} получена ✅"
            elif row["due_at"]:
                result = f"Оплата {amt} до {row['due_at'][:10]} — статус: {row['status']}"
    
    conn.close()
    return result


def query_status(contact_id, entity, db_path) -> str | None:
    """Клиент спрашивает 'что там по КП?' → ищем статус."""
    conn = get_db(db_path)
    result = None
    
    # Ищем в tasks, deals, agreements по entity
    if entity:
        # Tasks containing entity
        row = conn.execute("""
            SELECT description, status, due_at FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND LOWER(description) LIKE ?
              AND status NOT IN ('cancelled')
            ORDER BY created_at DESC LIMIT 1
        """, (contact_id, f"%{entity}%")).fetchone()
        if row:
            status_emoji = {"open": "🔄", "in_progress": "🔨", "done": "✅"}.get(row["status"], "📋")
            due = f" (до {row['due_at'][:10]})" if row.get("due_at") else ""
            result = f"{status_emoji} {row['description'][:60]}{due}"
    
    # Fallback: active deal status
    if not result:
        row = conn.execute("""
            SELECT amount, stage FROM deals
            WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
            ORDER BY updated_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
        if row:
            amt = f"{row['amount']:,.0f}₽" if row.get("amount") else ""
            result = f"Сделка {amt}: стадия {row['stage']}"
    
    conn.close()
    return result


def query_amount(contact_id, entity, db_path) -> str | None:
    """Клиент спрашивает 'сколько мы договорились?' → сумма из БД."""
    conn = get_db(db_path)
    
    row = conn.execute("""
        SELECT amount, stage FROM deals
        WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY updated_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    
    conn.close()
    if row and row["amount"]:
        return f"Договорились на {row['amount']:,.0f}₽ 📋"
    return None


def query_context(contact_id, entity, db_path) -> str | None:
    """Клиент просит 'напомни что мы говорили' → последние memories."""
    conn = get_db(db_path)
    
    rows = conn.execute("""
        SELECT content, type, created_at FROM memories
        WHERE contact_id = ?
        ORDER BY importance DESC, created_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    
    conn.close()
    if rows:
        lines = []
        for r in rows:
            lines.append(f"• {r['content'][:100]}")
        return "Последнее что мы обсуждали:\n" + "\n".join(lines)
    return None


def query_task_status(contact_id, entity, db_path) -> str | None:
    """Статус задач для контакта."""
    conn = get_db(db_path)
    
    rows = conn.execute("""
        SELECT description, status, due_at, priority FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND status NOT IN ('done', 'cancelled')
        ORDER BY 
            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
            due_at ASC
        LIMIT 3
    """, (contact_id,)).fetchall()
    
    conn.close()
    if rows:
        lines = []
        for r in rows:
            icon = {"open": "📋", "in_progress": "🔨"}.get(r["status"], "📋")
            due = f" (до {r['due_at'][:10]})" if r.get("due_at") else ""
            lines.append(f"{icon} {r['description'][:50]}{due}")
        return "\n".join(lines)
    return None
```

### 2.4 Форматирование ответа (шаблоны, без LLM)

```python
def format_fact_response(intent: str, result: str, contact_name: str) -> str:
    """
    Форматирует фактологический ответ.
    Без LLM — шаблоны с подстановкой. 0$ cost.
    """
    templates = {
        "deadline": "📋 {result}",
        "status": "📊 {result}",
        "amount": "💰 {result}",
        "context_recall": "🧠 {result}",
        "task_status": "📝 {result}",
    }
    
    template = templates.get(intent, "📋 {result}")
    return template.format(result=result)


def format_no_data_response(intent: str) -> str | None:
    """
    Если данных нет в БД — НЕ выдумываем.
    Возвращаем None → это превращается в escalation.
    """
    return None  # Всегда None — не отвечаем если нет данных
```

---

## 3. ESCALATION ENGINE (уведомления owner'у)

### 3.1 Когда escalate

```python
ESCALATION_TRIGGERS = {
    # === ЦЕНООБРАЗОВАНИЕ ===
    "pricing": {
        "patterns": [
            r"сколько\b.*стоит",
            r"какая\b.*цена",
            r"стоимост",
            r"расценк",
            r"скидк",
            r"бюджет",
        ],
        "priority": 2,      # normal
        "delay": 1800,      # 30 мин
    },
    
    # === КП / КОММЕРЧЕСКОЕ ===
    "proposal": {
        "patterns": [
            r"пришлит[еь].*\bкп",
            r"коммерческ",
            r"предложени[ея]",
            r"составь.*кп",
            r"подготовь.*предложени",
        ],
        "priority": 2,
        "delay": 1800,
    },
    
    # === ДОГОВОР ===
    "contract": {
        "patterns": [
            r"пришлит[еь].*договор",
            r"подпишем",
            r"подписан",
            r"когда.*подписать",
        ],
        "priority": 2,
        "delay": 1800,
    },
    
    # === ВСТРЕЧА ===
    "meeting": {
        "patterns": [
            r"встретимся",
            r"созвон",
            r"встреч",
            r"когда.*удобно",
            r"свободн.*время",
        ],
        "priority": 1,      # low
        "delay": 1800,
    },
    
    # === ЖАЛОБА / РИСК ===
    "complaint": {
        "patterns": [
            r"обещали",
            r"где\b",
            r"задержк",
            r"просроч",
            r"не\b.*пришл",
            r"не\b.*сделал",
            r"разочарован",
            r"ужасно",
        ],
        "priority": 4,      # urgent
        "delay": 600,       # 10 мин
    },
    
    # === ЗАПРОС OWNER'А ===
    "owner_request": {
        "patterns": [
            r"передайте.*камил",
            r"дайте.*камил",
            r"связь.*с.*руководител",
            r"хочу\b.*поговорить.*с.*ваш",
        ],
        "priority": 3,      # high
        "delay": 300,       # 5 мин
    },
}
```

### 3.2 Ночные уведомления

```
23:00 - 08:00 (GMT+8):
  Риски/жалобы (priority 4) → отправить СРАЗУ (ночь = срочно)
  Остальные → schedule на 08:00

08:00 - 22:59:
  Все → schedule через delay (30 мин / 10 мин / 5 мин)
```

### 3.3 Формат уведомления owner'у

```python
NOTIFY_TEMPLATES = {
    "pricing": """💰 {contact_name} спрашивает о цене ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}

💡 Напишите клиенту напрямую в Telegram.""",

    "proposal": """📄 {contact_name} просит КП ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}""",

    "contract": """📝 {contact_name} спрашивает о договоре ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}""",

    "meeting": """📅 {contact_name} предлагает встречу ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}""",

    "complaint": """⚠️ {contact_name}: жалоба/риск ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}""",

    "owner_request": """👤 {contact_name} просит связаться с вами ({wait_time})

💬 «{message_text}»""",

    "new_lead": """🔥 Новый контакт: {contact_name}

💬 «{message_text}»

📋 {context_block}""",

    "question_no_data": """❓ {contact_name} спрашивает ({wait_time})

💬 «{message_text}»

⚠️ Не удалось найти ответ в базе данных.

📋 Контекст:
{context_block}""",

    "waiting_client": """🔔 {contact_name} ждёт вашего ответа ({wait_time})

💬 «{message_text}»

📋 Контекст:
{context_block}""",
}


def format_notification(escalation_type, contact_name, message_text,
                        wait_time, context_block) -> str:
    template = NOTIFY_TEMPLATES.get(escalation_type, NOTIFY_TEMPLATES["waiting_client"])
    return template.format(
        contact_name=contact_name,
        message_text=message_text[:150],
        wait_time=wait_time,
        context_block=context_block or "—",
    )
```

---

## 4. PROACTIVE REMINDERS

### 4.1 Что напоминаем owner'у

```
Ежедневно 08:00 (уже есть — daily digest):
├── Новые лиды за вчера
├── Просроченные задачи
├── Брошенные диалоги (>24ч)
└── Ключевые события

Каждый час (НОВОЕ):
├── Follow-up commitments (договорённости с наступившим сроком)
└── Tasks due today

Каждые 4 часа (уже есть — abandoned check):
└── Брошенные диалоги

Startup (НОВОЕ):
└── Pending notifications которые пропустили за downtime
```

### 4.2 Follow-up Detection

```python
# В extract_entities или отдельном пост-процессинге:

def detect_followup_commitment(text, label, entities) -> dict | None:
    """
    Детектит договорённости из текста сообщения.
    Возвращает {type, description, due_date} или None.
    
    Не regex — через LLM в extract_entities. 
    Добавляем в EXTRACT_SYSTEM инструкцию:
    """
    pass
    # Логика встроена в brain.extract_entities():
    # LLM уже извлекает agreement с due_date.
    # Мы просто проверяем: есть agreement + due_date → follow-up detected.
    pass


# В scheduler — каждый час:
async def _check_followups(bot, brain):
    """
    Проверяет: есть ли договорённости/задачи с сегодняшним дедлайном,
    по которым owner ещё не ответил.
    """
    conn = get_db(settings.db_path)
    today = datetime.now(GMT8).strftime("%Y-%m-%d")
    
    # Tasks due today or overdue
    due_tasks = conn.execute("""
        SELECT t.*, c.name as contact_name, ct.tg_chat_id
        FROM tasks t
        LEFT JOIN contacts c ON t.related_type='contact' AND t.related_id=c.id
        LEFT JOIN chat_threads ct ON ct.contact_id = c.id
        WHERE t.due_at <= datetime(?, '+1 day')
          AND t.status NOT IN ('done', 'cancelled')
        ORDER BY t.priority DESC, t.due_at ASC
        LIMIT 5
    """, (today,)).fetchall()
    
    # Agreements due soon
    due_agreements = conn.execute("""
        SELECT a.*, c.name as contact_name
        FROM agreements a
        JOIN contacts c ON a.contact_id = c.id
        WHERE a.due_at <= datetime(?, '+2 days')
          AND a.status NOT IN ('completed', 'cancelled', 'signed')
        ORDER BY a.due_at ASC
        LIMIT 5
    """, (today,)).fetchall()
    
    conn.close()
    
    if not due_tasks and not due_agreements:
        return
    
    lines = []
    for t in due_tasks:
        due = f"до {t['due_at'][:10]}" if t.get("due_at") else ""
        icon = "🔴" if t["priority"] == "urgent" else "🟡"
        lines.append(f"{icon} {t['description'][:50]} — {t.get('contact_name', '?')} {due}")
    
    for a in due_agreements:
        due = f"до {a['due_at'][:10]}" if a.get("due_at") else ""
        lines.append(f"📝 {a['summary'][:50] or 'Договор'} — {a['contact_name']} {due}")
    
    text = "⏰ <b>Сроки сегодня:</b>\n\n" + "\n".join(lines)
    await bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode="HTML",
    )
```

### 4.3 Abandoned Dialogues (уже есть, уточняю)

```python
# Уже работает: scheduler → every 4h → check_abandoned
# Никаких изменений. Логика корректна.
```

---

## 5. EDGE CASES И АНТИПЕТЛИ

### 5.1 Антипетля (критично)

```
Сценарий                           Защита
──────────────────────────────     ─────────────────────────────
Бот отправил уведомление owner'у   Никогда не пишет в бизнес-чат.
  → Telegram шлёт update           Антипетли нет по определению.
                                   Бот ТОЛЬКО пишет owner'у в личку.

Owner ответил в бизнес-чате        owner_replied_after = True.
  → 30 мин таймер ещё тикает       _check_and_notify_later()
                                   проверяет → CANCEL.

Клиент пишет "ок" после вопроса    dialog_ended = True → SKIP.
  → бот всё равно notify?          detect_waiting() проверяет.
```

**Вывод: антипетли НЕТ в этой архитектуре.** Бот никогда не пишет в бизнес-чат. Пишет только owner'у в личку.

### 5.2 Owner ответил за 30 минут

```
t=0:   Клиент: "Когда договор?" → is_waiting=True → schedule 30 мин
t=5:   Owner: "В пятницу пришлю"
t=10:  Клиент: "ок"
t=30:  _check_and_notify_later()
       → dialog_state["owner_replied_after"] = True
       → CANCEL notification
```

### 5.3 Клиент написал 5 вопросов подряд

```
t=0:   "Когда договор?" → schedule 30 мин (msg_id=101)
t=1:   "Сколько стоит?" → schedule 30 мин (msg_id=102)
t=2:   "Можете встретиться?" → schedule 30 мин (msg_id=103)

t=30:  msg_id=101 → owner ещё не ответил → NOTIFY (все 3 в одном сообщении)
       → anti-spam: уже уведомили по этому chat_id → aggregate

Решение: aggregate все pending notifications для одного chat_id
в одно уведомление owner'у. Не 3 отдельных.
```

### 5.4 Ночное сообщение

```
t=23:30: Клиент: "Когда договор?" → is_waiting=True

Решение: НЕ schedule на 30 мин. 
Schedule на 08:00 утра (или 30 мин, что позже).

Приоритет 4 (risk/complaint) → отправить СРАЗУ даже ночью.
```

### 5.5 Факт-based ответ и данных нет

```
Клиент: "Когда договор?"
Бот: _extract_question_intent() → intent="deadline", entity="договор"
Бот: query_deadline(contact_id, "договор", db_path) → None (нет agreements)

Решение: НЕ отвечаем "не знаю" → escalate как question_no_data → notify owner'у.
```

### 5.6 Вопрос owner'а в бизнес-чате

```
Owner пишет в бизнес-чат клиенту: "Пришлю КП завтра"
→ sender_business_bot заполнен → HARD SKIP
→ owner пишет в бизнес-чат = он общается с клиентом
→ бот не вмешивается
```

### 5.7 Клиент пишет owner'у напрямую (личка)

```
Клиент пишет owner'у в личку в Telegram (не через бота).
Бот видит через Business API.

Это НЕ message в личку бота. Это business_message.
Обрабатывается как обычно: ingest + notification decision.
```

### 5.8 Факт-based ответ на неправильный intent

```
Клиент: "Когда вы закончите?" (не договор, а проект в целом)
Бот: _extract_question_intent() → None (не совпал ни один паттерн)
Решение: ESCALATE → notify owner'у

Лучше escalate ложноположительно, чем ответить неправильно.
```

---

## 6. ФАЙЛЫ И ИХ СОДЕРЖАМОЕ

### 6.1 Новые файлы

```
lenochka-bot/services/
│
├── waiting_detector.py         (~150 строк)
│   ├── detect_waiting() → WaitingDecision
│   ├── CLIENT_WAITING_SIGNALS
│   └── DIALOG_ENDED_SIGNALS
│
├── dialog_state.py             (~100 строк)
│   ├── get_dialog_state() → {owner_replied_after, ...}
│   ├── _is_owner_message()
│   └── _is_new_contact()
│
├── fact_responder.py           (~200 строк)
│   ├── extract_question_intent() → {intent, entity, query_fn}
│   ├── QUESTION_INTENTS (паттерны)
│   ├── query_deadline() / query_status() / query_amount()
│   ├── query_context() / query_task_status()
│   ├── format_fact_response()
│   └── format_no_data_response() → None (всегда escalate)
│
├── notifier.py                 (~200 строк)
│   ├── send_waiting_notification()
│   ├── ESCALATION_TRIGGERS
│   ├── NOTIFY_TEMPLATES
│   ├── format_notification()
│   ├── _already_notified() / _mark_notified()
│   ├── _schedule_morning_notification()
│   └── aggregate_pending_notifications()
│
└── response_context.py         (~100 строк)
    ├── build_notification_context()
    ├── build_fact_context()
    └── _format_context_block()
```

### 6.2 Изменения в существующих файлах

```
lenochka-bot/services/
├── pipeline.py
│   ├── _process_batch() — Phase 4: notification decision
│   ├── _handle_fact_question() — попытка ответить из БД
│   ├── _schedule_notification() — отложенное уведомление
│   └── _check_and_notify_later() — recheck через delay
│
├── scheduler.py
│   └── + _check_followups() — каждый час
│
lenochka-memory/
├── schemas/init.sql
│   └── + pending_notifications table
│
lenochka-memory/brain.py
│   └── extract_entities — detect agreements/due_dates (followup)
```

---

## 7. ПОШАГОВАЯ РЕАЛИЗАЦИЯ

### Phase 1: Fact-Based Response (~3 часа)

```
1.1 Создать fact_responder.py
    - extract_question_intent() с паттернами
    - query_deadline / query_status / query_amount / query_context
    - format_fact_response()

1.2 Интегрировать в pipeline._process_batch()
    - После classify: проверить вопрос → попробовать ответить из БД
    - Если данные есть → ответить (бесплатно, SQL only)
    - Если данных нет → перейти к escalation

1.3 Тест: "Когда договор?" → ответ из БД
    Тест: "Когда вы закончите?" → None → escalation
```

### Phase 2: Escalation + Notifications (~3 часа)

```
2.1 Создать waiting_detector.py
    - detect_waiting() → WaitingDecision
    - CLIENT_WAITING_SIGNALS
    - DIALOG_ENDED_SIGNALS

2.2 Создать dialog_state.py
    - get_dialog_state()
    - owner_replied_after check

2.3 Создать notifier.py
    - send_waiting_notification()
    - ESCALATION_TRIGGERS
    - NOTIFY_TEMPLATES
    - Night mode (23-08 → schedule to 08:00)

2.4 Создать response_context.py
    - build_notification_context()

2.5 Добавить pending_notifications в init.sql

2.6 Интегрировать в pipeline
    - Phase 4: detect_waiting → schedule → async check later → notify

2.7 Тест: Клиент спрашивает цену → 30 мин → owner получает уведомление
    Тест: Owner ответил за 10 мин → уведомление отменяется
    Тест: Клиент написал "ок" → уведомление отменяется
```

### Phase 3: Proactive Reminders (~2 часа)

```
3.1 Добавить _check_followups() в scheduler (каждый час)
    - Tasks due today
    - Agreements due soon

3.2 Startup recovery
    - Проверить pending_notifications при запуске
    - Обработать пропущенные за downtime

3.3 Aggregate notifications
    - Несколько pending для одного чата → одно уведомление

3.4 Тест: Задача с due_at = сегодня → 09:00 owner получает напоминание
```

### Phase 4: Polish + Edge Cases (~2 часа)

```
4.1 Anti-spam: не чаще раза в 60 мин на чат (уже в notifier)
4.2 Night mode для всех типов (уже в notifier)
4.3 Aggregate: несколько вопросов клиента → одно уведомление
4.4 Startup: pending_notifications обработка
4.5 Логирование: notification sent/cancelled/aggregated
4.6 Метрики: count notifications by type, by day
```

**Итого: ~10 часов на всю реализацию.**

---

## 8. COST ANALYSIS

```
Fact-based response:
├── SQL queries (3-5 запросов) → 0$
├── Pattern matching (regex) → 0$
├── Шаблон форматирования → 0$
└── ИТОГО: 0$

Escalation notification:
├── SQL queries (context assembly) → 0$
├── Шаблон форматирования → 0$
├── Telegram API send_message → 0$ (бесплатный API)
└── ИТОГО: 0$

Proactive reminders:
├── SQL queries → 0$
├── Шаблон форматирования → 0$
└── ИТОГО: 0$

ВСЕГО response engine: 0$ LLM cost.
Вся "умная" часть работает на SQL + regex + шаблонах.
LLM используется ТОЛЬКО для classify + extract (уже есть).
```

---

## 9. ОГРАНИЧЕНИЯ (честно)

```
Что НЕ умеет fact-based response:
├── Отвечать на сложные вопросы ("Что вы думаете о...")
├── Извлекать информацию из длинных сообщений
├── Понимать сарказм/иронию
├── Работать с мультимодальным контентом (фото/голос)
└── Генерировать естественные ответы (шаблоны звучат формально)

Что НЕ умеет escalation:
├── Определять приоритет точно (эвристика, не LLM)
├── Понимать что "срочно" означает для owner'а
├── Учитывать контекст owner'а (он может быть занят)
└── Автоматически отвечать owner'у что клиент ждёт

Что можно улучшить позже:
├── LLM для сложных fact-based вопросов (дороже, но точнее)
├── Умный приоритет escalation (LLM sentiment + urgency)
├── Ответ owner'а через бота ("ответь ему что завтра")
└── Multi-language support
```

---

## 10. РЕШЕНИЯ ПРИНЯТЫЕ В ЭТОЙ СЕССИИ

| # | Решение | Причина |
|---|---------|---------|
| 1 | Бот не отвечает клиентам сложные вещи | IdeaOfProduct: бот не договаривается, не определяет цены |
| 2 | Бот отвечает ТОЛЬКО фактами из БД | Безопасно: 0$ LLM, не выдумывает |
| 3 | Escalation через уведомление owner'у | Владелец принимает решения, не AI |
| 4 | 30 мин таймер перед уведомлением | Не спамить owner'а на каждый вопрос |
| 5 | "Ок/согласен/договорились" = молчание | Диалог завершён, не нужно вмешиваться |
| 6 | Owner ответил → отмена уведомления | Не дублировать если owner уже решил |
| 7 | Ночные уведомления только для рисков | Не будить owner'а по каждому вопросу |
| 8 | Aggregate: несколько pending → одно уведомление | Не спамить owner'а |
| 9 | Pending notifications table (персистентность) | Не терять при рестарте |
| 10 | 0$ LLM cost на весь response engine | SQL + regex + шаблоны |
