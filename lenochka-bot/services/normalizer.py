"""
Normalizer — превращает ЛЮБОЙ Telegram Message в текст для ingest.
Обрабатывает: text, caption, sticker, voice, photo, video, document,
contact, location, reply, forward.
"""
from dataclasses import dataclass, field
from aiogram.types import (
    Message, MessageOriginUser, MessageOriginChat,
    MessageOriginChannel, MessageOriginHiddenUser,
)


@dataclass
class NormalizedMessage:
    text: str
    content_type: str
    metadata: dict = field(default_factory=dict)
    reply_context: str | None = None
    forward_context: str | None = None

    @property
    def full_text(self) -> str:
        """Текст с контекстом reply/forward для ingest."""
        parts = []
        if self.forward_context:
            parts.append(self.forward_context)
        if self.reply_context:
            parts.append(self.reply_context)
        parts.append(self.text)
        return " ".join(parts)


# --- Emoji Intent Mapping ---
EMOJI_INTENT = {
    "👍": "confirm", "👌": "confirm", "🤝": "confirm", "👊": "confirm",
    "✅": "done", "☑️": "done", "🎉": "done",
    "❌": "cancel", "🚫": "cancel", "👎": "cancel",
    "🔥": "urgent", "⚡": "urgent", "⏰": "reminder",
    "💰": "payment", "💵": "payment", "💸": "payment",
    "❤️": "approve", "💯": "approve", "🙌": "approve",
    "📅": "schedule", "🗓️": "schedule",
    "😂": "laugh", "🤣": "laugh", "😅": "nervous",
    "🤔": "thinking", "🙏": "please", "😢": "sad",
}


def normalize_message(msg: Message) -> NormalizedMessage:
    """
    Главная функция: извлекает текст + контекст из любого Message.
    """
    # 1. Основной текст
    nm = _extract_content(msg)

    # 2. Reply context
    nm.reply_context = _resolve_reply(msg)

    # 3. Forward context
    nm.forward_context = _resolve_forward(msg)

    return nm


def _extract_content(msg: Message) -> NormalizedMessage:
    """Извлекает основной контент сообщения."""

    # Text
    if msg.text:
        return NormalizedMessage(text=msg.text, content_type="text")

    # Caption (photo, video, document, animation)
    if msg.caption:
        media = _detect_media(msg)
        return NormalizedMessage(
            text=msg.caption,
            content_type=f"{media}+caption",
            metadata={"has_media": True},
        )

    # Sticker
    if msg.sticker:
        emoji = msg.sticker.emoji or "❓"
        intent = EMOJI_INTENT.get(emoji, "unknown")
        return NormalizedMessage(
            text=f"[sticker: {emoji} → {intent}]",
            content_type="sticker",
            metadata={"emoji": emoji, "intent": intent},
        )

    # Contact
    if msg.contact:
        c = msg.contact
        name = f"{c.first_name} {c.last_name or ''}".strip()
        return NormalizedMessage(
            text=f"[contact: {name} phone:{c.phone_number}]",
            content_type="contact",
            metadata={"name": name, "phone": c.phone_number},
        )

    # Location
    if msg.location:
        loc = msg.location
        return NormalizedMessage(
            text=f"[location: {loc.latitude},{loc.longitude}]",
            content_type="location",
            metadata={"lat": loc.latitude, "lon": loc.longitude},
        )

    # Voice
    if msg.voice:
        return NormalizedMessage(
            text=f"[voice: {msg.voice.duration}s]",
            content_type="voice",
            metadata={"duration": msg.voice.duration, "file_id": msg.voice.file_id},
        )

    # Document
    if msg.document:
        fn = msg.document.file_name or "unnamed"
        mime = msg.document.mime_type or ""
        return NormalizedMessage(
            text=f"[document: {fn}]",
            content_type="document",
            metadata={"file_name": fn, "mime": mime},
        )

    # Photo (without caption)
    if msg.photo:
        return NormalizedMessage(text="[photo]", content_type="photo")

    # Video
    if msg.video:
        return NormalizedMessage(
            text=f"[video: {msg.video.duration}s]",
            content_type="video",
            metadata={"duration": msg.video.duration},
        )

    # Video note (circle)
    if msg.video_note:
        return NormalizedMessage(
            text=f"[video note: {msg.video_note.duration}s]",
            content_type="video_note",
            metadata={"duration": msg.video_note.duration},
        )

    # Dice
    if msg.dice:
        return NormalizedMessage(
            text=f"[dice: {msg.dice.emoji}={msg.dice.value}]",
            content_type="dice",
        )

    # Poll
    if msg.poll:
        p = msg.poll
        options = ", ".join(o.text for o in p.options)
        return NormalizedMessage(
            text=f"[poll: {p.question}] options: {options}",
            content_type="poll",
        )

    return NormalizedMessage(text="[unsupported message type]", content_type="unknown")


def _detect_media(msg: Message) -> str:
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.animation:
        return "animation"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    return "media"


def _resolve_reply(msg: Message) -> str | None:
    """
    Если сообщение — ответ, вернуть контекст оригинала.
    Критично: 'да' на '150к?' = decision. 'да' на 'как дела?' = chit-chat.
    """
    if not msg.reply_to_message:
        return None

    original = msg.reply_to_message
    orig_nm = _extract_content(original)
    orig_text = orig_nm.text

    if len(orig_text) > 150:
        orig_text = orig_text[:147] + "..."

    author = "Я" if _is_self(original) else (
        original.from_user.first_name if original.from_user else "Unknown"
    )
    return f'[reply to {author}: "{orig_text}"]'


def _resolve_forward(msg: Message) -> str | None:
    """Если сообщение переслано — определить исходного автора."""
    if not msg.forward_origin:
        return None

    origin = msg.forward_origin
    if isinstance(origin, MessageOriginUser):
        return f"[forwarded from: {origin.sender_user.first_name}]"
    elif isinstance(origin, MessageOriginChat):
        return f"[forwarded from chat: {origin.sender_chat.title or 'chat'}]"
    elif isinstance(origin, MessageOriginChannel):
        return f"[forwarded from channel: {origin.chat.title or 'channel'}]"
    elif isinstance(origin, MessageOriginHiddenUser):
        return "[forwarded from: hidden user]"
    return "[forwarded]"


def _is_self(msg: Message) -> bool:
    """Проверить, отправлено ли сообщение самим пользователем (owner)."""
    if msg.from_user and msg.from_user.is_bot:
        return True
    if msg.sender_business_bot:
        return True
    return False
