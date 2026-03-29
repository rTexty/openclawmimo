# Lenochka Telegram Bot — Production Architecture

> Дата: 2026-03-29
> Stack: Python 3.12+ / Aiogram 3.26+ / SQLite+WAL / sentence-transformers
> Telegram Bot API: v9.5 (март 2026)

---

## 1. АРХИТЕКТУРНАЯ СТРАТЕГИЯ: ДВА РЕЖИМА В ОДНОМ БОТЕ

Lenochka работает в **двух принципиально разных режимах** одновременно:

| Режим | Что происходит | Канал обновлений |
|-------|---------------|-----------------|
| **Direct Bot Chat** | Пользователь пишет НАПРЯМУЮ боту в личку | `message` update |
| **Business Account** | Бот подключён к Telegram Business аккаунту Камиля, видит ВСЕ его переписки | `business_message`, `business_connection` updates |

### Почему это критически важно

**Direct Bot Chat** — для команд: `/status`, `/leads`, `/tasks`, настройка, дайджесты.
**Business Account** — для НЕВИДИМОЙ CRM: бот видит переписку Камиля с клиентами, классифицирует, извлекает сущности, пишет в память.

```
┌─────────────────────────────────────────────────────────┐
│                    Telegram Cloud                        │
├──────────────────────┬──────────────────────────────────┤
│  Business Account    │     Direct Bot Chat               │
│  (аккаунт Камиля)    │     (бот @lenochka_bot)           │
│                      │                                   │
│  Камиль ↔ Клиент₁   │     Камиль ↔ Бот                  │
│  Камиль ↔ Клиент₂   │     /status, /leads, /digest      │
│  Камиль ↔ Клиент₃   │     настройки, обратная связь      │
│                      │                                   │
│  business_message    │     message                       │
│  business_connection │     callback_query                │
└──────────┬───────────┴──────────────┬───────────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────────────────────────────────────────┐
│                  Lenochka Bot Server                     │
│                  (Aiogram 3.26, async)                   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────────┐  ┌──────────────────────────────┐ │
│  │  Update Router    │  │  Middleware Pipeline          │ │
│  │                   │  │                               │ │
│  │  business_message │  │  Throttling → Auth → Context  │ │
│  │    → normalize    │  │  → Logging → Error Handling   │ │
│  │    → ingest       │  │                               │ │
│  │    → memory       │  └──────────────────────────────┘ │
│  │                   │                                   │
│  │  message (direct) │  ┌──────────────────────────────┐ │
│  │    → command?      │  │  Pipeline Processor           │ │
│  │    → reply?        │  │  (async queue, batched)       │ │
│  │    → ingest        │  │                               │ │
│  │                   │  │  classify → extract → store    │ │
│  │  edited_message   │  │  → crm_upsert → chaos         │ │
│  │    → supersede     │  └──────────────────────────────┘ │
│  │                   │                                   │
│  │  deleted_biz_msg  │  ┌──────────────────────────────┐ │
│  │    → soft_delete   │  │  Brain Integration            │ │
│  │                   │  │  (brain.py loaded once)        │ │
│  │  business_conn    │  │  embed_model in memory         │ │
│  │    → register      │  │  LLM calls with retry          │ │
│  └──────────────────┘  └──────────────────────────────┘ │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  SQLite WAL (lenochka.db) + mem.py/brain.py            │
└─────────────────────────────────────────────────────────┘
```

---

## 2. UPDATE TYPES — ПОЛНАЯ МАТРИЦА

### 2.1 Business API Updates (главный источник данных)

| Update Type | Aiogram Type | Что значит | Обработка |
|-------------|-------------|-----------|-----------|
| `business_connection` | `BusinessConnection` | Бот подключён/отключён от Business-аккаунта | Регистрация owner, enable/disable ingestion |
| `business_message` | `Message` | Новое сообщение в любом чате Business-аккаунта | **Основной пайплайн ingest** |
| `edited_business_message` | `Message` | Отредактированное сообщение | Supersede: найти memory по source_message_id, обновить |
| `deleted_business_messages` | `BusinessMessagesDeleted` | Удалённые сообщения | Soft-delete: пометить как удалённые |

#### Подводные камни Business API

1. **`business_connection_id`** — уникальный ID подключения. Один бот может быть подключён к нескольким Business-аккаунтам. Нужен маппинг `business_connection_id → owner_user_id`.

2. **`can_reply`** — бот может отвечать от имени Business-аккаунта только если ему выдали права. Если `can_reply=False` — бот только читает.

3. **`sender_business_bot`** — если бот отправил сообщение от имени бизнес-аккаунта, это поле содержит бота. Нужно ИГНОРИРОВАТЬ собственные сообщения бота (петля).

4. **Нет `forward_origin` в business_message** — пересланные сообщения приходят как обычные, без указания исходного автора. Нужно парсить из контекста.

5. **Права бота (`BusinessBotRights`)**:
   - `can_reply` — отвечать от имени аккаунта
   - `can_read_messages` — читать сообщения (главное для CRM)
   - `can_delete_messages` — удалять сообщения
   - Если `can_read_messages=False` — бот бесполезен как CRM

6. **Статус подключения**: `BusinessConnection.status` может быть `active` или `revoked`. При `revoked` — перестать обрабатывать.

7. **Один бизнес-аккаунт = один владелец**. Но бот может быть подключён к нескольким бизнес-аккаунтам (multi-user в перспективе).

### 2.2 Direct Bot Chat Updates (команды и взаимодействие)

| Update Type | Что | Обработка |
|-------------|-----|-----------|
| `message` (private) | Пользователь пишет боту напрямую | Команды, вопросы, настройка |
| `callback_query` | Inline-кнопки | Подтверждения, навигация |
| `edited_message` | Редактирование в личном чате бота | Не обрабатывается (команды не редактируют) |
| `my_chat_member` | Бота добавили/удалили из чата | Регистрация групп |

### 2.3 Групповые чаты (перспектива)

| Update Type | Что | Обработка |
|-------------|-----|-----------|
| `message` (group) | Сообщение в группе | Только если бот упомянут или это reply боту |
| `message_reaction` | Реакция на сообщение | 👍 = подтверждение, ❌ = отказ (если бот админ) |

---

## 3. MESSAGE NORMALIZE LAYER

Самый критический слой — превращение любого Message в текст для ingest.

### 3.1 Extract Text Pipeline

