#!/usr/bin/env python3
"""
Lenochka Pipeline — Полный пайплайн обработки входящего сообщения.

Выходной контракт:
  - Нет вывода  → SILENT (ничего не отправлять в Telegram)
  - Текст в stdout → отправить этот текст в Telegram

Использование:
  python3 run_pipeline.py \\
    --text "текст сообщения" \\
    --sender_id 123456789 \\
    --sender_name "Иван" \\
    --chat_id "-100123456" \\
    --message_id 42 \\
    [--tg_username "ivan"] \\
    [--chat_type personal|group|supergroup|channel] \\
    [--chat_title "Название чата"] \\
    [--is_owner] \\
    [--is_owner_chat] \\
    [--business_connection_id "abc123"] \\
    [--event_type message|edited_message|business_message|...] \\
    [--deleted_message_ids "1,2,3"] \\
    [--content_type text|sticker|photo|video|voice|document|...] \\
    [--sticker_emoji "👍"] \\
    [--reply_to_text "текст цитаты"] \\
    [--reply_to_author "Имя"] \\
    [--forward_from "Источник"]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Пути ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_DIR = PROJECT_ROOT / "lenochka-memory"
STATE_FILE = MEMORY_DIR / "db" / "response_state.json"
sys.path.insert(0, str(MEMORY_DIR))

try:
    import mem
    import brain
except ImportError as e:
    print(f"[pipeline] Import error: {e}", file=sys.stderr)
    sys.exit(1)

# ── Константы ─────────────────────────────────────────────────────────────────
TZ_OWNER = timezone(timedelta(hours=8))  # GMT+8
NIGHT_HOUR_START = 23
NIGHT_HOUR_END = 8
MIN_RESPONSE_INTERVAL_S = 180  # 3 мин между ответами в один чат
MAX_CONSECUTIVE_RESPONSES = 3  # после 3 подряд — cooldown
COOLDOWN_S = 900  # 15 мин cooldown
REPEAT_WINDOW_S = 600  # 10 мин окно для детекта повторов
MAX_REPEATED_UNANSWERED = 3  # после 3 повторов → эскалация

# Камиль — owner. Уведомления идут в его личный чат.
OWNER_TELEGRAM_ID = "5944980799"

# Валидные labels от LLM
VALID_LABELS: frozenset[str] = frozenset(
    {
        "noise",
        "chit-chat",
        "business-small",
        "task",
        "decision",
        "lead-signal",
        "risk",
    }
)

# Stage progression order (индекс = порядок)
_STAGE_ORDER: dict[str, int] = {
    "discovery": 0,
    "proposal": 1,
    "negotiation": 2,
    "closed_won": 3,
    "closed_lost": 99,
}

# Эмодзи-иконки для уведомлений Камилю по типу события
_ESCALATION_EMOJI: dict[str, str] = {
    "lead-signal": "💼",
    "risk": "🚨",
    "task": "📋",
    "complaint": "🚨",
    "default": "🔔",
}

# Безопасные для fast-skip фразы (НЕ включают deal-confirm слова)
SILENT_PHRASES: frozenset[str] = frozenset(
    {
        "ок",
        "окей",
        "ok",
        "okay",
        "ладно",
        "понял",
        "поняла",
        "понятно",
        "угу",
        "ага",
        "ясно",
        "received",
        "got it",
        "noted",
        "yep",
        "yup",
    }
)

# Фразы которые МОГУТ быть подтверждением сделки — НЕ fast-skip, а ingest
DEAL_CONFIRM_PHRASES: frozenset[str] = frozenset(
    {
        "согласен",
        "согласна",
        "договорились",
        "принято",
        "отлично",
        "хорошо",
        "sure",
    }
)

SILENT_EMOJIS: frozenset[str] = frozenset({"👍", "🤝", "✅", "👌", "💯", "👊", "☑️"})
SILENT_LABELS: frozenset[str] = frozenset({"noise", "chit-chat", "business-small"})
CRM_LABELS: frozenset[str] = frozenset({"task", "decision", "lead-signal", "risk"})

# Вариативные шаблоны ответов клиенту (выбираются случайно)
ESCALATE_TEMPLATES: dict[str, list[str]] = {
    "lead-signal": [
        "Передала менеджеру, скоро ответит!",
        "Уже передала, ответит в ближайшее время.",
        "Приняла! Менеджер скоро свяжется.",
        "Записала, ждите ответ от менеджера.",
    ],
    "risk": [
        "Поняла, срочно передала. Ответит как можно скорее!",
        "Вопрос на контроле, передала прямо сейчас.",
        "Получила, уже передаю менеджеру — ответит в ближайшее время.",
    ],
    "task": [
        "Принято! Уточню детали и вернусь к вам.",
        "Записала, передала менеджеру для уточнения.",
        "Поняла, разберёмся и вернёмся с ответом.",
    ],
    "default": [
        "Передала менеджеру, скоро ответим!",
        "Вопрос передан, ждите ответ.",
        "Приняла, менеджер ответит в ближайшее время.",
    ],
}

# Ночной режим — клиенту говорим что ответим утром
ESCALATE_NIGHT_TEMPLATES: dict[str, list[str]] = {
    "lead-signal": [
        "Спасибо за сообщение! Ответим утром, как только начнётся рабочий день.",
        "Принято! Сейчас нерабочее время — свяжемся утром.",
    ],
    "risk": [
        "Получила ваш вопрос. Срочно — передала, ответит в ближайшее время.",
    ],
    "task": [
        "Принято! Сейчас ночь — разберёмся утром и вернёмся к вам.",
    ],
    "default": [
        "Спасибо! Сейчас нерабочее время — ответим утром.",
        "Принято, ответим с утра!",
    ],
}

EMOJI_INTENT: dict[str, str] = {
    "👍": "confirm",
    "👌": "confirm",
    "🤝": "confirm",
    "👊": "confirm",
    "✅": "done",
    "☑️": "done",
    "🎉": "done",
    "❌": "cancel",
    "🚫": "cancel",
    "👎": "cancel",
    "🔥": "urgent",
    "⚡": "urgent",
    "⏰": "reminder",
    "💰": "payment",
    "💵": "payment",
    "💸": "payment",
    "❤️": "approve",
    "💯": "approve",
    "🙌": "approve",
    "📅": "schedule",
    "🗓️": "schedule",
    "😂": "laugh",
    "🤣": "laugh",
    "😅": "nervous",
    "🤔": "thinking",
    "😢": "sad",
    "🙏": "please",
}


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    """Все логи только в stderr — никогда не попадут в Telegram."""
    print(f"[pipeline] {msg}", file=sys.stderr)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(mem.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_night_for_owner() -> bool:
    h = datetime.now(TZ_OWNER).hour
    return h >= NIGHT_HOUR_START or h < NIGHT_HOUR_END


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1: NORMALIZE
# ─────────────────────────────────────────────────────────────────────────────


def normalize_text(args: argparse.Namespace) -> str:
    text = (args.text or "").strip()

    content_type = args.content_type or "text"

    if content_type == "sticker":
        emoji = args.sticker_emoji or ""
        intent = EMOJI_INTENT.get(emoji, "sticker")
        text = f"[sticker: {emoji} = {intent}]"
    elif content_type == "voice":
        text = f"[voice]{f': {text}' if text else ''}"
    elif content_type == "photo":
        text = f"[photo]{f': {text}' if text else ''}"
    elif content_type == "video":
        text = f"[video]{f': {text}' if text else ''}"
    elif content_type == "video_note":
        text = f"[video note]{f': {text}' if text else ''}"
    elif content_type == "document":
        text = f"[document]{f': {text}' if text else ''}"
    elif content_type == "contact":
        text = f"[contact: {text}]" if text else "[contact]"
    elif content_type == "location":
        text = f"[location: {text}]" if text else "[location]"
    elif content_type == "dice":
        text = f"[dice: {text}]" if text else "[dice]"

    if args.reply_to_text:
        snippet = args.reply_to_text[:150]
        author = args.reply_to_author or "?"
        text = f'[Reply to {author}: "{snippet}"] {text}'

    if args.forward_from:
        text = f"[Forwarded from {args.forward_from}] {text}"

    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2: FAST SKIP (без LLM)
# ─────────────────────────────────────────────────────────────────────────────


def is_fast_skip(text: str) -> bool:
    """Проверить правила молчания без LLM. True = молчать."""
    if not text:
        return True
    # Убираем пунктуацию в конце и приводим к нижнему регистру
    normalized = re.sub(r"[\s.,!?;:]+$", "", text.lower().strip())
    if normalized in SILENT_PHRASES:
        log(f"fast_skip: phrase '{normalized}'")
        return True
    # Deal-confirm фразы НЕ пропускаем — они могут быть подтверждением сделки
    if normalized in DEAL_CONFIRM_PHRASES:
        log(f"fast_skip: DEAL CONFIRM phrase '{normalized}' → let ingest decide")
        return False
    if text.strip() in SILENT_EMOJIS:
        log(f"fast_skip: emoji '{text.strip()}'")
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3: DEDUP
# ─────────────────────────────────────────────────────────────────────────────


def is_duplicate(
    conn: sqlite3.Connection, chat_thread_id: int, source_msg_id: int | None
) -> bool:
    if source_msg_id is None:
        return False
    row = conn.execute(
        "SELECT id FROM messages WHERE chat_thread_id=? AND source_msg_id=?",
        (chat_thread_id, source_msg_id),
    ).fetchone()
    if row:
        log(f"dedup: source_msg_id={source_msg_id} already in messages id={row['id']}")
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3: RESOLVE CONTACT + THREAD
# ─────────────────────────────────────────────────────────────────────────────


def resolve_contact(
    conn: sqlite3.Connection,
    sender_id: int | None,
    sender_name: str | None,
    username: str | None,
) -> int:
    existing = None
    if sender_id is not None:
        existing = conn.execute(
            "SELECT id, name FROM contacts WHERE tg_user_id=?", (str(sender_id),)
        ).fetchone()
    if not existing and username:
        uname = username.lstrip("@")
        existing = conn.execute(
            "SELECT id, name FROM contacts WHERE tg_username=?", (uname,)
        ).fetchone()

    if existing:
        contact_id: int = existing["id"]
        if sender_name and sender_name != existing["name"]:
            conn.execute(
                "UPDATE contacts SET name=?, updated_at=datetime('now') WHERE id=?",
                (sender_name, contact_id),
            )
            conn.commit()
        return contact_id

    name = (
        sender_name
        or (f"@{username.lstrip('@')}" if username else None)
        or (f"tg_{sender_id}" if sender_id else "unknown")
    )
    conn.execute(
        "INSERT INTO contacts (name, tg_username, tg_user_id) VALUES (?, ?, ?)",
        (
            name,
            username.lstrip("@") if username else None,
            str(sender_id) if sender_id is not None else None,
        ),
    )
    conn.commit()
    cid: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log(f"resolve_contact: created new contact_id={cid} name={name!r}")
    return cid


def resolve_chat_thread(
    conn: sqlite3.Connection,
    chat_id: str,
    contact_id: int,
    chat_type: str,
    title: str | None,
) -> int:
    existing = conn.execute(
        "SELECT id FROM chat_threads WHERE tg_chat_id=?", (chat_id,)
    ).fetchone()
    if existing:
        return existing["id"]
    conn.execute(
        "INSERT INTO chat_threads (tg_chat_id, contact_id, type, title) VALUES (?, ?, ?, ?)",
        (chat_id, contact_id, chat_type, title),
    )
    conn.commit()
    tid: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log(f"resolve_thread: created chat_thread_id={tid} chat_id={chat_id}")
    return tid


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 4: STORE MESSAGE
# ─────────────────────────────────────────────────────────────────────────────


def store_message(
    conn: sqlite3.Connection,
    chat_thread_id: int,
    from_user_id: str | None,
    text: str,
    source_msg_id: int | None,
    chash: str,
    content_type: str | None,
) -> int:
    meta = (
        json.dumps({"content_type": content_type}, ensure_ascii=False)
        if content_type
        else None
    )
    conn.execute(
        """INSERT INTO messages
               (chat_thread_id, from_user_id, text, sent_at,
                source_msg_id, content_hash, meta_json, analyzed)
           VALUES (?, ?, ?, datetime('now'), ?, ?, ?, 0)""",
        (chat_thread_id, from_user_id, text[:4096], source_msg_id, chash, meta),
    )
    conn.commit()
    msg_id: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log(f"store_message: msg_id={msg_id}")
    return msg_id


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 5: INGEST (classify + extract + memory + chaos)
# ─────────────────────────────────────────────────────────────────────────────


def _call_silent(fn, *args, **kwargs):
    """Вызвать fn с перехватом stdout → stderr (чтобы ничего лишнего не попало в Telegram)."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = fn(*args, **kwargs)
    output = captured.getvalue()
    if output.strip():
        log(f"mem stdout: {output.strip()[:300]}")
    return result


