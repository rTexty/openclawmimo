#!/usr/bin/env python3
"""
Lenochka Pipeline v2 — Упрощённый пайплайн обработки входящего сообщения.

Выходной контракт:
  - Нет вывода  → SILENT (ничего не отправлять в Telegram)
  - Текст в stdout → отправить этот текст в Telegram

Использование:
  python3 run_pipeline.py \\
    --text "текст" --sender_id 123 --sender_name "Иван" \\
    --chat_id "-100123" --message_id 42 \\
    [--is_owner] [--is_owner_chat] [--business_connection_id "abc"] \\
    [--event_type business_message] [--content_type sticker] [--sticker_emoji "👍"] \\
    [--reply_to_text "цитата"] [--reply_to_author "Имя"]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Пути ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_DIR = PROJECT_ROOT / "lenochka-memory"
CRM_SCRIPT = PROJECT_ROOT / "skills" / "lenochka-crm" / "run_crm.py"
sys.path.insert(0, str(MEMORY_DIR))

try:
    import mem
    import brain
except ImportError as e:
    print(f"[pipeline] Import error: {e}", file=sys.stderr)
    sys.exit(1)

# ── Константы ─────────────────────────────────────────────────────────────────
OWNER_ID = "5944980799"
TZ_OWNER = timezone(timedelta(hours=8))
BOT_USERNAME = "lenochkab2b_bot"
LOAD_DIR_BASE = Path("/tmp/lenochka_load")

SILENT_PHRASES = frozenset(
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
DEAL_CONFIRM_PHRASES = frozenset(
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
SILENT_EMOJIS = frozenset({"👍", "🤝", "✅", "👌", "💯", "👊", "☑️"})
VALID_LABELS = frozenset(
    {"noise", "chit-chat", "business-small", "task", "decision", "lead-signal", "risk"}
)
SILENT_LABELS = frozenset({"noise", "chit-chat", "business-small"})

ESCALATE_TEMPLATES = {
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
ESCALATE_NIGHT_TEMPLATES = {
    "lead-signal": [
        "Спасибо за сообщение! Ответим утром, как только начнётся рабочий день.",
        "Принято! Сейчас нерабочее время — свяжемся утром.",
    ],
    "risk": ["Получила ваш вопрос. Срочно — передала, ответит в ближайшее время."],
    "task": ["Принято! Сейчас ночь — разберёмся утром и вернёмся к вам."],
    "default": [
        "Спасибо! Сейчас нерабочее время — ответим утром.",
        "Принято, ответим с утра!",
    ],
}

EMOJI_INTENT = {
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

# ── Утилиты ───────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", file=sys.stderr)


def get_db():
    conn = (
        mem.get_db()
        if hasattr(mem, "get_db")
        else __import__("sqlite3").connect(str(mem.DB_PATH), timeout=10)
    )
    conn.row_factory = __import__("sqlite3").Row
    return conn


def is_night() -> bool:
    h = datetime.now(TZ_OWNER).hour
    return h >= 23 or h < 8


def _run_crm(*args: str) -> str:
    """Вызвать run_crm.py и вернуть stdout."""
    try:
        result = subprocess.run(
            [sys.executable, str(CRM_SCRIPT)] + list(args),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()
    except Exception as e:
        log(f"crm call error: {e}")
        return ""


# ── Шаг 1: NORMALIZE ─────────────────────────────────────────────────────────


def normalize(args: argparse.Namespace) -> str:
    text = (args.text or "").strip()
    ct = args.content_type or "text"

    if ct == "sticker":
        emoji = args.sticker_emoji or ""
        intent = EMOJI_INTENT.get(emoji, "sticker")
        text = f"[sticker: {emoji} = {intent}]"
    elif ct in (
        "voice",
        "photo",
        "video",
        "video_note",
        "document",
        "contact",
        "location",
        "dice",
    ):
        prefix = f"[{ct.replace('_', ' ')}]"
        text = f"{prefix}: {text}" if text else prefix

    if args.reply_to_text:
        text = f'[Reply to {args.reply_to_author or "?"}: "{args.reply_to_text[:150]}"] {text}'
    if args.forward_from:
        text = f"[Forwarded from {args.forward_from}] {text}"

    return text.strip()


# ── Шаг 2: ROUTING GATE ──────────────────────────────────────────────────────


def should_process(args: argparse.Namespace, text: str) -> str:
    """
    Возвращает: "process" | "silent_ingest" | "silent"
    Полная таблица — в ROUTING_GATE.md
    """
    if getattr(args, "sender_business_bot", None):
        return "silent"

    is_owner = bool(args.is_owner)
    is_owner_chat = bool(args.is_owner_chat)
    event_type = args.event_type or "message"
    has_bc = bool(getattr(args, "business_connection_id", None))
    chat_type = args.chat_type or "personal"

    if is_owner:
        if event_type in ("message", "direct_message"):
            return "process"
        if is_owner_chat:
            return "silent_ingest"
        return "process"

    if has_bc:
        return "process"

    if chat_type in ("group", "supergroup"):
        if re.search(rf"@{re.escape(BOT_USERNAME)}\b", text, re.IGNORECASE):
            return "process"
        return "silent_ingest"

    if chat_type == "channel":
        return "silent"

    return "process"


# ── Шаг 3: FAST SKIP ─────────────────────────────────────────────────────────


def is_fast_skip(text: str) -> bool:
    if not text:
        return True
    normalized = re.sub(r"[\s.,!?;:]+$", "", text.lower().strip())
    if normalized in SILENT_PHRASES:
        return True
    if normalized in DEAL_CONFIRM_PHRASES:
        return False
    if text.strip() in SILENT_EMOJIS:
        return True
    return False


# ── Шаг 4: INGEST (classify + memory) ────────────────────────────────────────


def ingest(text: str, contact_id: int, chat_thread_id: int, message_id: int) -> dict:
    """Вызвать mem.ingest и вернуть {label, confidence, entities}."""
    try:
        buf = __import__("io").StringIO()
        with __import__("contextlib").redirect_stdout(buf):
            result = mem.ingest(
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

    try:
        with __import__("contextlib").redirect_stdout(__import__("io").StringIO()):
            label, conf, _ = brain.classify_message(text)
    except Exception:
        label, conf = "other", 0.5
    return {"label": label, "confidence": conf, "entities": {}}


# ── Шаг 5: RESPOND ───────────────────────────────────────────────────────────


def _pick_template(label: str, night: bool = False) -> str:
    import random

    pool = ESCALATE_NIGHT_TEMPLATES if night else ESCALATE_TEMPLATES
    templates = pool.get(label) or pool.get("default") or ["Передала менеджеру."]
    return random.choice(templates)


def _try_fact_response(conn, contact_id: int, text: str) -> str | None:
    """Попытаться ответить фактом из БД."""
    lower = text.lower()

    if any(w in lower for w in ("сделк", "заказ", "договорил", "сколько")):
        row = conn.execute(
            "SELECT stage, amount FROM deals WHERE contact_id=? "
            "AND stage NOT IN ('closed_won','closed_lost') ORDER BY updated_at DESC LIMIT 1",
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

    if any(w in lower for w in ("договор", "контракт")):
        row = conn.execute(
            "SELECT summary, status, due_at FROM agreements WHERE contact_id=? "
            "AND status NOT IN ('completed','cancelled') ORDER BY created_at DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        if row:
            status_ru = {
                "draft": "черновик",
                "sent": "отправлен",
                "signed": "подписан",
            }.get(row["status"], row["status"])
            parts = [
                f"Договор: {row['summary'] or 'без описания'}",
                f"статус — {status_ru}",
            ]
            if row["due_at"]:
                parts.append(f"срок — {row['due_at'][:10]}")
            return ". ".join(parts) + "."

    return None


def decide_response(
    conn,
    label: str,
    confidence: float,
    contact_id: int,
    chat_thread_id: int,
    message_id: int,
    chat_id: str,
    is_owner: bool,
    contact_name: str,
    text: str,
    event_type: str = "message",
) -> str | None:
    """Решить: ответить фактом, эскалировать, или молчать."""
    lower = text.strip().lower()

    if is_owner and lower == "/load":
        return handle_load_command(conn, chat_id, int(OWNER_ID))

    # Owner в bot_dm → агент отвечает естественно через LLM
    is_bot_dm = is_owner and event_type in ("message", "direct_message")
    if is_bot_dm:
        # Сохраняем сообщение в БД, но не решаем за LLM
        return "[NATURAL_RESPONSE]"

    if label in SILENT_LABELS:
        return None

    is_complaint = label == "risk"
    night = is_night()

    should_escalate = (
        label in ("lead-signal", "risk", "other")
        or (label == "task" and confidence < 0.85)
        or (label == "decision" and confidence < 0.5)
    )
    if should_escalate:
        spam_result = _run_crm("anti_spam_check", "--chat_id", chat_id)
        if spam_result == "blocked":
            log("response: anti-spam blocked escalation")
            return None

        _run_crm(
            "escalation",
            "--chat_thread_id",
            str(chat_thread_id),
            "--contact_id",
            str(contact_id),
            "--message_id",
            str(message_id),
            "--label",
            label,
            *(["--is_complaint"] if is_complaint else []),
            "--contact_name",
            contact_name,
            "--chat_id",
            chat_id,
            "--message_text",
            text[:300],
        )

        _run_crm("anti_spam_record", "--chat_id", chat_id)

        reply = _pick_template(label, night=night and not is_complaint)
        log(f"response: escalate + reply label={label}")
        return reply

    if label == "decision":
        fact = _try_fact_response(conn, contact_id, text)
        if fact:
            spam_result = _run_crm("anti_spam_check", "--chat_id", chat_id)
            if spam_result != "blocked":
                _run_crm("anti_spam_record", "--chat_id", chat_id)
                return fact
        return None

    return None


# ── Load session handlers ────────────────────────────────────────────────────


def handle_load_command(conn, chat_id: str, sender_id: int) -> str | None:
    """Обработать команду /load — создать load_session."""
    if str(sender_id) != OWNER_ID:
        return None

    conn.execute(
        "INSERT INTO load_sessions (chat_id, owner_id, status) VALUES (?, ?, 'waiting_files')",
        (chat_id, str(sender_id)),
    )
    conn.commit()
    return "📎 Жду файлы. Загрузи JSON или HTML экспорт Telegram."


def handle_load_file(
    conn, chat_id: str, sender_id: int, filename: str, source_path: str | None = None
) -> str | None:
    """Обработать загрузку файла во время load-сессии."""
    if str(sender_id) != OWNER_ID:
        return None

    session = conn.execute(
        "SELECT id, status FROM load_sessions "
        "WHERE chat_id=? AND owner_id=? AND status IN ('waiting_files', 'ready_to_process') "
        "ORDER BY created_at DESC LIMIT 1",
        (chat_id, str(sender_id)),
    ).fetchone()

    if not session:
        return None

    files_dir = LOAD_DIR_BASE / chat_id
    files_dir.mkdir(parents=True, exist_ok=True)

    # Copy downloaded file from OpenClaw temp to load directory
    if source_path and Path(source_path).exists():
        dest = files_dir / filename
        import shutil

        shutil.copy2(source_path, dest)
        log(f"load: copied {source_path} → {dest}")

    conn.execute(
        "UPDATE load_sessions SET status='ready_to_process', files_path=?, updated_at=datetime('now') WHERE id=?",
        (str(files_dir), session["id"]),
    )
    conn.commit()
    return f"📎 Получен: {filename}. Загрузи ещё или нажми Готово."


def handle_load_ready(conn, chat_id: str, sender_id: int) -> str | None:
    """Обработать команду 'Готово' — запустить импорт."""
    if str(sender_id) != OWNER_ID:
        return None

    session = conn.execute(
        "SELECT id, files_path FROM load_sessions "
        "WHERE chat_id=? AND owner_id=? AND status='ready_to_process' "
        "ORDER BY created_at DESC LIMIT 1",
        (chat_id, str(sender_id)),
    ).fetchone()

    if not session:
        return "Нет файлов для обработки. Напиши /load чтобы начать."

    files_dir = session["files_path"]
    if not files_dir or not Path(files_dir).exists():
        return "❌ Файлы не найдены."

    conn.execute(
        "UPDATE load_sessions SET status='processing', updated_at=datetime('now') WHERE id=?",
        (session["id"],),
    )
    conn.commit()

    load_script = PROJECT_ROOT / "skills" / "lenochka-load" / "run_load.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(load_script),
                "--dir",
                files_dir,
                "--chat_id",
                chat_id,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout.strip()
        try:
            stats = json.loads(output)
        except json.JSONDecodeError:
            stats = {"status": "error", "error": "Invalid output from import"}

        if stats.get("status") == "ok":
            msg = (
                f"✅ Загружено {stats['total_messages']} сообщений "
                f"от {stats['contacts_created']} контактов, "
                f"создано {stats['memories_created']} memories"
            )
            if stats.get("errors"):
                msg += f"\n⚠️ Ошибки: {', '.join(stats['errors'][:3])}"
            conn.execute(
                "UPDATE load_sessions SET status='done', messages_count=?, updated_at=datetime('now') WHERE id=?",
                (stats["total_messages"], session["id"]),
            )
            conn.commit()
            return msg
        else:
            conn.execute(
                "UPDATE load_sessions SET status='error', updated_at=datetime('now') WHERE id=?",
                (session["id"],),
            )
            conn.commit()
            return f"❌ Ошибка импорта: {stats.get('error', 'unknown')}"
    except subprocess.TimeoutExpired:
        conn.execute(
            "UPDATE load_sessions SET status='error', updated_at=datetime('now') WHERE id=?",
            (session["id"],),
        )
        conn.commit()
        return "❌ Импорт не успел за 5 минут. Попробуй разбить файл."
    except Exception as e:
        conn.execute(
            "UPDATE load_sessions SET status='error', updated_at=datetime('now') WHERE id=?",
            (session["id"],),
        )
        conn.commit()
        return f"❌ Ошибка: {e}"


_DUAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"камиль[,\s]",
        r"камилю",
        r"я\s+(е[ё]е|ей)\s+(успокоила|подтвердила|сказала|ответила)",
        r"завтра\s+с\s+утра\s+нужно",
        r"нужно\s+(будет|вам)\s+(дать|связаться|написать)",
    ]
]


def sanitize(text: str) -> str | None:
    """Убрать dual response и thinking leakage."""
    for pat in _DUAL_PATTERNS:
        if pat.search(text):
            log(f"sanitize: BLOCKED dual response (pattern={pat.pattern!r})")
            parts = re.split(r"\n{2,}", text, maxsplit=1)
            if len(parts) > 1:
                first = parts[0].strip()
                if first and not any(p.search(first) for p in _DUAL_PATTERNS):
                    return first
            return None
    return text.strip() or None


# ── Resolve contact + thread ─────────────────────────────────────────────────


def resolve_contact(conn, sender_id, sender_name, username):
    existing = None
    if sender_id is not None:
        existing = conn.execute(
            "SELECT id, name FROM contacts WHERE tg_user_id=?", (str(sender_id),)
        ).fetchone()
    if not existing and username:
        existing = conn.execute(
            "SELECT id, name FROM contacts WHERE tg_username=?", (username.lstrip("@"),)
        ).fetchone()

    if existing:
        cid = existing["id"]
        if sender_name and sender_name != existing["name"]:
            conn.execute(
                "UPDATE contacts SET name=?, updated_at=datetime('now') WHERE id=?",
                (sender_name, cid),
            )
            conn.commit()
        return cid

    name = sender_name or (
        f"@{username.lstrip('@')}" if username else f"tg_{sender_id}"
    )
    conn.execute(
        "INSERT INTO contacts (name, tg_username, tg_user_id) VALUES (?, ?, ?)",
        (
            name,
            username.lstrip("@") if username else None,
            str(sender_id) if sender_id else None,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def resolve_chat_thread(conn, chat_id, contact_id, chat_type, title):
    normalized = (
        chat_id.replace("telegram:", "") if chat_id.startswith("telegram:") else chat_id
    )
    existing = conn.execute(
        "SELECT id FROM chat_threads WHERE tg_chat_id=?", (normalized,)
    ).fetchone()
    if existing:
        return existing["id"]
    conn.execute(
        "INSERT INTO chat_threads (tg_chat_id, contact_id, type, title) VALUES (?, ?, ?, ?)",
        (normalized, contact_id, chat_type, title),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Store message ────────────────────────────────────────────────────────────


def store_message(
    conn, chat_thread_id, from_user, text, source_msg_id, chash, content_type
):
    meta = (
        json.dumps({"content_type": content_type}, ensure_ascii=False)
        if content_type
        else None
    )
    conn.execute(
        """INSERT INTO messages (chat_thread_id, from_user_id, text, sent_at,
           source_msg_id, content_hash, meta_json, analyzed)
           VALUES (?, ?, ?, datetime('now'), ?, ?, ?, 0)""",
        (chat_thread_id, from_user, text[:4096], source_msg_id, chash, meta),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Dedup ────────────────────────────────────────────────────────────────────


def is_duplicate(conn, chat_thread_id, source_msg_id):
    if source_msg_id is None:
        return False
    row = conn.execute(
        "SELECT id FROM messages WHERE chat_thread_id=? AND source_msg_id=?",
        (chat_thread_id, source_msg_id),
    ).fetchone()
    if row:
        log(f"dedup: source_msg_id={source_msg_id} exists")
        return True
    return False


# ── ГЛАВНЫЙ ПАЙПЛАЙН ─────────────────────────────────────────────────────────


def run_pipeline(args) -> str | None:
    event_type = args.event_type or "message"

    if event_type in ("deleted_messages", "deleted_business_messages"):
        if args.deleted_message_ids:
            conn = get_db()
            try:
                ids = [
                    int(x.strip())
                    for x in args.deleted_message_ids.split(",")
                    if x.strip().isdigit()
                ]
                for mid in ids:
                    conn.execute(
                        "UPDATE messages SET meta_json=json_set(COALESCE(meta_json,'{}'),'$.deleted',1) "
                        "WHERE source_msg_id=? AND chat_thread_id IN (SELECT id FROM chat_threads WHERE tg_chat_id=?)",
                        (mid, args.chat_id),
                    )
                conn.commit()
            finally:
                conn.close()
        return None

    text = normalize(args)
    if not text:
        return None

    chash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    log(f"normalize: event={event_type} text={text[:100]!r}")

    if event_type in ("edited_message", "edited_business_message"):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT m.id FROM messages m JOIN chat_threads ct ON m.chat_thread_id=ct.id "
                "WHERE ct.tg_chat_id=? AND m.source_msg_id=?",
                (args.chat_id, args.message_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE messages SET text=?, content_hash=?, analyzed=0 WHERE id=?",
                    (text[:4096], chash, row["id"]),
                )
                conn.commit()
        finally:
            conn.close()
        return None

    is_owner = bool(args.is_owner)
    is_owner_chat = bool(args.is_owner_chat)
    lower_text = text.strip().lower()

    action = should_process(args, text)
    log(
        f"routing: action={action} is_owner={is_owner} is_owner_chat={is_owner_chat} bc={getattr(args, 'business_connection_id', None)!r}"
    )

    if action == "silent":
        return None

    # ── Load session: "Готово" ──
    if is_owner and lower_text == "готово":
        conn = get_db()
        try:
            return handle_load_ready(conn, args.chat_id, args.sender_id)
        finally:
            conn.close()

    conn = get_db()
    try:
        # ── Load session: file upload ──
        if is_owner and args.content_type in ("document", "photo"):
            session = conn.execute(
                "SELECT id, status FROM load_sessions "
                "WHERE chat_id=? AND owner_id=? AND status IN ('waiting_files', 'ready_to_process') "
                "ORDER BY created_at DESC LIMIT 1",
                (args.chat_id, str(args.sender_id)),
            ).fetchone()
            if session:
                filename = args.text[:100] if args.text else "file"
                return handle_load_file(
                    conn, args.chat_id, args.sender_id, filename, args.file_path
                )

        contact_id = resolve_contact(
            conn, args.sender_id, args.sender_name, args.tg_username
        )
        chat_thread_id = resolve_chat_thread(
            conn,
            args.chat_id,
            contact_id,
            args.chat_type or "personal",
            args.chat_title,
        )

        if is_duplicate(conn, chat_thread_id, args.message_id):
            return None

        from_user = (
            str(args.sender_id) if args.sender_id else ("self" if is_owner else None)
        )
        msg_id = store_message(
            conn,
            chat_thread_id,
            from_user,
            text,
            args.message_id,
            chash,
            args.content_type,
        )

        if action == "silent_ingest":
            conn.execute(
                "UPDATE messages SET analyzed=1, classification='owner_message' WHERE id=?",
                (msg_id,),
            )
            conn.commit()
            return None

        if not is_owner and is_fast_skip(text):
            conn.execute(
                "UPDATE messages SET analyzed=1, classification='chit-chat' WHERE id=?",
                (msg_id,),
            )
            conn.commit()
            return None

        result = ingest(text, contact_id, chat_thread_id, msg_id)
        label = result.get("label", "other")
        confidence = float(result.get("confidence", 0.5))
        entities = result.get("entities") or {}

        if label not in VALID_LABELS:
            label = "other"
            confidence = min(confidence, 0.4)

        normalized_text = re.sub(r"[\s.,!?;:]+$", "", text.lower().strip())
        if normalized_text in DEAL_CONFIRM_PHRASES and label in SILENT_LABELS:
            label = "decision"
            confidence = 0.4

        log(f"ingest: label={label} conf={confidence:.2f}")

        if label in ("task", "decision", "lead-signal", "risk") and entities:
            amounts = [
                float(a)
                for a in entities.get("amounts", [])
                if isinstance(a, (int, float))
            ]
            if amounts and label in ("decision", "lead-signal"):
                stage = "closed_won" if label == "decision" else "discovery"
                _run_crm(
                    "deal",
                    "--contact_id",
                    str(contact_id),
                    "--amount",
                    str(max(amounts)),
                    "--stage",
                    stage,
                )

            tasks = entities.get("tasks") or []
            if isinstance(tasks, list):
                for t in tasks[:3]:
                    if isinstance(t, dict) and t.get("description"):
                        _run_crm(
                            "task",
                            "--contact_id",
                            str(contact_id),
                            "--description",
                            t["description"],
                            "--due_date",
                            t.get("due_date", ""),
                            "--priority",
                            t.get("priority", "normal"),
                        )

            if label == "lead-signal":
                _run_crm(
                    "lead",
                    "--contact_id",
                    str(contact_id),
                    "--source",
                    "telegram",
                    "--amount",
                    str(max(amounts)) if amounts else "",
                )

        contact_row = conn.execute(
            "SELECT name FROM contacts WHERE id=?", (contact_id,)
        ).fetchone()
        contact_name = (
            contact_row["name"] if contact_row else (args.sender_name or "Клиент")
        )

        response = decide_response(
            conn,
            label,
            confidence,
            contact_id,
            chat_thread_id,
            msg_id,
            args.chat_id,
            is_owner,
            contact_name,
            text,
            event_type,
        )

        conn.execute(
            "UPDATE messages SET analyzed=1, classification=? WHERE id=?",
            (label, msg_id),
        )
        conn.commit()

        chat_type = args.chat_type or "personal"
        if chat_type in ("group", "supergroup"):
            if not re.search(rf"@{re.escape(BOT_USERNAME)}\b", text, re.IGNORECASE):
                log("mention_gate: group without @mention → suppress")
                return None

        log(
            f"analyzed: msg_id={msg_id} label={label} response={'yes' if response else 'silent'}"
        )
        return sanitize(response) if response else None

    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser():
    p = argparse.ArgumentParser(description="Lenochka Pipeline v2")
    p.add_argument("--text", default="")
    p.add_argument("--sender_id", type=int, default=None)
    p.add_argument("--sender_name", default=None)
    p.add_argument("--tg_username", default=None)
    p.add_argument("--chat_id", required=True)
    p.add_argument("--chat_title", default=None)
    p.add_argument(
        "--chat_type",
        default="personal",
        choices=["personal", "group", "supergroup", "channel"],
    )
    p.add_argument("--message_id", type=int, default=None)
    p.add_argument("--is_owner", action="store_true")
    p.add_argument("--is_owner_chat", action="store_true")
    p.add_argument("--business_connection_id", default=None)
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
    p.add_argument("--deleted_message_ids", default=None)
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
    p.add_argument("--sticker_emoji", default=None)
    p.add_argument("--reply_to_text", default=None)
    p.add_argument("--reply_to_author", default=None)
    p.add_argument("--forward_from", default=None)
    p.add_argument("--file_path", default=None, help="Local path to downloaded file")
    return p


def main():
    args = build_parser().parse_args()
    result = run_pipeline(args)
    if result:
        print(result)


if __name__ == "__main__":
    main()