```python
def extract_text(msg: Message) -> NormalizedMessage:
    """
    Извлекает текст из ЛЮБОГО типа сообщения.
    Возвращает NormalizedMessage с текстом, типом контента, метаданными.
    """
    # Приоритет извлечения:
    if msg.text:
        return Normalized(text=msg.text, content_type="text")
    
    if msg.caption:
        # photo, video, document, animation с подписью
        return Normalized(text=msg.caption, content_type=f"{detect_media(msg)}+caption")
    
    if msg.sticker:
        # Стикер → маппинг эмодзи на намерение
        emoji = msg.sticker.emoji
        intent = EMOJI_INTENT.get(emoji, "unknown")
        return Normalized(
            text=f"[sticker: {emoji} → {intent}]",
            content_type="sticker",
            metadata={"emoji": emoji, "intent": intent}
        )
    
    if msg.contact:
        c = msg.contact
        text = f"[contact: {c.first_name} {c.last_name or ''} phone:{c.phone_number}]"
        return Normalized(text=text, content_type="contact",
                         metadata={"first_name": c.first_name, "phone": c.phone_number})
    
    if msg.location:
        loc = msg.location
        text = f"[location: {loc.latitude},{loc.longitude}]"
        return Normalized(text=text, content_type="location")
    
    if msg.voice:
        # Заглушка: транскрипция будет в Phase 4
        duration = msg.voice.duration
        return Normalized(
            text=f"[voice message: {duration}s — transcription pending]",
            content_type="voice",
            metadata={"duration": duration, "file_id": msg.voice.file_id}
        )
    
    if msg.document:
        fn = msg.document.file_name or "unnamed"
        return Normalized(
            text=f"[document: {fn}] {msg.caption or ''}",
            content_type="document",
            metadata={"file_name": fn, "mime": msg.document.mime_type}
        )
    
    if msg.photo:
        # Берём photo с максимальным разрешением
        caption = msg.caption or ""
        return Normalized(
            text=f"[photo] {caption}",
            content_type="photo",
            metadata={"caption": caption}
        )
    
    if msg.video:
        caption = msg.caption or ""
        return Normalized(
            text=f"[video: {msg.video.duration}s] {caption}",
            content_type="video"
        )
    
    if msg.video_note:
        return Normalized(
            text=f"[video note: {msg.video_note.duration}s]",
            content_type="video_note"
        )
    
    if msg.dice:
        return Normalized(
            text=f"[dice: {msg.dice.emoji}={msg.dice.value}]",
            content_type="dice"
        )
    
    # Неизвестный тип
    return Normalized(text="[unsupported message type]", content_type="unknown")
```

### 3.2 Emoji Intent Mapping

```python
EMOJI_INTENT = {
    # Подтверждение / согласие
    '👍': 'confirm',      # "да, согласен"
    '👌': 'confirm',      # "ок, принято"
    '🤝': 'confirm',      # "договорились"
    '👊': 'confirm',      # "сделка"
    
    # Выполнение
    '✅': 'done',         # "выполнено"
    '☑️': 'done',         # "готово"
    '🎉': 'done',         # "ура, закрыто"
    
    # Отказ / отмена
    '❌': 'cancel',       # "нет"
    '🚫': 'cancel',       # "отмена"
    '👎': 'cancel',       # "не согласен"
    
    # Срочность
    '🔥': 'urgent',       # "срочно!"
    '⚡': 'urgent',       # "быстро"
    '⏰': 'reminder',     # "напомни"
    
    # Деньги
    '💰': 'payment',      # "деньги пришли"
    '💵': 'payment',      # "оплата"
    '💸': 'payment',      # "отправил деньги"
    
    # Одобрение
    '❤️': 'approve',      # "нравится"
    '💯': 'approve',      # "отлично"
    '🙌': 'approve',      # "вау"
    
    # Планирование
    '📅': 'schedule',     # "встреча"
    '🗓️': 'schedule',     # "запланировать"
    
    # НЕ подтверждение
    '😂': 'laugh',        # "ха, шутишь"
    '🤣': 'laugh',        # "смешно"
    '😅': 'nervous',      # "неловко"
    '🤔': 'thinking',     # "думаю"
    '😢': 'sad',          # "грустно"
    '🙏': 'please',       # "пожалуйста"
}
```

### 3.3 Reply Context Resolution

```python
def resolve_reply_context(msg: Message) -> str | None:
    """
    Если сообщение — ответ на другое, вернуть контекст оригинала.
    Критично: "да" на "150к?" = decision. "да" на "как дела?" = chit-chat.
    """
    if not msg.reply_to_message:
        return None
    
    original = msg.reply_to_message
    orig_text = extract_text(original).text
    
    # Обрезаем до 150 символов для контекста
    if len(orig_text) > 150:
        orig_text = orig_text[:147] + "..."
    
    orig_author = original.from_user.first_name if original.from_user else "Unknown"
    return f"[Reply to {orig_author}: \"{orig_text}\"]"
```

### 3.4 Forward Context Resolution

```python
def resolve_forward_origin(msg: Message) -> str | None:
    """
    Если сообщение переслано — определить исходного автора.
    """
    if not msg.forward_origin:
        return None
    
    origin = msg.forward_origin
    if isinstance(origin, MessageOriginUser):
        return f"[Forwarded from: {origin.sender_user.first_name}]"
    elif isinstance(origin, MessageOriginChat):
        return f"[Forwarded from chat: {origin.sender_chat.title}]"
    elif isinstance(origin, MessageOriginChannel):
        return f"[Forwarded from channel: {origin.chat.title}]"
    elif isinstance(origin, MessageOriginHiddenUser):
        return f"[Forwarded from: hidden user]"
    
    return "[Forwarded]"
```

---

## 4. INGEST PIPELINE — ПОЛНЫЙ FLOW

### 4.1 Main Pipeline

```
Incoming Message (business_message или direct message)
    │
    ▼
┌──────────────────┐
│ 1. DEDUP CHECK   │ ← content_hash + source_message_id + chat_id
│    (skip if seen) │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 2. NORMALIZE     │ ← extract_text() + resolve_reply() + resolve_forward()
│    → clean_text   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 3. RESOLVE       │ ← Telegram user → CRM contact (upsert)
│    CONTACT       │ ← chat → chat_thread (upsert)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 4. STORE MESSAGE │ ← INSERT в messages table с analyzed=false
│    (raw)         │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 5. CLASSIFY      │ ← brain.classify_message(clean_text, chat_context)
│    → label, conf │   с context window (последние 5 сообщений)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 6. EXTRACT       │ ← brain.extract_entities(clean_text, label)
│    → entities    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌────────────────┐
│ 7. STORE MEMORY  │────▶│ 8. CHAOS STORE │
│    (episodic)    │     │    (FTS5 + vec) │
│    + vector      │     └────────────────┘
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 9. CRM UPSERT    │ ← contacts, deals, leads, tasks, agreements
│    (NEW!)        │ ← Самая большая дыра, которую мы закрываем
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 10. MARK ANALYZED│ ← UPDATE messages SET analyzed=true
│                  │
└──────────────────┘
```