def run_ingest(
    text: str, contact_id: int, chat_thread_id: int, message_id: int
) -> dict:
    try:
        result = _call_silent(
            mem.ingest,
            text=text,
            contact_id=contact_id,
            chat_thread_id=chat_thread_id,
            source_message_id=message_id,
        )
        if isinstance(result, str):
            result = json.loads(result)
        if isinstance(result, dict):
            return result
    except Exception as e:
        log(f"ingest error: {e}")

    # Fallback: manual classify (тоже silent)
    try:
        label, conf, reasoning = _call_silent(brain.classify_message, text)
    except Exception as e:
        log(f"classify fallback error: {e}")
        label, conf, reasoning = "other", 0.5, ""
    return {"label": label, "confidence": conf, "reasoning": reasoning, "stored": False}


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 6: CRM UPSERT
# ─────────────────────────────────────────────────────────────────────────────


def crm_upsert(
    conn: sqlite3.Connection,
    label: str,
    entities: dict,
    contact_id: int,
    message_id: int,
) -> None:
    if label not in CRM_LABELS or not entities:
        return

    # Deal / amounts
    raw_amounts = entities.get("amounts", [])
    amounts = []
    for a in raw_amounts:
        try:
            amounts.append(float(a))
        except (TypeError, ValueError):
            pass

    if amounts and label in ("decision", "lead-signal"):
        amount = max(amounts)
        stage = "closed_won" if label == "decision" else "discovery"
        existing = conn.execute(
            "SELECT id, stage FROM deals WHERE contact_id=? "
            "AND stage NOT IN ('closed_won','closed_lost') LIMIT 1",
            (contact_id,),
        ).fetchone()
        if existing:
            old_stage = existing["stage"]
            # Не откатывать stage назад — только продвигать
            if _STAGE_ORDER.get(stage, 0) > _STAGE_ORDER.get(old_stage, 0):
                conn.execute(
                    "UPDATE deals SET amount=?, stage=?, updated_at=datetime('now') WHERE id=?",
                    (max(amount, existing.get("amount") or 0), stage, existing["id"]),
                )
                log(f"crm: deal stage {old_stage} → {stage}")
            else:
                # Обновить только amount если больше
                conn.execute(
                    "UPDATE deals SET amount=MAX(amount, ?), updated_at=datetime('now') WHERE id=?",
                    (amount, existing["id"]),
                )
                log(f"crm: deal amount updated, stage kept at {old_stage}")
        else:
            conn.execute(
                "INSERT INTO deals (contact_id, amount, stage) VALUES (?, ?, ?)",
                (contact_id, amount, stage),
            )
        conn.commit()
        log(f"crm: deal upserted amount={amount} stage={stage}")

    # Tasks
    task_entities = entities.get("tasks") or []
    if isinstance(task_entities, list):
        for task in task_entities[:3]:
            if not isinstance(task, dict):
                continue
            desc = task.get("description", "")
            if not desc:
                continue
            conn.execute(
                "INSERT INTO tasks "
                "(description, related_type, related_id, due_at, priority, source_message_id) "
                "VALUES (?, 'contact', ?, ?, ?, ?)",
                (
                    desc,
                    contact_id,
                    task.get("due_date"),
                    task.get("priority", "normal"),
                    message_id,
                ),
            )
        conn.commit()
        log(f"crm: {len(task_entities)} task(s) stored")

    # Lead
    if label == "lead-signal":
        existing_lead = conn.execute(
            "SELECT id FROM leads WHERE contact_id=? AND status NOT IN ('won','lost') LIMIT 1",
            (contact_id,),
        ).fetchone()
        if not existing_lead:
            amount = max(amounts) if amounts else None
            conn.execute(
                "INSERT INTO leads (contact_id, source, status, amount) VALUES (?, 'telegram', 'new', ?)",
                (contact_id, amount),
            )
            conn.commit()
            log(f"crm: new lead for contact_id={contact_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Anti-spam state (persisted to JSON file with file locking)
# ─────────────────────────────────────────────────────────────────────────────

_LOCK_FILE = MEMORY_DIR / "db" / "response_state.lock"
_STATE_MAX_AGE_DAYS = 30  # записи старше N дней удаляются


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state_atomic(state: dict) -> None:
    """Атомарная запись: tmp файл → rename. Не бьётся при crash."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(STATE_FILE.parent), prefix=".state_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(STATE_FILE))
        except Exception:
            os.unlink(tmp)
            raise
    except Exception as e:
        log(f"state save error: {e}")


def _with_state_lock(fn):
    """Обёртка: захватывает file lock, делает read-modify-write."""

    def wrapper(*args, **kwargs):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(str(_LOCK_FILE), "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            result = fn(*args, **kwargs)
            return result
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    return wrapper


@_with_state_lock
def can_respond(chat_id: str) -> bool:
    """True = anti-spam разрешает ответ."""
    state = _load_state()
    chat_state = state.get(chat_id, {})
    now_ts = now_utc().timestamp()

    cooldown_until = chat_state.get("cooldown_until", 0)
    if now_ts < cooldown_until:
        log(f"antispam: cooldown for {chat_id} until {cooldown_until:.0f}")
        return False

    last_response = chat_state.get("last_response", 0)
    if now_ts - last_response < MIN_RESPONSE_INTERVAL_S:
        log(
            f"antispam: too soon ({now_ts - last_response:.0f}s < {MIN_RESPONSE_INTERVAL_S}s)"
        )
        return False

    return True


@_with_state_lock
def record_response(chat_id: str) -> None:
    """Обновить счётчик после отправленного ответа."""
    state = _load_state()
    chat_state = state.get(chat_id, {})
    now_ts = now_utc().timestamp()

    last_response = chat_state.get("last_response", 0)
    consecutive = chat_state.get("consecutive", 0)

    # Сбросить consecutive если пауза > 10 мин
    if now_ts - last_response > 600:
        consecutive = 0

    consecutive += 1
    chat_state["last_response"] = now_ts
    chat_state["consecutive"] = consecutive

    if consecutive >= MAX_CONSECUTIVE_RESPONSES:
        chat_state["cooldown_until"] = now_ts + COOLDOWN_S
        chat_state["consecutive"] = 0
        log(f"antispam: cooldown {COOLDOWN_S}s triggered for {chat_id}")

    state[chat_id] = chat_state
    _save_state_atomic(state)


def cleanup_state(max_age_days: int = _STATE_MAX_AGE_DAYS) -> None:
    """Удалить неактивные записи из state файла."""
    try:
        state = _load_state()
        if not state:
            return
        now_ts = now_utc().timestamp()
        cutoff = now_ts - max_age_days * 86400
        cleaned = {
            k: v for k, v in state.items() if v.get("last_response", now_ts) >= cutoff
        }
        if len(cleaned) < len(state):
            log(f"state cleanup: removed {len(state) - len(cleaned)} old entries")
            _save_state_atomic(cleaned)
    except Exception as e:
        log(f"state cleanup error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 7: Эскалация → pending_notification + прямое уведомление Камилю
# ─────────────────────────────────────────────────────────────────────────────


def _send_owner_notification(text: str) -> None:
    """Отправить уведомление Камилю через openclaw message send."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                OWNER_TELEGRAM_ID,
                "--message",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log("owner_notify: sent ok")
        else:
            err = (result.stderr or "")[:200]
            log(f"owner_notify: failed rc={result.returncode} err={err}")
    except Exception as e:
        log(f"owner_notify: exception: {e}")


_ESCALATION_LABEL_RU: dict[str, str] = {
    "lead-signal": "Запрос / интерес клиента",
    "risk": "Жалоба / конфликт",
    "task": "Задача от клиента",
    "complaint": "Жалоба",
}


def _build_owner_notification(
    label: str, contact_name: str, chat_id: str, message_text: str, is_complaint: bool
) -> str:
    icon = _ESCALATION_EMOJI.get(
        "complaint" if is_complaint else label, _ESCALATION_EMOJI["default"]
    )
    label_ru = _ESCALATION_LABEL_RU.get(label, label)
    lines = [
        f"{icon} {label_ru}",
        f"👤 {contact_name}",
        f"💬 «{message_text[:300]}»",
        f"🔗 chat_id: {chat_id}",
    ]
    if is_complaint:
        lines.append("⚡ Срочно — клиент недоволен!")
    return "\n".join(lines)


def create_escalation(
    conn: sqlite3.Connection,
    chat_thread_id: int,
    contact_id: int,
    message_id: int,
    label: str,
    is_complaint: bool,
    contact_name: str = "Клиент",
    chat_id: str = "",
    message_text: str = "",
) -> None:
    """Создать pending_notification и немедленно уведомить Камиля."""
    delay_min = 10 if is_complaint else 30

    if is_night_for_owner() and not is_complaint:
        owner_now = datetime.now(TZ_OWNER)
        target = owner_now.replace(
            hour=NIGHT_HOUR_END, minute=0, second=0, microsecond=0
        )
        # hour >= 23 значит мы в "вечерней" части ночи → +1 день
        if owner_now.hour >= NIGHT_HOUR_START:
            target += timedelta(days=1)
        notify_at = target.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        log(f"escalation: night mode → deferred to {notify_at}")
        deferred = True
    else:
        notify_at = (now_utc() + timedelta(minutes=delay_min)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        deferred = False

    escalation_type = "complaint" if is_complaint else label
    conn.execute(
        "INSERT INTO pending_notifications "
        "(chat_thread_id, contact_id, message_id, entity_type, escalation_type, notify_at, status) "
        "VALUES (?, ?, ?, 'escalation', ?, ?, 'pending')",
        (chat_thread_id, contact_id, message_id, escalation_type, notify_at),
    )
    conn.commit()
    log(
        f"escalation: pending_notification created type={escalation_type} notify_at={notify_at}"
    )

    # Прямое уведомление Камилю — сразу (жалобы всегда, остальные кроме ночи)
    if not deferred or is_complaint:
        owner_text = _build_owner_notification(
            label=label,
            contact_name=contact_name,
            chat_id=chat_id,
            message_text=message_text,
            is_complaint=is_complaint,
        )
        _send_owner_notification(owner_text)


# ─────────────────────────────────────────────────────────────────────────────
# Шаг 7: RESPONSE DECISION (переписано)
# ─────────────────────────────────────────────────────────────────────────────


def _pick_template(label: str, night: bool = False) -> str:
    """Выбрать случайный шаблон ответа."""
    pool = ESCALATE_NIGHT_TEMPLATES if night else ESCALATE_TEMPLATES
    templates = pool.get(label) or pool.get("default") or ["Передала менеджеру."]
    return random.choice(templates)


def _try_fact_response(
    conn: sqlite3.Connection, contact_id: int, original_text: str
) -> str | None:
    """Попытаться ответить фактом из БД. Возвращает текст или None."""
    lower = original_text.lower()

    # Активные сделки
    if any(w in lower for w in ("сделк", "заказ", "договорил", "сколько")):
        row = conn.execute(
            "SELECT stage, amount FROM deals "
            "WHERE contact_id=? AND stage NOT IN ('closed_won','closed_lost') "
            "ORDER BY updated_at DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        if row:
            stage_ru = {
                "discovery": "в работе",
                "proposal": "на согласовании",
                "negotiation": "в переговорах",
            }.get(row["stage"], row["stage"])
            parts = [f"Сделка {stage_ru}"]
            if row["amount"]:
                parts.append(f"на {row['amount']:,.0f}".replace(",", " "))
            return ". ".join(parts) + "."

    # Просроченные задачи
    if any(w in lower for w in ("задач", "что делать", "что по план", "план")):
        rows = conn.execute(
            "SELECT description, due_at FROM tasks "
            "WHERE related_type='contact' AND related_id=? "
            "AND status NOT IN ('done','cancelled') "
            "ORDER BY due_at ASC LIMIT 3",
            (contact_id,),
        ).fetchall()
        if rows:
            lines = [
                f"• {r['description']}"
                + (f" (до {r['due_at'][:10]})" if r["due_at"] else "")
                for r in rows
            ]
            return "Задачи:\n" + "\n".join(lines)

    # Договор
    if any(w in lower for w in ("договор", "контракт")):
        row = conn.execute(
            "SELECT summary, status, due_at FROM agreements "
            "WHERE contact_id=? AND status NOT IN ('completed','cancelled') "
            "ORDER BY created_at DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        if row:
            status_ru = {
                "draft": "черновик",
                "sent": "отправлен",
                "signed": "подписан",
            }.get(row["status"], row["status"])
            parts = [f"Договор: {row['summary'] or 'без описания'}"]
            parts.append(f"статус — {status_ru}")
            if row["due_at"]:
                parts.append(f"срок — {row['due_at'][:10]}")
            return ". ".join(parts) + "."

    # Счета / оплата
    if any(w in lower for w in ("оплат", "счет", "счёт", "деньги")):
        row = conn.execute(
            "SELECT amount, status, due_at FROM invoices "
            "WHERE agreement_id IN (SELECT id FROM agreements WHERE contact_id=?) "
            "AND status NOT IN ('paid','cancelled') "
            "ORDER BY due_at ASC LIMIT 1",
            (contact_id,),
        ).fetchone()
        if row:
            status_ru = {
                "draft": "черновик",
                "sent": "выставлен",
                "overdue": "просрочен",
            }.get(row["status"], row["status"])
            parts = [f"Счёт {status_ru}"]
            if row["amount"]:
                parts.append(f"на {row['amount']:,.0f}".replace(",", " "))
            if row["due_at"]:
                parts.append(f"срок — {row['due_at'][:10]}")
            return ". ".join(parts) + "."

    return None


def _is_repeated_question(
    conn: sqlite3.Connection, chat_thread_id: int, current_text: str
) -> int:
    """Проверить, не повторяет ли клиент вопрос. Возвращает кол-во похожих за REPEAT_WINDOW_S."""
    cutoff = (now_utc() - timedelta(seconds=REPEAT_WINDOW_S)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        "SELECT text FROM messages "
        "WHERE chat_thread_id=? AND sent_at > ? AND analyzed=1 "
        "ORDER BY sent_at DESC LIMIT 10",
        (chat_thread_id, cutoff),
    ).fetchall()

    current_lower = current_text.lower().strip()
    repeats = 0
    for r in rows:
        prev = r["text"].lower().strip()
        # Простая проверка: совпадение >= 70% слов или Jaccard similarity
        words_curr = set(current_lower.split())
        words_prev = set(prev.split())
        if not words_curr or not words_prev:
            continue
        jaccard = len(words_curr & words_prev) / len(words_curr | words_prev)
        if jaccard >= 0.5:
            repeats += 1
    return repeats


def decide_response(
    conn: sqlite3.Connection,
    label: str,
    confidence: float,
    contact_id: int,
    chat_thread_id: int,
    message_id: int,
    chat_id: str,
    is_owner: bool,
    is_owner_chat: bool,
    contact_name: str = "Клиент",
    original_text: str = "",
) -> str | None:
    """Возвращает текст ответа для Telegram или None (молчать)."""
    night = is_night_for_owner()

    # Owner-related messages → не отвечать никогда
    if is_owner or is_owner_chat:
        log("response: owner context → silent")
        return None

    # SILENT labels (noise / chit-chat / business-small)
    if label in SILENT_LABELS:
        log(f"response: label={label} → silent")
        return None

    is_complaint = label == "risk"

    # ── ESCALATION (анти-spam НЕ блокирует!) ──
    # "other" = LLM не смог → эскалировать (безопаснее молчать)
    # "decision" + low confidence = deal-confirm safety net → эскалировать
    should_escalate = (
        label in ("lead-signal", "risk", "other")
        or (label == "task" and confidence < 0.85)
        or (label == "decision" and confidence < 0.5)
    )
    if should_escalate:
        # Проверить повторные вопросы → повышенная срочность
        repeats = _is_repeated_question(conn, chat_thread_id, original_text)
        if repeats >= MAX_REPEATED_UNANSWERED:
            log(f"response: {repeats} repeats detected → urgent escalation")
            is_complaint = True  # эскалировать как жалобу

        create_escalation(
            conn,
            chat_thread_id,
            contact_id,
            message_id,
            label,
            is_complaint,
            contact_name=contact_name,
            chat_id=chat_id,
            message_text=original_text,
        )
        reply = _pick_template(label, night=night and not is_complaint)
        record_response(chat_id)
        log(f"response: label={label} → escalate + reply to client")
        return reply

    # Confident task → ack + escalate for owner awareness
    if label == "task" and confidence >= 0.85:
        create_escalation(
            conn,
            chat_thread_id,
            contact_id,
            message_id,
            "task",
            False,
            contact_name=contact_name,
            chat_id=chat_id,
            message_text=original_text,
        )
        reply = _pick_template("task", night=night)
        record_response(chat_id)
        log(f"response: task conf={confidence:.2f} → ack")
        return reply

    # ── FACT RESPONSE: попытаться ответить из БД ──
    # Только если anti-spam разрешает (для фактов можно чаще)
    if label == "decision":
        fact = _try_fact_response(conn, contact_id, original_text)
        if fact and can_respond(chat_id):
            record_response(chat_id)
            log(f"response: fact response for decision")
            return fact
        # decision без фактов → silent (ingest сохранил)
        log(f"response: decision, no facts → silent")
        return None

    # other → silent
    log(f"response: label={label} conf={confidence:.2f} → silent")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Обработка edited_message
# ─────────────────────────────────────────────────────────────────────────────


def handle_edited(
    conn: sqlite3.Connection,
    chat_id: str,
    source_msg_id: int | None,
    text: str,
    chash: str,
) -> None:
    if source_msg_id is None:
        log("edited: no source_msg_id → skip")
        return

    row = conn.execute(
        "SELECT m.id FROM messages m "
        "JOIN chat_threads ct ON m.chat_thread_id = ct.id "
        "WHERE ct.tg_chat_id=? AND m.source_msg_id=?",
        (chat_id, source_msg_id),
    ).fetchone()

    if not row:
        log(f"edited: source_msg_id={source_msg_id} not found → skip")
        return

    msg_id: int = row["id"]
    conn.execute(
        "UPDATE messages SET text=?, content_hash=?, analyzed=0, classification=NULL WHERE id=?",
        (text[:4096], chash, msg_id),
    )

    # Update memory content if exists
    mem_row = conn.execute(
        "SELECT id FROM memories WHERE source_message_id=?", (msg_id,)
    ).fetchone()
    if mem_row:
        conn.execute(
            "UPDATE memories SET content=?, content_hash=? WHERE id=?",
            (text, chash, mem_row["id"]),
        )
        conn.execute(
            "UPDATE chaos_entries SET content=? WHERE memory_id=?",
            (text, mem_row["id"]),
        )

    conn.commit()
    log(f"edited: updated msg_id={msg_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Обработка deleted_messages
# ─────────────────────────────────────────────────────────────────────────────


def handle_deleted(
    conn: sqlite3.Connection, chat_id: str, deleted_ids_str: str | None
) -> None:
    if not deleted_ids_str:
        return
    ids = []
    for part in deleted_ids_str.split(","):
        try:
            ids.append(int(part.strip()))
        except ValueError:
            pass
    if not ids:
        return

    for mid in ids:
        conn.execute(
            "UPDATE messages "
            "SET meta_json = json_set(COALESCE(meta_json, '{}'), '$.deleted', 1) "
            "WHERE source_msg_id=? "
            "AND chat_thread_id IN (SELECT id FROM chat_threads WHERE tg_chat_id=?)",
            (mid, chat_id),
        )
    conn.commit()
    log(f"deleted: soft-deleted {len(ids)} messages in chat_id={chat_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Business Connection update
# ─────────────────────────────────────────────────────────────────────────────


def handle_business_connection(
    conn: sqlite3.Connection,
    connection_id: str,
    owner_user_id: int,
    status: str,
    can_reply: bool,
    can_read: bool,
) -> None:
    if status == "active":
        conn.execute(
            "INSERT OR REPLACE INTO business_connections "
            "(connection_id, owner_user_id, can_reply, can_read_messages, status) "
            "VALUES (?, ?, ?, ?, 'active')",
            (connection_id, owner_user_id, int(can_reply), int(can_read)),
        )
        log(f"business_connection: upserted active connection_id={connection_id}")
    else:
        conn.execute(
            "UPDATE business_connections SET status='revoked', revoked_at=datetime('now') "
            "WHERE connection_id=?",
            (connection_id,),
        )
        log(f"business_connection: revoked connection_id={connection_id}")
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНЫЙ ПАЙПЛАЙН
# ─────────────────────────────────────────────────────────────────────────────


def run_pipeline(args: argparse.Namespace) -> str | None:
    """
    Возвращает текст ответа (для вывода в stdout → Telegram) или None (молчать).
    """
    conn = get_db()
    try:
        return _run_pipeline_inner(conn, args)
    finally:
        conn.close()


def _run_pipeline_inner(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> str | None:
    event_type = args.event_type or "message"

    # ── Периодическая очистка state файла (1 из 100 вызовов) ──
    if random.random() < 0.01:
        cleanup_state()

    # ── Обработка deleted messages ──
    if event_type in ("deleted_messages", "deleted_business_messages"):
        handle_deleted(conn, args.chat_id, args.deleted_message_ids)
        return None

    # ── STEP 1: Normalize ──
    text = normalize_text(args)
    if not text:
        log("normalize: empty → skip")
        return None

    chash = content_hash(text)
    log(f"normalize: event={event_type} text={text[:100]!r} hash={chash}")

    # ── Обработка edited messages (обновить запись, не отвечать) ──
    if event_type in ("edited_message", "edited_business_message"):
        handle_edited(conn, args.chat_id, args.message_id, text, chash)
        return None

    is_owner = bool(args.is_owner)
    is_owner_chat = bool(args.is_owner_chat)

    # ── STEP 2: Fast skip — для owner-сообщений и аффирмаций ──
    do_fast_skip = not is_owner and is_fast_skip(text)

    # Resolve contact + thread нужен в любом случае (для хранения)
    contact_id = resolve_contact(
        conn, args.sender_id, args.sender_name, args.tg_username
    )
    chat_thread_id = resolve_chat_thread(
        conn, args.chat_id, contact_id, args.chat_type or "personal", args.chat_title
    )
    log(f"resolve: contact_id={contact_id} thread_id={chat_thread_id}")

    # ── STEP 2b: Dedup check ──
    if is_duplicate(conn, chat_thread_id, args.message_id):
        return None

    # ── STEP 4: Store message (всегда, даже при fast_skip) ──
    from_user = (
        str(args.sender_id)
        if args.sender_id is not None
        else ("self" if is_owner else None)
    )
    message_db_id = store_message(
        conn, chat_thread_id, from_user, text, args.message_id, chash, args.content_type
    )

    # Fast skip: только сохранить, без ingest/CRM/response
    if do_fast_skip:
        log("fast_skip: stored message, skipping ingest/response")
        conn.execute(
            "UPDATE messages SET analyzed=1, classification='chit-chat' WHERE id=?",
            (message_db_id,),
        )
        conn.commit()
        return None

    # ── STEP 5: Ingest ──
    ingest_result = run_ingest(text, contact_id, chat_thread_id, message_db_id)
    label: str = ingest_result.get("label", "other")
    confidence: float = float(ingest_result.get("confidence", 0.5))
    entities: dict = ingest_result.get("entities") or {}

    # Валидация label от LLM
    if label not in VALID_LABELS:
        log(f"ingest: invalid label={label!r} → normalizing to 'other'")
        label = "other"
        # LLM не смог классифицировать → понизить confidence чтобы trigger escalation
        confidence = min(confidence, 0.4)

    # Валидация confidence
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
        confidence = 0.5

    # Safety net: DEAL_CONFIRM_PHRASES, если LLM ошибочно классифицировал как silent,
    # всё равно эскалировать — это может быть подтверждение сделки
    normalized_text = re.sub(r"[\s.,!?;:]+$", "", text.lower().strip())
    if normalized_text in DEAL_CONFIRM_PHRASES and label in SILENT_LABELS:
        log(
            f"deal-confirm safety: '{normalized_text}' classified as {label!r} → override to decision"
        )
        label = "decision"
        confidence = 0.4  # force escalation path

    log(f"ingest: label={label} conf={confidence:.2f}")

    # ── STEP 6: CRM upsert ──
    crm_upsert(conn, label, entities, contact_id, message_db_id)

    # ── STEP 7: Response decision ──
    # Получить имя контакта для уведомления Камилю
    contact_row = conn.execute(
        "SELECT name FROM contacts WHERE id=?", (contact_id,)
    ).fetchone()
    contact_name = (
        contact_row["name"] if contact_row else (args.sender_name or "Клиент")
    )

    response_text = decide_response(
        conn=conn,
        label=label,
        confidence=confidence,
        contact_id=contact_id,
        chat_thread_id=chat_thread_id,
        message_id=message_db_id,
        chat_id=args.chat_id,
        is_owner=is_owner,
        is_owner_chat=is_owner_chat,
        contact_name=contact_name,
        original_text=text,
    )

    # ── STEP 8: Mark analyzed ──
    conn.execute(
        "UPDATE messages SET analyzed=1, classification=? WHERE id=?",
        (label, message_db_id),
    )
    conn.commit()
    log(
        f"analyzed: msg_id={message_db_id} label={label} response={'yes' if response_text else 'silent'}"
    )

    return response_text


# ─────────────────────────────────────────────────────────────────────────────
# Post-filter: защита от thinking leakage в stdout
# ─────────────────────────────────────────────────────────────────────────────

_THINKING_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"по\s+правилам",
        r"pipeline\s+сказал",
        r"подождите",
        r"клас{2}ифицировал",
        r"записал\s+в\s+памят",
        r"проверил\s+в\s+бд",
        r"по\s+моему\s+анализу",
        r"решение:\s*я",
        r"ответ\s+клиенту\s+с",
        r"lenochka[-_]response",
        r"SKILL\.md",
        r"AGENTS\.md",
        r"рассуждения",
        r"reasoning",
        r"монолог",
    ]
]


def _sanitize_output(text: str) -> str | None:
    """Убрать thinking leakage из текста перед отправкой в Telegram."""
    for pat in _THINKING_PATTERNS:
        if pat.search(text):
            log(f"sanitize: BLOCKED thinking leakage (pattern={pat.pattern!r})")
            return None
    stripped = text.strip()
    if not stripped:
        return None
    return stripped


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lenochka Pipeline — полный пайплайн обработки входящего сообщения"
    )
    p.add_argument("--text", default="", help="Текст сообщения")
    p.add_argument(
        "--sender_id", type=int, default=None, help="Telegram user ID отправителя"
    )
    p.add_argument("--sender_name", default=None, help="Имя отправителя")
    p.add_argument("--tg_username", default=None, help="@username отправителя")
    p.add_argument("--chat_id", required=True, help="Telegram chat ID")
    p.add_argument("--chat_title", default=None, help="Название чата (для групп)")
    p.add_argument(
        "--chat_type",
        default="personal",
        choices=["personal", "group", "supergroup", "channel"],
    )
    p.add_argument("--message_id", type=int, default=None, help="Telegram message ID")
    p.add_argument(
        "--is_owner", action="store_true", help="Сообщение от Камиля (owner'а)"
    )
    p.add_argument(
        "--is_owner_chat",
        action="store_true",
        help="Чат = owner пишет в свой же бизнес-аккаунт",
    )
    p.add_argument(
        "--business_connection_id", default=None, help="Telegram Business connection ID"
    )
    p.add_argument(
        "--event_type",
        default="message",
        choices=[
            "message",
            "edited_message",
            "business_message",
            "edited_business_message",
            "deleted_messages",
            "deleted_business_messages",
        ],
    )
    p.add_argument(
        "--deleted_message_ids",
        default=None,
        help="Через запятую: ID сообщений для soft-delete",
    )
    p.add_argument(
        "--content_type",
        default="text",
        choices=[
            "text",
            "sticker",
            "photo",
            "video",
            "voice",
            "document",
            "contact",
            "location",
            "video_note",
            "dice",
        ],
    )
    p.add_argument("--sticker_emoji", default=None, help="Эмодзи стикера")
    p.add_argument("--reply_to_text", default=None, help="Текст цитируемого сообщения")
    p.add_argument(
        "--reply_to_author", default=None, help="Автор цитируемого сообщения"
    )
    p.add_argument(
        "--forward_from", default=None, help="Источник пересланного сообщения"
    )
    # Business connection event (отдельный флоу)
    p.add_argument(
        "--bc_connection_id",
        default=None,
        help="[business_connection event] ID подключения",
    )
    p.add_argument(
        "--bc_owner_user_id",
        type=int,
        default=None,
        help="[business_connection event] Telegram user ID owner'а",
    )
    p.add_argument(
        "--bc_status",
        default=None,
        choices=["active", "revoked"],
        help="[business_connection event] Статус подключения",
    )
    p.add_argument(
        "--bc_can_reply",
        action="store_true",
        help="[business_connection event] can_reply=True",
    )
    p.add_argument(
        "--bc_can_read",
        action="store_true",
        help="[business_connection event] can_read_messages=True",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Business connection event (отдельный путь — не сообщение)
    if args.bc_connection_id and args.bc_status:
        conn = get_db()
        try:
            handle_business_connection(
                conn,
                connection_id=args.bc_connection_id,
                owner_user_id=args.bc_owner_user_id or 0,
                status=args.bc_status,
                can_reply=args.bc_can_reply,
                can_read=args.bc_can_read,
            )
        finally:
            conn.close()
        return

    result = run_pipeline(args)

    # Выходной контракт: вывод в stdout → текст в Telegram; нет вывода → SILENT
    if result:
        sanitized = _sanitize_output(result)
        if sanitized:
            print(sanitized)


if __name__ == "__main__":
    main()
