---
name: lenochka-pipeline
description: "Полный пайплайн обработки сообщений Lenochka: нормализация (извлечение текста из любого типа), дедупликация, обработка edited/deleted, маппинг Telegram → CRM. Инструкция пошагового flow."
---

# Lenochka Pipeline — Полный пайплайн обработки

Инструкция: как обрабатывать КАЖДОЕ входящее сообщение от начала до конца.

---

## 🔁 ПОЛНЫЙ FLOW

```
Входящее сообщение (из Telegram)
    │
    ▼
┌──────────────────┐
│ 1. NORMALIZE     │ Извлечь текст из любого типа сообщения
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 2. DEDUP         │ content_hash + source_message_id
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 3. RESOLVE       │ Telegram user → CRM contact (upsert)
│    CONTACT       │ chat → chat_thread (upsert)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 4. STORE MESSAGE │ INSERT в messages (analyzed=false)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 5. INGEST        │ classify → extract → store memory + chaos
│    (lenochka-    │
│     memory)      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 6. CRM UPSERT    │ entities → contacts, deals, tasks, leads
│    (lenochka-    │
│     crm)         │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 7. RESPONSE      │ skip / respond_fact / escalate
│    (lenochka-    │
│     response)    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 8. MARK ANALYZED │ UPDATE messages SET analyzed=true
└──────────────────┘
```

---

## 📝 ШАГ 1: NORMALIZE (Извлечение текста)

### Текстовые сообщения
```
message.text       → text как есть
message.caption    → caption (photo/video/document + подпись)
```

### Медиа
```
message.sticker    → EMOJI_INTENT маппинг (см. ниже)
message.voice      → "[voice: {duration}s — transcription pending]"
message.photo      → "[photo] {caption или пусто}"
message.video      → "[video: {duration}s] {caption}"
message.video_note → "[video note: {duration}s]"
message.document   → "[document: {filename}] {caption}"
message.dice       → "[dice: {emoji}={value}]"
```

### Контакт и геолокация
```
message.contact    → "[contact: {first_name} {last_name} phone:{phone}]"
message.location   → "[location: {lat},{lon}]"
```

### Reply и Forward контекст
```
Если reply_to_message:
  orig_text = extract(reply_to_message).text[:150]
  orig_author = reply_to_message.from_user.first_name
  prepended = "[Reply to {orig_author}: \"{orig_text}\"] {current_text}"

Если forward_origin:
  origin = forward_origin
  if origin = MessageOriginUser → "[Forwarded from: {name}] {text}"
  if origin = MessageOriginChat → "[Forwarded from chat: {title}] {text}"
  if origin = MessageOriginHiddenUser → "[Forwarded from: hidden] {text}"
```

### Emoji Intent Mapping
```python
EMOJI_INTENT = {
    # Подтверждение
    '👍': 'confirm',    '👌': 'confirm',    '🤝': 'confirm',    '👊': 'confirm',
    # Выполнение
    '✅': 'done',       '☑️': 'done',       '🎉': 'done',
    # Отказ
    '❌': 'cancel',     '🚫': 'cancel',     '👎': 'cancel',
    # Срочность
    '🔥': 'urgent',     '⚡': 'urgent',     '⏰': 'reminder',
    # Деньги
    '💰': 'payment',    '💵': 'payment',    '💸': 'payment',
    # Одобрение
    '❤️': 'approve',    '💯': 'approve',    '🙌': 'approve',
    # Планирование
    '📅': 'schedule',   '🗓️': 'schedule',
    # НЕ подтверждение
    '😂': 'laugh',      '🤣': 'laugh',      '😅': 'nervous',
    '🤔': 'thinking',   '😢': 'sad',        '🙏': 'please',
}
```

### ВАЖНО: Reply контекст критичен для классификации
```
"да" на "150к?"       → decision (подтверждение суммы)
"да" на "как дела?"   → chit-chat
"готово" на "Сделай КП" → task-done
```
Без reply-контекста классификатор не сможет правильно определить intent.

---

## 🔒 ШАГ 2: DEDUP (Дедупликация)

### Два механизма

**1. Content hash (SHA-256)**
```python
content_hash = hashlib.sha256(text.strip().encode('utf-8')).hexdigest()[:16]
```
Проверка:
```sql
SELECT id FROM messages WHERE content_hash = ?
```
Если найден → SKIP (дубликат).

**2. Source message ID (Telegram-specific)**
```sql
SELECT id FROM messages WHERE chat_thread_id = ? AND source_msg_id = ?
```
Уникальный INDEX: `UNIQUE(chat_thread_id, source_msg_id)`
Если найден → SKIP.

### Когда дубликаты возникают
- Telegram retry при сбое доставки
- Перезапуск polling → повторная обработка
- Ручной вызов ingest дважды

---

## 👤 ШАГ 3: RESOLVE CONTACT

### Telegram user → CRM contact

```python
# Поиск существующего контакта
existing = SELECT id FROM contacts WHERE tg_user_id = ?
if not existing:
    existing = SELECT id FROM contacts WHERE tg_username = ?

if existing:
    # Обновить name если передан и отличается
    contact_id = existing
else:
    # Создать новый
    INSERT INTO contacts (name, tg_username, tg_user_id) VALUES (?, ?, ?)
    contact_id = last_insert_rowid()
```