### 4.2 CRM Upsert Logic (самое важное добавление)

```python
async def crm_upsert(entities: dict, contact_id: int | None, 
                     chat_thread_id: int | None, message_id: int):
    """
    Превращает извлечённые сущности в CRM-записи.
    Это мост между ingest и CRM-таблицами.
    """
    conn = get_db()
    
    # CONTACT
    if entities.get("contact"):
        c = entities["contact"]
        existing = conn.execute(
            "SELECT id FROM contacts WHERE tg_username = ?", 
            (c.get("tg_username"),)
        ).fetchone()
        
        if existing:
            conn.execute("""
                UPDATE contacts SET 
                    name = COALESCE(?, name),
                    company_id = (SELECT id FROM companies WHERE name = ?),
                    updated_at = datetime('now')
                WHERE id = ?
            """, (c.get("name"), c.get("company"), existing["id"]))
            contact_id = existing["id"]
        else:
            conn.execute("""
                INSERT INTO contacts (name, tg_username, notes)
                VALUES (?, ?, ?)
            """, (c.get("name") or "Unknown", c.get("tg_username"), 
                  f"Auto-created from message #{message_id}"))
            contact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    # DEAL (если есть сумма + контакт)
    if entities.get("amounts") and contact_id:
        amount = max(entities["amounts"])  # Берём максимальную сумму
        
        existing_deal = conn.execute("""
            SELECT id, amount FROM deals 
            WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
            ORDER BY created_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
        
        if existing_deal:
            # Обновляем сумму если новая больше
            if amount > (existing_deal["amount"] or 0):
                conn.execute(
                    "UPDATE deals SET amount = ?, updated_at = datetime('now') WHERE id = ?",
                    (amount, existing_deal["id"])
                )
        else:
            conn.execute("""
                INSERT INTO deals (contact_id, amount, stage, notes)
                VALUES (?, ?, 'discovery', ?)
            """, (contact_id, amount, f"Auto-detected from message #{message_id}"))
    
    # TASK
    if entities.get("task"):
        t = entities["task"]
        conn.execute("""
            INSERT INTO tasks (description, related_type, related_id, due_at, 
                              priority, source_message_id)
            VALUES (?, 'contact', ?, ?, ?, ?)
        """, (
            t.get("description", "Untitled task"),
            contact_id,
            t.get("due_date"),
            t.get("priority", "normal"),
            message_id,
        ))
    
    # LEAD
    if entities.get("lead") and contact_id:
        l = entities["lead"]
        existing_lead = conn.execute("""
            SELECT id FROM leads 
            WHERE contact_id = ? AND status NOT IN ('won', 'lost')
        """, (contact_id,)).fetchone()
        
        if not existing_lead:
            conn.execute("""
                INSERT INTO leads (contact_id, source, amount, probability, status)
                VALUES (?, ?, ?, ?, 'new')
            """, (contact_id, l.get("source", "telegram"), 
                  l.get("amount"), l.get("probability", 0.5)))
    
    # AGREEMENT
    if entities.get("agreement") and contact_id:
        a = entities["agreement"]
        conn.execute("""
            INSERT INTO agreements (contact_id, summary, amount, due_at, 
                                   source_message_id)
            VALUES (?, ?, ?, ?, ?)
        """, (contact_id, a.get("summary"), a.get("amount"), 
              a.get("due_date"), message_id))
    
    conn.commit()
    conn.close()
```

---

## 5. АРХИТЕКТУРА AIОGRAM 3.x

### 5.1 Project Structure

```
lenochka-bot/
├── __init__.py
├── __main__.py              # Entry point: asyncio.run(main())
│
├── config.py                # Settings (pydantic-settings)
├── bot.py                   # Bot instance + Dispatcher creation
│
├── middlewares/
│   ├── __init__.py
│   ├── throttling.py        # Anti-spam: не более N сообщений/сек
│   ├── auth.py              # Проверка business_connection_id
│   ├── db.py                # Inject DB connection в handler data
│   ├── context.py           # Сбор контекста (reply, forward, chat history)
│   └── logging.py           # Structured logging каждого update
│
├── handlers/
│   ├── __init__.py          # Роутер-агрегатор
│   ├── commands.py          # /start, /status, /leads, /tasks, /digest, /help
│   ├── business.py          # business_connection, business_message handlers
│   ├── direct.py            # Прямые сообщения боту (вопросы, настройка)
│   ├── edited.py            # edited_message, edited_business_message
│   ├── deleted.py           # deleted_business_messages
│   ├── callback.py          # CallbackQuery handlers (inline buttons)
│   └── errors.py            # Global error handler
│
├── filters/
│   ├── __init__.py
│   ├── business.py          # IsBusinessMessage filter
│   ├── owner.py             # IsOwner filter (проверка user_id)
│   └── content_type.py      # ContentType filter для media
│
├── services/
│   ├── __init__.py
│   ├── pipeline.py          # Главный ingest pipeline (async)
│   ├── normalizer.py        # Text extraction, emoji mapping
│   ├── contact_resolver.py  # Telegram user → CRM contact
│   ├── crm_upsert.py        # Entities → CRM tables
│   ├── brain_wrapper.py     # Обёртка над brain.py (classify, extract, embed)
│   ├── memory.py            # Вызовы mem.py функций
│   ├── digest.py            # Генерация и отправка дайджестов
│   └── scheduler.py         # APScheduler: дайджесты, consolidate
│
├── models/
│   ├── __init__.py
│   ├── normalized.py        # NormalizedMessage dataclass
│   └── enums.py             # ContentType, MessageIntent enums
│
├── utils/
│   ├── __init__.py
│   ├── emoji.py             # EMOJI_INTENT mapping
│   ├── text.py              # Text utilities (truncate, clean)
│   ├── dates.py             # Relative date parsing
│   └── logging.py           # Logger setup
│
├── lenochka-memory/         # Существующий проект (symlink или import)
│   ├── mem.py
│   ├── brain.py
│   ├── schemas/
│   └── db/
│
└── requirements.txt
    # aiogram>=3.26.0
    # pydantic-settings>=2.0
    # apscheduler>=3.10
    # sentence-transformers (optional)
    # sqlite-vec (optional)
```

### 5.2 Entry Point

