"""
Notifier — escalation engine, timer persistence, startup recovery.

Обрабатывает:
1. Escalation: клиенту нужен ответ owner'а → 30 мин таймер → notify
2. Startup recovery: pending notifications при рестарте бота
3. Pending notification management (save/cancel/mark)
"""
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from services.brain_wrapper import get_db
from services.response_engine import (
    format_escalation_notification, format_duration,
)

logger = logging.getLogger("lenochka.notifier")

GMT8 = timezone(timedelta(hours=8))

# =========================================================
# ESCALATION DELAYS (по типу)
# =========================================================

ESCALATION_DELAY = {
    "complaint":  600,    # 10 мин — жалобы быстрее
    "pricing":    1800,   # 30 мин
    "proposal":   1800,
    "contract":   1800,
    "meeting":    1800,
    "other":      1800,
}


# =========================================================
# 1. HANDLE ESCALATION — точка входа из pipeline
# =========================================================

async def handle_escalation(bot, item, decision: dict, db_path: str):
    """
    Escalation: ставим таймер → проверяем через N минут → notify owner'у.
    """
    escalation_type = decision.get("escalation_type", decision.get("intent", "other"))
    delay = ESCALATION_DELAY.get(escalation_type, 1800)

    # Night mode: 23-08 и НЕ complaint → schedule на 08:00
    hour = datetime.now(GMT8).hour
    is_night = 23 <= hour or hour < 8
    is_urgent = escalation_type == "complaint"

    if is_night and not is_urgent:
        delay = _seconds_until(8, 0)
        logger.info(f"Night mode: escalation delayed until 08:00 ({delay:.0f}s)")

    # Save pending notification
    notify_at = datetime.now(GMT8) + timedelta(seconds=delay)
    notif_id = _save_pending(
        chat_thread_id=item.chat_thread_id,
        contact_id=item.contact_id,
        message_id=item.message_id,
        message_text=item.normalized.text[:500] if item.normalized else "",
        entity_type="escalation",
        entity_id=item.message_id,
        escalation_type=escalation_type,
        notify_at=notify_at,
        db_path=db_path,
    )

    # Schedule async check
    asyncio.create_task(
        _check_and_notify_later(bot, notif_id, item, decision, delay, db_path)
    )

    logger.info(
        f"Escalation scheduled: type={escalation_type}, delay={delay:.0f}s, "
        f"notif_id={notif_id}"
    )


async def _check_and_notify_later(bot, notif_id: int, item, decision: dict,
                                    delay: float, db_path: str):
    """Ждём delay секунд, проверяем статус, агрегируем и отправляем owner'у."""
    await asyncio.sleep(delay)

    # Проверяем что не отменили
    row = _get_notification(notif_id, db_path)
    if not row or row["status"] != "pending":
        return

    # Проверяем: owner уже ответил в этом чате? → отмена
    from services.dialog_state import get_dialog_state
    state = get_dialog_state(item.chat_thread_id, item.message_id, db_path)
    if state["owner_replied_after"]:
        _cancel(notif_id, db_path)
        logger.info(f"Escalation cancelled (owner replied): notif_id={notif_id}")
        return

    # Агрегируем ВСЕ pending для этого чата и отправляем ОДНО сводное сообщение
    if item.chat_thread_id:
        await _aggregate_and_send(bot, item.chat_thread_id, db_path)
    else:
        # Нет chat_thread — отправляем индивидуально
        await _send_single_notification(bot, row, db_path)


async def _send_single_notification(bot, row: dict, db_path: str):
    """Отправить одно уведомление owner'у."""
    contact_name = _get_contact_name(row.get("contact_id"), db_path)
    escalation_type = row.get("escalation_type", "other")
    context_block = _build_context_block(
        row.get("chat_thread_id"), row.get("contact_id"), db_path
    )

    text = format_escalation_notification(
        escalation_type=escalation_type,
        contact_name=contact_name,
        message_text=row.get("message_text", ""),
        wait_time="(таймер истёк)",
        context_block=context_block,
    )

    try:
        from config import settings
        await bot.send_message(
            chat_id=settings.owner_id,
            text=text,
            parse_mode="HTML",
        )
        _mark_sent(row["id"], db_path)
        logger.info(f"Single notification sent: id={row['id']}, type={escalation_type}")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


async def _aggregate_and_send(bot, chat_thread_id: int, db_path: str):
    """
    Агрегировать ВСЕ pending notifications для одного чата
    и отправить owner'у ОДНО сводное сообщение.
    """
    conn = get_db(db_path)
    try:
        pending = conn.execute("""
            SELECT * FROM pending_notifications
            WHERE chat_thread_id = ? AND status = 'pending'
              AND notify_at <= datetime('now')
            ORDER BY created_at ASC
            LIMIT 20
        """, (chat_thread_id,)).fetchall()
    finally:
        conn.close()

    if not pending:
        return

    notif_ids = [p["id"] for p in pending]

    if len(pending) == 1:
        # Одно уведомление — отправляем как обычно
        await _send_single_notification(bot, pending[0], db_path)
        return

    # Несколько уведомлений — агрегируем в одно сообщение
    contact_name = _get_contact_name(pending[0].get("contact_id"), db_path)

    lines = []
    for p in pending:
        etype = p.get("escalation_type", "other")
        text_short = (p.get("message_text") or "")[:80]
        icon = {"pricing": "💰", "proposal": "📄", "contract": "📝",
                "meeting": "📅", "complaint": "⚠️", "other": "❓"}.get(etype, "❓")
        lines.append(f"{icon} «{text_short}»")

    agg_text = (
        f"📬 <b>{contact_name}</b>: {len(pending)} сообщений ждут ответа\n\n"
        + "\n".join(lines)
        + "\n\n💡 Напишите клиенту напрямую."
    )

    try:
        from config import settings
        await bot.send_message(
            chat_id=settings.owner_id,
            text=agg_text,
            parse_mode="HTML",
        )
        for nid in notif_ids:
            _mark_sent(nid, db_path)
        logger.info(f"Aggregated {len(notif_ids)} notifications for chat {chat_thread_id}")
    except Exception as e:
        logger.error(f"Aggregate notification failed: {e}")