### Chat thread resolution

```python
existing_thread = SELECT id FROM chat_threads WHERE tg_chat_id = ?
if not existing_thread:
    INSERT INTO chat_threads (tg_chat_id, contact_id, type, title) VALUES (?, ?, ?, ?)
    chat_thread_id = last_insert_rowid()
else:
    chat_thread_id = existing_thread
```

---

## 💾 ШАГ 4: STORE MESSAGE

```sql
INSERT INTO messages (chat_thread_id, from_user_id, text, sent_at,
                      content_type, source_msg_id, content_hash, analyzed)
VALUES (?, ?, ?, ?, ?, ?, ?, 0)
```

**analyzed = false** — пока не пройдёт ingest pipeline.
**source_msg_id** — Telegram message_id для дедупликации и supersede.
**content_hash** — SHA-256 для общего дедупа.

---

## 🧠 ШАГ 5: INGEST (lenochka-memory)

```bash
python3 run_memory.py ingest \
  --text "{normalized_text}" \
  --contact_id {contact_id} \
  --chat_thread_id {chat_thread_id} \
  --message_id {message_id}
```

Возвращает:
```json
{
  "label": "decision",
  "confidence": 0.92,
  "entities": {"amounts": [150000], ...},
  "stored": true,
  "memory_id": 142
}
```

---

## 🏢 ШАГ 6: CRM UPSERT (lenochka-crm)

Только если label ∈ {task, decision, lead-signal, risk} и entities не пустые.

### Маппинг entities → CRM commands:

```
entities.contact (name, tg_username)
  → python3 run_crm.py contact --upsert --name ... --tg_username ...

entities.amounts[] + contact_id
  → python3 run_crm.py deal --update --contact_id N --amount MAX(amounts)

entities.task (description, due_date, priority)
  → python3 run_crm.py task --create --contact_id N --description ... --due_date ...

entities.lead (source, amount, probability)
  → python3 run_crm.py lead --create --contact_id N --source telegram ...

entities.agreement (summary, amount, due_date)
  → python3 run_crm.py agreement --create --contact_id N --summary ... --due_date ...
```

### Порядок важен:
1. Сначала contact (получить contact_id)
2. Потом deal/lead (нужен contact_id)
3. Потом task/agreement (нужен contact_id)

---

## 💬 ШАГ 7: RESPONSE (lenochka-response)

См. скилл lenochka-response: fast skip → fact response → escalation.

---

## ✅ ШАГ 8: MARK ANALYZED

```sql
UPDATE messages SET analyzed = 1, classification = ? WHERE id = ?
```

---

## ✏️ ОБРАБОТКА EDITED MESSAGES

```
Telegram шлёт: edited_business_message
```

1. Получить text (новый)
2. Найти существующую запись по source_message_id + chat_thread_id
3. UPDATE messages SET text = ? WHERE chat_thread_id = ? AND source_msg_id = ?
4. Найти memory по source_message_id:
   UPDATE memories SET content = ?, content_hash = ? WHERE source_message_id = ?
5. Найти chaos_entries по memory_id:
   UPDATE chaos_entries SET content = ? WHERE memory_id = ?
6. НЕ создавать новую memory — обновлять существующую
7. Переклассифицировать (label мог измениться)
8. Обновить CRM если изменились entities (например сумма 120к → 150к)

---

## 🗑 ОБРАБОТКА DELETED MESSAGES

```
Telegram шлёт: deleted_business_messages (список message_ids)
```

1. Для каждого message_id:
   UPDATE messages SET meta_json = '{"deleted": true}' WHERE chat_thread_id = ? AND source_msg_id = ?
2. НЕ удалять физически (soft delete)
3. НЕ трогать memories и chaos_entries (они — историческая запись)
4. При recall/build_context: проверять `json_extract(meta_json, '$.deleted') IS NOT 1`

---

## 🔗 BUSINESS CONNECTION

При получении `business_connection` update:

```
if status == 'active':
  INSERT OR REPLACE INTO business_connections
    (connection_id, owner_user_id, can_reply, can_read_messages, status)
  VALUES (?, ?, ?, ?, 'active')

if status == 'revoked':
  UPDATE business_connections SET status='revoked', revoked_at=datetime('now')
  WHERE connection_id = ?
```

---

## ⚠️ EDGE CASES

| Случай | Что делать |
|--------|-----------|
| Пустое сообщение (без текста) | Проверить sticker, voice, photo. Если всё пусто → SKIP |
| Очень длинное (>4096) | content_hash от полного, store от первых 500 символов |
| Медиа-группа (album) | Обрабатывать как отдельные сообщения (media_group_id не трекать) |
| Forwarded message | Парсить forward_origin для attribution |
| Reply без текста (reply на фото) | Извлечь текст из reply_to_message |
| Owner пишет в бизнес-чат | Ingest для CRM, НЕ response (не отвечать самому себе) |
| Групповой чат | Только если mention или reply боту |
| Несколько business_connection_id | Маппинг через business_connections таблицу |
| Pipeline queue не персистентен | Рестарт = потеря in-flight. При старте проверить незавершённые |