```python
# __main__.py
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import settings
from bot import create_bot, create_dp
from handlers import setup_routers
from middlewares import setup_middlewares
from services.pipeline import PipelineProcessor
from services.scheduler import setup_scheduler
from services.brain_wrapper import BrainWrapper

logger = logging.getLogger("lenochka")


async def main():
    # 1. Инициализация brain (один раз!)
    brain = BrainWrapper()
    await brain.initialize()  # Загрузка модели эмбеддингов (~6.6с)
    
    # 2. Bot + Dispatcher
    bot = create_bot()
    dp = create_dp()
    
    # 3. Регистрация роутеров и middleware
    dp.include_router(setup_routers())
    setup_middlewares(dp, brain=brain)
    
    # 4. Pipeline processor (async queue)
    pipeline = PipelineProcessor(brain=brain)
    dp["pipeline"] = pipeline
    
    # 5. Scheduler (дайджесты, consolidate)
    scheduler = setup_scheduler(bot, brain)
    scheduler.start()
    
    # 6. Startup: зарегистрировать команды, webhook/polling
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск и настройка"),
        BotCommand(command="status", description="Текущий статус"),
        BotCommand(command="leads", description="Активные лиды"),
        BotCommand(command="tasks", description="Открытые задачи"),
        BotCommand(command="digest", description="Дайджест за сегодня"),
        BotCommand(command="weekly", description="Недельный отчёт"),
        BotCommand(command="find", description="Поиск по памяти"),
        BotCommand(command="help", description="Помощь"),
    ])
    
    # 7. Start polling
    logger.info("Lenochka starting...")
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message", "edited_message",
            "business_connection", "business_message", 
            "edited_business_message", "deleted_business_messages",
            "callback_query", "my_chat_member",
        ]
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
```

### 5.3 Config (pydantic-settings)

```python
# config.py
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Telegram
    bot_token: str
    owner_id: int  # Telegram user ID владельца (Камиль)
    
    # Database
    db_path: Path = Path(__file__).parent.parent / "lenochka-memory" / "db" / "lenochka.db"
    
    # LLM
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "mimo-v2-pro"
    
    # Pipeline
    pipeline_batch_size: int = 10
    pipeline_batch_interval: float = 5.0  # секунд
    
    # Throttling
    rate_limit_messages: int = 30  # сообщений в минуту на пользователя
    
    # Digest
    digest_hour: int = 8  # GMT+8
    digest_minute: int = 0
    weekly_day: int = 6  # Sunday
    
    # Webhook (опционально)
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_port: int = 8443
    
    class Config:
        env_prefix = "LEN_"
        env_file = ".env"


settings = Settings()
```

### 5.4 Middleware Pipeline

```python
# middlewares/throttling.py
from aiogram import BaseMiddleware
from aiogram.types import Message, Update
from collections import defaultdict
import time


class ThrottlingMiddleware(BaseMiddleware):
    """Anti-spam: не более N сообщений в минуту от одного пользователя."""
    
    def __init__(self, rate_limit: int = 30):
        self.rate_limit = rate_limit
        self.history: dict[int, list[float]] = defaultdict(list)
    
    async def __call__(self, handler, event: Message, data: dict):
        user_id = event.from_user.id if event.from_user else 0
        now = time.time()
        
        # Очистка старых записей (>60с)
        self.history[user_id] = [
            t for t in self.history[user_id] if now - t < 60
        ]
        
        if len(self.history[user_id]) >= self.rate_limit:
            # Rate limited — молча пропускаем
            return
        
        self.history[user_id].append(now)
        return await handler(event, data)
```

```python
# middlewares/auth.py
from aiogram import BaseMiddleware
from aiogram.types import Update
from config import settings


class AuthMiddleware(BaseMiddleware):
    """
    Проверяет:
    1. Для direct messages — только owner_id
    2. Для business messages — проверка business_connection_id
    """
    
    async def __call__(self, handler, event: Update, data: dict):
        # Business messages — всегда обрабатываем (проверка в handler)
        if event.business_message or event.business_connection:
            data["is_business"] = True
            return await handler(event, data)
        
        # Direct messages — проверяем owner
        if event.message and event.message.chat.type == "private":
            if event.message.from_user.id != settings.owner_id:
                # Не владелец — можно ответить вежливо или проигнорировать
                data["is_owner"] = False
            else:
                data["is_owner"] = True
        
        return await handler(event, data)
```

```python
# middlewares/context.py
from aiogram import BaseMiddleware
from aiogram.types import Message


class ContextMiddleware(BaseMiddleware):
    """
    Собирает контекст для сообщения:
    - Reply context (если reply)
    - Forward context (если forward)
    - Последние N сообщений из этого чата (для classify)
    """
    
    async def __call__(self, handler, event: Message, data: dict):
        from services.normalizer import resolve_reply_context, resolve_forward_origin
        
        reply_ctx = resolve_reply_context(event)
        forward_ctx = resolve_forward_origin(event)
        
        data["reply_context"] = reply_ctx
        data["forward_context"] = forward_ctx
        
        # Загрузка последних 5 сообщений из chat_thread (для classify)
        # Делается в pipeline, не здесь, чтобы не блокировать middleware
        
        return await handler(event, data)
```

### 5.5 Business Message Handler

```python
# handlers/business.py
from aiogram import Router, F
from aiogram.types import (
    Message, BusinessConnection, BusinessMessagesDeleted
)
from aiogram.filters import Filter
import logging

router = Router(name="business")
logger = logging.getLogger("lenochka.business")


class IsBusinessMessage(Filter):
    """Фильтр: сообщение из business_connection."""
    async def __call__(self, message: Message) -> bool:
        return bool(message.business_connection_id)


@router.business_connection()
async def on_business_connection(bc: BusinessConnection, **kwargs):
    """
    Бот подключён/отключён от Business-аккаунта.
    
    ВАЖНО: При подключении:
    - Сохраняем business_connection_id → user_id mapping
    - Проверяем can_read_messages и can_reply
    - Если нет can_read_messages — бот бесполезен как CRM
    """
    logger.info(
        f"Business connection: user={bc.user.id}, "
        f"status={bc.status}, can_reply={bc.rights.can_reply if bc.rights else 'N/A'}"
    )
    
    if bc.status == "active":
        # Регистрация подключения
        from services.memory import register_business_connection
        register_business_connection(
            user_id=bc.user.id,
            connection_id=bc.id,
            can_reply=bc.rights.can_reply if bc.rights else False,
            can_read=bc.rights.can_read_messages if bc.rights else True,
        )
    elif bc.status == "revoked":
        # Отключение
        from services.memory import revoke_business_connection
        revoke_business_connection(bc.id)


@router.message(IsBusinessMessage())
async def on_business_message(message: Message, pipeline, **kwargs):
    """
    Главный обработчик: бизнес-сообщение попадает в ingest pipeline.
    
    Это ВСЯ переписка Камиля с клиентами.
    Бот видит обе стороны: и сообщения Камиля, и ответы клиентов.
    
    КРИТИЧНО:
    - sender_business_bot — если бот сам отправил, пропускаем (нет петли)
    - is_from_offline — если это автоответ/запланированное, тоже пропускаем
    - business_connection_id — маппим на owner
    """
    # Антипетля: игнорируем собственные сообщения бота
    if message.sender_business_bot:
        return
    
    # Игнорируем автоответы
    if message.is_from_offline:
        return
    
    # Отправляем в pipeline (async, non-blocking)
    await pipeline.enqueue(
        message=message,
        source="business",
        business_connection_id=message.business_connection_id,
    )


@router.edited_business_message()
async def on_business_message_edited(message: Message, pipeline, **kwargs):
    """Отредактированное сообщение → supersede в памяти."""
    if message.sender_business_bot:
        return
    
    await pipeline.enqueue(
        message=message,
        source="business_edited",
        business_connection_id=message.business_connection_id,
    )


@router.deleted_business_messages()
async def on_business_messages_deleted(
    deleted: BusinessMessagesDeleted, pipeline, **kwargs
):
    """Удалённые сообщения → soft-delete в памяти."""
    await pipeline.handle_deleted(
        business_connection_id=deleted.business_connection_id,
        chat_id=deleted.chat.id,
        message_ids=deleted.message_ids,
    )
```