# =========================================================
# 2. STARTUP RECOVERY
# =========================================================

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
        try:
            notify_at = datetime.fromisoformat(p["notify_at"])
        except (ValueError, TypeError):
            # Может быть без timezone
            try:
                notify_at = datetime.strptime(p["notify_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

        remaining = (notify_at - now).total_seconds()

        if remaining <= 0:
            # Просроченное — отправить сразу
            await _send_pending_notification(bot, p, db_path)
            recovered += 1
        else:
            # Будущее — запланировать
            asyncio.create_task(
                _wait_and_send(bot, p, remaining, db_path)
            )
            recovered += 1

    if recovered:
        logger.info(f"Startup recovery: {recovered} pending notifications restored")


async def _send_pending_notification(bot, row: dict, db_path: str):
    """Отправить pending notification owner'у."""
    text = _format_pending_as_owner_message(row)
    if not text:
        _cancel(row["id"], db_path)
        return

    try:
        from config import settings
        await bot.send_message(
            chat_id=settings.owner_id,
            text=text,
            parse_mode="HTML",
        )
        _mark_sent(row["id"], db_path)
        logger.info(f"Recovered notification sent: id={row['id']}")
    except Exception as e:
        logger.error(f"Failed to send recovered notification: {e}")


async def _wait_and_send(bot, row: dict, delay: float, db_path: str):
    """Ждём delay, проверяем что не отменили, отправляем."""
    await asyncio.sleep(delay)

    current = _get_notification(row["id"], db_path)
    if current and current["status"] == "pending":
        await _send_pending_notification(bot, current, db_path)


def _format_pending_as_owner_message(row: dict) -> str | None:
    """Форматировать pending notification как сообщение owner'у."""
    entity_type = row.get("entity_type", "")
    escalation_type = row.get("escalation_type", "")
    message_text = row.get("message_text", "")

    if entity_type == "escalation":
        return format_escalation_notification(
            escalation_type=escalation_type,
            contact_name=_get_contact_name_sync(row.get("contact_id")),
            message_text=message_text,
            wait_time="(восстановлено после рестарта)",
            context_block="",
        )

    return f"⚠️ Напоминание (восстановлено): {escalation_type}\n\n💬 «{message_text[:200]}»"


# =========================================================
# 3. PENDING NOTIFICATION CRUD
# =========================================================

def _save_pending(chat_thread_id, contact_id, message_id, message_text,
                  entity_type, entity_id, escalation_type, notify_at,
                  db_path) -> int | None:
    """Сохранить pending notification. Возвращает id."""
    conn = get_db(db_path)
    try:
        conn.execute("""
            INSERT INTO pending_notifications
                (chat_thread_id, contact_id, message_id, message_text,
                 entity_type, entity_id, escalation_type, notify_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (chat_thread_id, contact_id, message_id, message_text,
              entity_type, entity_id, escalation_type,
              notify_at.isoformat() if hasattr(notify_at, 'isoformat') else str(notify_at)))
        nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return nid
    except Exception as e:
        logger.error(f"save_pending error: {e}")
        return None
    finally:
        conn.close()


def _get_notification(notif_id: int, db_path: str) -> dict | None:
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pending_notifications WHERE id = ?", (notif_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _mark_sent(notif_id: int, db_path: str):
    conn = get_db(db_path)
    try:
        conn.execute(
            "UPDATE pending_notifications SET status = 'sent' WHERE id = ?",
            (notif_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _cancel(notif_id: int, db_path: str):
    conn = get_db(db_path)
    try:
        conn.execute(
            "UPDATE pending_notifications SET status = 'cancelled' WHERE id = ?",
            (notif_id,),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_by_entity(entity_type: str, entity_id: int, db_path: str):
    """Отменить все pending notifications для сущности."""
    conn = get_db(db_path)
    try:
        conn.execute("""
            UPDATE pending_notifications SET status = 'cancelled'
            WHERE entity_type = ? AND entity_id = ? AND status = 'pending'
        """, (entity_type, entity_id))
        conn.commit()
    finally:
        conn.close()


# =========================================================
# 4. HELPERS
# =========================================================

def _seconds_until(hour: int, minute: int) -> float:
    """Секунды до указанного времени сегодня/завтра в GMT+8."""
    now = datetime.now(GMT8)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _get_contact_name(contact_id: int | None, db_path: str) -> str:
    if not contact_id:
        return "Клиент"
    conn = get_db(db_path)
    try:
        row = conn.execute("SELECT name FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return row["name"] if row else "Клиент"
    finally:
        conn.close()


def _get_contact_name_sync(contact_id) -> str:
    """Sync версия для форматирования."""
    if not contact_id:
        return "Клиент"
    try:
        from config import settings
        return _get_contact_name(int(contact_id), settings.db_path)
    except Exception:
        return "Клиент"


def _build_context_block(chat_thread_id: int | None,
                          contact_id: int | None, db_path: str) -> str:
    """Собрать контекст для уведомления owner'у."""
    from services.response_context import build_notification_context, format_context_block
    ctx = build_notification_context(chat_thread_id, contact_id, db_path)
    return format_context_block(ctx)