### 5.6 Direct Bot Command Handlers

```python
# handlers/commands.py
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode

router = Router(name="commands")


@router.message(Command("start"))
async def cmd_start(message: Message, **kwargs):
    """Приветствие и проверка Business-подключения."""
    from services.memory import get_business_status
    
    status = get_business_status(message.from_user.id)
    
    text = (
        "🤖 <b>Lenochka</b> — ваш AI-ассистент\n\n"
        "Я анализирую переписки, извлекаю задачи, следлю за лидами "
        "и помогаю не терять контекст.\n\n"
    )
    
    if status.get("connected"):
        text += "✅ Business-аккаунт подключён. CRM работает.\n"
    else:
        text += (
            "⚠️ <b>Для работы CRM нужно подключить бота к Business-аккаунту:</b>\n"
            "1. Откройте Telegram → Настройки → Telegram Business\n"
            "2. Боты → Добавить бота\n"
            "3. Включите «Чтение сообщений»\n"
        )
    
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("status"))
async def cmd_status(message: Message, **kwargs):
    """Текущий статус: активные сделки, открытые задачи, последние события."""
    from services.memory import get_status_summary
    
    summary = get_status_summary()
    text = (
        f"📊 <b>Статус</b>\n\n"
        f"📨 Сообщений сегодня: {summary['messages_today']}\n"
        f"🔥 Активных лидов: {summary['active_leads']}\n"
        f"💰 Открытых сделок: {summary['open_deals']}\n"
        f"📋 Открытых задач: {summary['open_tasks']}\n"
        f"⚠️ Просроченных: {summary['overdue_tasks']}\n"
        f"👻 Брошенных диалогов: {summary['abandoned']}\n"
        f"🧠 Memories: {summary['total_memories']}\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("leads"))
async def cmd_leads(message: Message, **kwargs):
    """Активные лиды."""
    from services.memory import get_active_leads
    leads = get_active_leads()
    
    if not leads:
        await message.answer("🔥 Нет активных лидов.")
        return
    
    lines = []
    for l in leads[:10]:
        amount = f" — {l['amount']:,.0f}₽" if l.get('amount') else ""
        lines.append(
            f"• <b>{l['contact_name']}</b>{amount} — {l['status']}"
        )
    
    await message.answer(
        f"🔥 <b>Активные лиды ({len(leads)}):</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, **kwargs):
    """Открытые задачи."""
    from services.memory import get_open_tasks
    tasks = get_open_tasks()
    
    if not tasks:
        await message.answer("📋 Нет открытых задач.")
        return
    
    lines = []
    for t in tasks[:10]:
        due = f" (до {t['due_at'][:10]})" if t.get('due_at') else ""
        priority = "🔴" if t['priority'] == 'urgent' else "🟡" if t['priority'] == 'high' else "⚪"
        lines.append(f"{priority} {t['description'][:60]}{due}")
    
    await message.answer(
        f"📋 <b>Открытые задачи ({len(tasks)}):</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("digest"))
async def cmd_digest(message: Message, **kwargs):
    """Дайджест за сегодня."""
    from services.digest import generate_and_send_daily
    await generate_and_send_daily(message.chat.id)


@router.message(Command("weekly"))
async def cmd_weekly(message: Message, **kwargs):
    """Недельный отчёт."""
    from services.digest import generate_and_send_weekly
    await generate_and_send_weekly(message.chat.id)


@router.message(Command("find"))
async def cmd_find(message: Message, **kwargs):
    """Поиск по памяти."""
    query = message.text.split(maxsplit=1)
    if len(query) < 2:
        await message.answer("Использование: /find <запрос>")
        return
    
    from services.memory import search_memory
    results = search_memory(query[1])
    
    if not results:
        await message.answer("Ничего не найдено.")
        return
    
    lines = []
    for r in results[:5]:
        lines.append(f"• [{r['type']}] {r['content'][:80]}")
    
    await message.answer(
        f"🔍 <b>Результаты:</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(message: Message, **kwargs):
    """Справка."""
    text = (
        "🤖 <b>Lenochka — команды</b>\n\n"
        "/status — текущий статус CRM\n"
        "/leads — активные лиды\n"
        "/tasks — открытые задачи\n"
        "/digest — дайджест за сегодня\n"
        "/weekly — недельный отчёт\n"
        "/find <запрос> — поиск по памяти\n"
        "/help — эта справка\n\n"
        "💡 Просто пишите в свой Telegram — я автоматически "
        "анализирую переписки, извлекаю задачи и веду CRM."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)
```

### 5.7 Pipeline Processor (async queue + batching)

```python
# services/pipeline.py
import asyncio
import logging
from dataclasses import dataclass
from aiogram.types import Message

logger = logging.getLogger("lenochka.pipeline")


@dataclass
class PipelineItem:
    message: Message
    source: str  # "business", "business_edited", "direct"
    business_connection_id: str | None = None


class PipelineProcessor:
    """
    Async ingest pipeline с батчингом.
    
    Принимает сообщения через enqueue(), накапливает в батч,
    обрабатывает пачкой для экономии LLM-вызовов.
    """
    
    def __init__(self, brain, batch_size: int = 10, batch_interval: float = 5.0):
        self.brain = brain
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.queue: asyncio.Queue[PipelineItem] = asyncio.Queue()
        self._task: asyncio.Task | None = None
    
    async def start(self):
        """Запустить фоновый обработчик."""
        self._task = asyncio.create_task(self._process_loop())
        logger.info("Pipeline processor started")
    
    async def stop(self):
        """Остановить обработчик."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def enqueue(self, message: Message, source: str,
                      business_connection_id: str | None = None):
        """Добавить сообщение в очередь обработки."""
        item = PipelineItem(
            message=message,
            source=source,
            business_connection_id=business_connection_id,
        )
        await self.queue.put(item)
    
    async def handle_deleted(self, business_connection_id: str,
                             chat_id: int, message_ids: list[int]):
        """Обработать удалённые сообщения."""
        from services.memory import soft_delete_messages
        soft_delete_messages(chat_id, message_ids)
    
    async def _process_loop(self):
        """Фоновый цикл: собирает батч и обрабатывает."""
        while True:
            batch = []
            try:
                # Ждём первое сообщение
                item = await asyncio.wait_for(
                    self.queue.get(), timeout=self.batch_interval
                )
                batch.append(item)
                
                # Собираем остальные (не ждём больше batch_interval)
                deadline = asyncio.get_event_loop().time() + 1.0
                while len(batch) < self.batch_size:
                    timeout = deadline - asyncio.get_event_loop().time()
                    if timeout <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(
                            self.queue.get(), timeout=timeout
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break
                
                # Обработка батча
                await self._process_batch(batch)
                
            except asyncio.TimeoutError:
                # Нет сообщений — нормально, продолжаем
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Pipeline error: {e}", exc_info=True)
                # Не падаем, продолжаем обработку
    
    async def _process_batch(self, batch: list[PipelineItem]):
        """Обработать батч сообщений."""
        from services.normalizer import normalize_message
        from services.contact_resolver import resolve_contact
        from services.crm_upsert import crm_upsert
        from services.memory import store_message, dedup_check
        
        for item in batch:
            try:
                msg = item.message
                
                # 1. Dedup
                content_hash = dedup_check(msg)
                if content_hash is None:
                    continue  # Дубликат
                
                # 2. Normalize
                normalized = normalize_message(msg)
                if not normalized.text or normalized.text.startswith("[unsupported"):
                    continue
                
                # 3. Resolve contact + chat thread
                contact_id, chat_thread_id = resolve_contact(msg, item.source)
                
                # 4. Store raw message
                message_id = store_message(
                    chat_thread_id=chat_thread_id,
                    from_user_id=str(msg.from_user.id) if msg.from_user else "self",
                    text=normalized.text,
                    sent_at=msg.date,
                    content_type=normalized.content_type,
                    meta=normalized.metadata,
                    source_msg_id=msg.message_id,
                    content_hash=content_hash,
                )
                
                # 5. Classify (с контекстом последних 5 сообщений)
                chat_context = self._get_chat_context(chat_thread_id)
                label, conf, reason = self.brain.classify_message(
                    normalized.text, chat_context=chat_context
                )
                
                # 6. Extract entities
                entities = self.brain.extract_entities(
                    normalized.text, label=label, chat_context=chat_context
                )
                
                # 7-8. Store memory + CHAOS
                if label in ("task", "decision", "lead-signal", "risk"):
                    importance = 0.8 if label in ("decision", "risk") else 0.6
                    self.brain.store_memory(
                        content=f"[{label}] {normalized.text[:200]}",
                        mem_type="episodic",
                        importance=importance,
                        contact_id=contact_id,
                        chat_thread_id=chat_thread_id,
                        source_message_id=message_id,
                        content_hash=content_hash,
                    )
                    
                    self.brain.chaos_store(
                        content=normalized.text[:200],
                        category=label,
                        priority=importance,
                        contact_id=contact_id,
                    )
                
                # 9. CRM upsert (НОВОЕ — закрывает главную дыру)
                if entities and label not in ("noise", "chit-chat"):
                    crm_upsert(entities, contact_id, chat_thread_id, message_id)
                
                # 10. Mark analyzed
                self._mark_analyzed(message_id)
                
                logger.info(
                    f"Processed: msg#{message_id} [{label}] "
                    f"conf={conf:.2f} contact={contact_id}"
                )
                
            except Exception as e:
                logger.error(f"Batch item error: {e}", exc_info=True)
                # Не падаем, переходим к следующему элементу
    
    def _get_chat_context(self, chat_thread_id: int) -> str:
        """Получить последние 5 сообщений из чата для контекста классификации."""
        # SQL query к messages table
        import sqlite3
        conn = sqlite3.connect(str(self.brain.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT text, from_user_id, sent_at FROM messages
            WHERE chat_thread_id = ?
            ORDER BY sent_at DESC LIMIT 5
        """, (chat_thread_id,)).fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        lines = []
        for r in reversed(rows):
            author = "Я" if r["from_user_id"] == "self" else "Клиент"
            lines.append(f"[{author}: {r['text'][:100]}]")
        
        return " ".join(lines)
    
    def _mark_analyzed(self, message_id: int):
        import sqlite3
        conn = sqlite3.connect(str(self.brain.db_path))
        conn.execute(
            "UPDATE messages SET analyzed = 1 WHERE id = ?", (message_id,)
        )
        conn.commit()
        conn.close()
```

---

## 6. SCHEDULER (APScheduler)

```python
# services/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging

logger = logging.getLogger("lenochka.scheduler")


def setup_scheduler(bot, brain) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    
    # Утренний дайджест — каждый день 08:00 GMT+8
    scheduler.add_job(
        send_daily_digest,
        CronTrigger(hour=8, minute=0, timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="daily_digest",
        name="Daily Digest",
    )
    
    # Недельный отчёт — воскресенье 18:00 GMT+8
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="weekly_report",
        name="Weekly Report",
    )
    
    # Consolidate — каждый день 03:00 GMT+8 (низкая нагрузка)
    scheduler.add_job(
        run_consolidate,
        CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
        args=[brain],
        id="consolidate",
        name="Memory Consolidation",
    )
    
    # Проверка брошенных диалогов — каждые 4 часа
    scheduler.add_job(
        check_abandoned,
        CronTrigger(hour="*/4", timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="abandoned_check",
        name="Abandoned Dialogues Check",
    )
    
    return scheduler


async def send_daily_digest(bot, brain):
    """Отправить утренний дайджест владельцу."""
    from config import settings
    from services.digest import generate_daily_digest
    
    digest_text = generate_daily_digest()
    await bot.send_message(
        chat_id=settings.owner_id,
        text=digest_text,
        parse_mode="HTML",
    )
    logger.info("Daily digest sent")


async def send_weekly_report(bot, brain):
    """Отправить недельный отчёт."""
    from config import settings
    from services.digest import generate_weekly_digest
    
    report = generate_weekly_digest()
    await bot.send_message(
        chat_id=settings.owner_id,
        text=report,
        parse_mode="HTML",
    )
    logger.info("Weekly report sent")


async def run_consolidate(brain):
    """Запустить ночную консолидацию памяти."""
    from services.memory import run_consolidation
    run_consolidation()
    logger.info("Consolidation completed")


async def check_abandoned(bot, brain):
    """Проверить брошенные диалоги и уведомить."""
    from config import settings
    from services.memory import get_abandoned_dialogues
    
    abandoned = get_abandoned_dialogues(hours=48)
    if not abandoned:
        return
    
    lines = [f"• {d['contact_name'] or d['title']}: {int(d['hours'])}ч без ответа" 
             for d in abandoned[:5]]
    
    text = (
        f"👻 <b>Брошенные диалоги ({len(abandoned)}):</b>\n\n"
        + "\n".join(lines)
    )
    
    await bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode="HTML",
    )
```

---

## 7. BRAIN WRAPPER (daemon mode для brain.py)

```python
# services/brain_wrapper.py
"""
Обёртка над brain.py — модель загружается ОДИН раз при старте.
Все последующие вызовы работают с уже загруженной моделью.
Это решает проблему холодного старта 6.6с на каждый CLI-вызов.
"""
import sys
import logging
from pathlib import Path

logger = logging.getLogger("lenochka.brain")

# Добавляем lenochka-memory в path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "lenochka-memory"))


class BrainWrapper:
    def __init__(self):
        self._classify_message = None
        self._extract_entities = None
        self._embed_text = None
        self._store = None
        self._chaos_store = None
        self._recall = None
        self._build_context_packet = None
        self._generate_daily_digest = None
        self._generate_weekly_digest = None
        self._consolidate = None
        self.db_path = None
    
    async def initialize(self):
        """Загрузить brain.py один раз. Модель эмбеддингов живёт в памяти."""
        import brain
        import mem
        
        self._classify_message = brain.classify_message
        self._extract_entities = brain.extract_entities
        self._embed_text = brain.embed_text
        self._store = mem.store
        self._chaos_store = mem.chaos_store
        self._recall = mem.recall
        self._build_context_packet = brain.build_context_packet
        self._generate_daily_digest = brain.generate_daily_digest
        self._generate_weekly_digest = brain.generate_weekly_digest
        self._consolidate = mem.consolidate
        self.db_path = mem.DB_PATH
        
        # Прогрев модели эмбеддингов
        logger.info("Loading embedding model...")
        _ = self._embed_text("warmup")
        logger.info("Brain initialized successfully")
    
    def classify_message(self, text, chat_context=None):
        return self._classify_message(text, chat_context)
    
    def extract_entities(self, text, label=None, chat_context=None):
        return self._extract_entities(text, label, chat_context)
    
    def embed_text(self, text):
        return self._embed_text(text)
    
    def store_memory(self, **kwargs):
        return self._store(**kwargs)
    
    def chaos_store(self, **kwargs):
        return self._chaos_store(**kwargs)
    
    def recall(self, **kwargs):
        return self._recall(**kwargs)
    
    def build_context_packet(self, **kwargs):
        return self._build_context_packet(**kwargs)
    
    def daily_digest(self, date=None):
        return self._generate_daily_digest(date)
    
    def weekly_digest(self):
        return self._generate_weekly_digest()
    
    def consolidate(self):
        return self._consolidate()
```

---

## 8. ПОДВОДНЫЕ КАМНИ И КРАЕВЫЕ СЛУЧАИ

### 8.1 Telegram Business API — что может пойти не так

| Проблема | Сценарий | Решение |
|----------|---------|---------|
| **Петля сообщений** | Бот отвечает → видит свой ответ → обрабатывает → отвечает снова | Игнорируем `sender_business_bot` и `is_from_offline` |
| **Права revoked** | Пользователь отключил бота в Business-настройках | Проверяем `bc.status == "revoked"`, ставим ingestion на паузу |
| **Нет can_read_messages** | Бот подключён, но не может читать сообщения | При `business_connection` проверяем права, предупреждаем если нет |
| **Несколько бизнес-аккаунтов** | Один бот подключён к 2+ аккаунтам | Маппинг `business_connection_id → owner_id` |
| **Сообщение без текста** | Пустой sticker, location без подписи | Normalize layer обрабатывает все типы |
| **Дубли при long-polling** | Telegram повторяет update при сетевых сбоях | Dedup по `update_id` (Aiogram handles) + `content_hash` + `source_message_id` |
| **Rate limit 429** | Слишком много API-вызовов | Aiogram built-in retry + throttling middleware |
| **Edited = новое сообщение** | Telegram шлёт `edited_business_message` полностью | Supersede: ищем по `source_message_id`, обновляем контент |
| **Deleted = потеря контекста** | Удалённое сообщение было частью диалога | Soft-delete: помечаем, не удаляем физически |
| **Group messages** | Бот видит сообщения в группах бизнес-аккаунта | Фильтр по `chat.type`, обрабатываем только если нужно |
| **Forwarded messages** | Камиль пересылает от клиента, `forward_origin` есть | Парсим origin для attribution |
| **Voice messages** | Клиент говорит «да, 150к» голосом | Заглушка + file_id сохраняем для будущей транскрипции |
| **Sticker as confirmation** | Клиент отвечает 👍 | Emoji intent mapping перед classify |
| **Message thread (topics)** | Супергруппа с топиками | `message_thread_id` как часть контекста |
| **business_connection_id ≠ chat_id** | connection_id — ID подключения, не чата | Отдельная таблица маппинга |

### 8.2 Aiogram 3.x — подводные камни

| Проблема | Сценарий | Решение |
|----------|---------|---------|
| **Middleware order** | Auth до Throttling = можно спамить если owner | Throttling → Auth → Context |
| **FSM state leak** | Состояние сохраняется между пользователями | Используем `user_id + chat_id` composite key |
| **Memory leak в middleware** | Throttling dict растёт бесконечно | TTL-очистка каждые 5 минут |
| **Handler exception** | Ошибка в одном handler ломает весь polling | Global error handler в errors.py |
| **Long polling timeout** | Telegram 502/503 при высокой нагрузке | Aiogram retry + exponential backoff |
| **Потеря сообщений при рестарте** | Нет checkpointing offset | Webhook mode или offset в Redis |
| **Async DB access** | sqlite3 блокирует event loop | `asyncio.to_thread()` для DB-вызовов |
| **Startup/shutdown hooks** | Инициализация БД до старта polling | `dp.startup.register()` и `dp.shutdown.register()` |

### 8.3 Edge Cases — полная матрица

| # | Сценарий | Что происходит сейчас | Что должно быть |
|---|---------|---------------------|----------------|
| 1 | Клиент пишет 👍 на «150к до пятницы?» | Normalize → "[sticker: 👍 → confirm]" | Classify с reply-контекстом → decision |
| 2 | Камиль редактирует «120к» → «150к» | edited_business_message | Supersede: обновить memory, пересчитать embedding |
| 3 | Клиент удаляет сообщение | deleted_business_messages | Soft-delete в messages, memory не трогаем |
| 4 | Голосовое «да, 150к, к пятнице» | "[voice message: 15s — transcription pending]" | Сохранить file_id, пока заглушка |
| 5 | Фото счёта с подписью «Вот реквизиты» | "[photo] Вот реквизиты" | Caption → ingest, OCR → Phase 4 |
| 6 | Пересланное сообщение от Ивана | forward_origin = MessageOriginUser | "[Forwarded from: Иван] {text}" |
| 7 | Reply на «Сделай КП»: «Готово» | reply_to_message контекст | "Готово" = task-done, не chit-chat |
| 8 | Бот отправил автоответ → бизнес-сообщение | sender_business_bot заполнен | Игнорируем (антипетля) |
| 9 | Клиент пишет «Не 150, а 120» | amounts=[150, 120] | Supersede: 120 актуально, 150 отменено |
| 10 | Два клиента говорят «согласен» | Два разных contact_id | Правильная привязка к своим сделкам |
| 11 | Бот подключён к 2 бизнес-аккаунтам | Два business_connection_id | Маппинг каждого на своего owner |
| 12 | Сообщение длиной 4096 символов | Telegram limit | Обработка длинных сообщений, не обрезка |
| 13 | Медиа-группа (album) | 10 фото одним альбомом | media_group_id → batch processing |
| 14 | Камиль в группе с клиентом | Бот видит оба бока | Определяем роль: owner vs client |
| 15 | 50 сообщений за минуту (бурный чат) | 50 ingest-вызовов | Pipeline batching: 10msg/5сек |

---

## 9. DATA FLOW — ПОЛНАЯ ЦЕПОЧКА

```
Telegram Cloud
    │
    │  [business_message / message]
    │
    ▼
Aiogram Dispatcher
    │
    ├─ ThrottlingMiddleware (rate limit per user)
    ├─ AuthMiddleware (owner check / business check)
    ├─ ContextMiddleware (reply, forward context)
    ├─ DbMiddleware (inject DB connection)
    └─ LoggingMiddleware (structured log)
    │
    ▼
Router: handlers/business.py или handlers/commands.py
    │
    ├─ Business → pipeline.enqueue()
    ├─ Command  → direct handler
    │
    ▼
PipelineProcessor (async queue, batched)
    │
    ├─ 1. dedup_check (content_hash + source_message_id)
    ├─ 2. normalize (extract_text + resolve_reply + resolve_forward)
    ├─ 3. resolve_contact (tg_user → CRM contact upsert)
    ├─ 4. store_message (INSERT messages, analyzed=false)
    ├─ 5. classify (brain.classify + chat context window)
    ├─ 6. extract (brain.extract_entities)
    ├─ 7. store_memory (episodic + vector embedding)
    ├─ 8. chaos_store (FTS5 trigram + vector)
    ├─ 9. crm_upsert (contacts, deals, tasks, leads, agreements)
    └─ 10. mark_analyzed (UPDATE messages SET analyzed=true)
    │
    ▼
SQLite WAL (lenochka.db)
    │
    ├─ CRM tables (contacts, deals, tasks, leads...)
    ├─ Agent Memory (memories + vec_memories)
    ├─ CHAOS (chaos_entries + vec_chaos + chaos_fts)
    └─ Messages (messages, chat_threads)
```

---

## 10. REQUIREMENTS

```txt
# Core
aiogram>=3.26.0
pydantic-settings>=2.0
apscheduler>=3.10

# Brain
sentence-transformers>=5.0  # optional, fallback если нет
sqlite-vec>=0.1.0           # optional, fallback если нет
numpy>=2.0

# Infra
python-dotenv>=1.0
aiohttp>=3.9
```

---

## 11. DEPLOYMENT

### Development
```bash
# .env
LEN_BOT_TOKEN=your-token-here
LEN_OWNER_ID=your-telegram-user-id

# Run
python -m lenochka-bot
```

### Production (systemd)
```ini
[Unit]
Description=Lenochka Telegram Bot
After=network.target

[Service]
Type=simple
User=lenochka
WorkingDirectory=/opt/lenochka
ExecStart=/opt/lenochka/venv/bin/python -m lenochka-bot
Restart=always
RestartSec=5
Environment=LEN_BOT_TOKEN=xxx
Environment=LEN_OWNER_ID=xxx

[Install]
WantedBy=multi-user.target
```

### Production (Docker)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "lenochka-bot"]
```

---

## 12. IMPLEMENTATION ORDER

| Phase | Что | Файлы | Оценка |
|-------|-----|-------|--------|
| **1** | Config + Bot skeleton + entry point | `config.py`, `bot.py`, `__main__.py` | 30 мин |
| **2** | Brain Wrapper (daemon mode) | `services/brain_wrapper.py` | 30 мин |
| **3** | Normalizer (extract text, emoji, reply, forward) | `services/normalizer.py`, `utils/emoji.py` | 1 час |
| **4** | Contact Resolver (tg_user → CRM contact) | `services/contact_resolver.py` | 30 мин |
| **5** | CRM Upsert (entities → CRM tables) | `services/crm_upsert.py` | 1 час |
| **6** | Pipeline Processor (async queue + batching) | `services/pipeline.py` | 1.5 часа |
| **7** | Business Handlers (connection, messages, edited, deleted) | `handlers/business.py`, `filters/business.py` | 1 час |
| **8** | Command Handlers (/status, /leads, /tasks, /digest...) | `handlers/commands.py` | 1 час |
| **9** | Middleware (throttling, auth, context, logging) | `middlewares/*.py` | 1 час |
| **10** | Scheduler (дайджесты, consolidate, abandoned) | `services/scheduler.py`, `services/digest.py` | 1 час |
| **11** | Error handling + logging | `handlers/errors.py`, `utils/logging.py` | 30 мин |
| **12** | Integration testing + edge cases | все файлы | 2 часа |
| **Итого** | | | **~12 часов** |

---

## 13. КЛЮЧЕВЫЕ РЕШЕНИЯ

1. **Business API > Userbot** — легально, стабильно, нет риска бана
2. **Aiogram 3.x > python-telegram-bot** — async-first, лучше middleware, типизация
3. **Async pipeline > inline processing** — не блокирует Telegram API при LLM-вызовах
4. **Batching > per-message** — 10 сообщений в одном classify = 60% меньше токенов
5. **Brain wrapper > CLI calls** — модель один раз в памяти, нет 6.6с cold start
6. **SQLite WAL > PostgreSQL** — проще, быстрее для single-user, миграция позже
7. **Scheduler > heartbeat** — точное время дайджестов, не зависит от polling
8. **Soft-delete > hard-delete** — данные не теряются, можно восстановить
