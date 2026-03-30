"""
Proactive Engine — упреждающие уведомления.

1. Owner alerts: tasks/agreements/deals/invoices due in 2-3 days
2. Client reminders: client obligations (invoices, agreements) via business API
3. Progress check-in: owner confirms task status before deadline
"""
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from services.brain_wrapper import get_db
from services.response_engine import parse_progress_reply_llm, format_progress_confirmation

logger = logging.getLogger("lenochka.proactive")

GMT8 = timezone(timedelta(hours=8))


# =========================================================
# 1. OWNER ALERTS — что наступает через 2-3 дня
# =========================================================

async def send_owner_alerts(bot, db_path: str):
    """Ежедневная проверка: что наступает в ближайшие дни."""
    alerts = []

    # Tasks due in 2 days
    for t in _get_upcoming_tasks(2, db_path):
        if not _was_sent("owner_task", t["id"], "task_due", db_path):
            days_left = _days_until(t["due_at"])
            text = (
                f"📋 Задача через {days_left}д: {t['description'][:80]}\n\n"
                f"👤 {t.get('contact_name', '—')}\n"
                f"📅 Срок: {t['due_at'][:10]}\n"
                f"{'🔴' if t['priority']=='urgent' else '🟡' if t['priority']=='high' else '⚪'} Приоритет: {t['priority']}"
            )
            alerts.append(("owner_task", t["id"], "task_due", text))

    # Agreements due in 3 days
    for a in _get_upcoming_agreements(3, db_path):
        if not _was_sent("owner_agreement", a["id"], "agreement_due", db_path):
            days_left = _days_until(a["due_at"])
            amt = f"{a['amount']:,.0f}₽" if a.get("amount") else ""
            text = (
                f"📝 Договор через {days_left}д: {a.get('summary', '—')}\n\n"
                f"👤 {a.get('contact_name', '—')}\n"
                f"💰 {amt}\n"
                f"📅 Срок: {a['due_at'][:10]}\n"
                f"📌 Статус: {a['status']}"
            )
            alerts.append(("owner_agreement", a["id"], "agreement_due", text))

    # Deals closing in 3 days
    for d in _get_upcoming_deal_closures(3, db_path):
        if not _was_sent("owner_deal", d["id"], "deal_closing", db_path):
            days_left = _days_until(d["expected_close_at"])
            amt = f"{d['amount']:,.0f}₽" if d.get("amount") else "сумма не указана"
            text = (
                f"💰 Сделка закрывается через {days_left}д\n\n"
                f"👤 {d.get('contact_name', '—')}\n"
                f"💵 {amt} ({d['stage']})\n"
                f"📅 Ожидаемая дата: {d['expected_close_at'][:10]}"
            )
            alerts.append(("owner_deal", d["id"], "deal_closing", text))

    # Invoices due in 2 days
    for i in _get_upcoming_invoices(2, db_path):
        if not _was_sent("owner_invoice", i["id"], "invoice_due", db_path):
            days_left = _days_until(i["due_at"])
            text = (
                f"🧾 Счёт через {days_left}д: {i['amount']:,.0f}₽\n\n"
                f"👤 {i.get('contact_name', '—')}\n"
                f"📝 По договору: {i.get('agreement_summary', '—')}\n"
                f"📅 Срок оплаты: {i['due_at'][:10]}"
            )
            alerts.append(("owner_invoice", i["id"], "invoice_due", text))

    # Send all
    if alerts:
        for entity_type, entity_id, alert_type, text in alerts:
            try:
                await bot.send_message(
                    chat_id=_get_owner_id(),
                    text=text,
                    parse_mode="HTML",
                )
                _mark_sent_proactive(entity_type, entity_id, alert_type, db_path)
            except Exception as e:
                logger.error(f"Owner alert failed: {e}")

        logger.info(f"Owner alerts sent: {len(alerts)}")


# =========================================================
# 2. CLIENT REMINDERS — obligations клиента через business API
# =========================================================

async def send_client_reminders(bot, db_path: str):
    """Напоминания клиентам об их obligations за 2-3 дня."""
    obligations = _get_client_obligations(3, db_path)

    # Агрегируем по contact
    by_contact: dict[int, list[dict]] = {}
    for ob in obligations:
        cid = ob.get("contact_id")
        if not cid:
            continue
        if not _was_sent(f"client_{ob['type']}", ob["entity_id"],
                          f"client_{ob['type']}", db_path):
            by_contact.setdefault(cid, []).append(ob)

    for contact_id, items in by_contact.items():
        # Ищем chat_thread и business_connection
        conn = get_db(db_path)
        try:
            ct = conn.execute("""
                SELECT ct.tg_chat_id, ct.id as chat_thread_id
                FROM chat_threads ct
                WHERE ct.contact_id = ?
                ORDER BY ct.created_at DESC LIMIT 1
            """, (contact_id,)).fetchone()

            biz = conn.execute("""
                SELECT connection_id FROM business_connections
                WHERE status = 'active' AND can_reply = 1
                ORDER BY connected_at DESC LIMIT 1
            """).fetchone()
        finally:
            conn.close()

        if not ct or not ct["tg_chat_id"]:
            continue

        # Формируем текст напоминания
        reminder_text = _format_client_reminder(items)

        if biz and biz["connection_id"]:
            # Отправляем клиенту через business API
            try:
                await bot.send_message(
                    chat_id=int(ct["tg_chat_id"]),
                    text=reminder_text,
                    business_connection_id=biz["connection_id"],
                )
                # Уведомляем owner'а что напомнили
                contact_name = items[0].get("contact_name", "Клиент")
                what = _summarize_obligations(items)
                await bot.send_message(
                    chat_id=_get_owner_id(),
                    text=f"✅ Напомнил {contact_name}: {what}",
                )
            except Exception as e:
                logger.error(f"Client reminder failed: {e}")
                # Fallback: просим owner'а написать
                await _send_owner_fallback(bot, items, db_path)
        else:
            # Нет прав — просим owner'а
            await _send_owner_fallback(bot, items, db_path)

        # Mark sent
        for item in items:
            _mark_sent_proactive(
                f"client_{item['type']}", item["entity_id"],
                f"client_{item['type']}", db_path
            )


def _format_client_reminder(items: list[dict]) -> str:
    """Сформировать текст напоминания клиенту."""
    if len(items) == 1:
        ob = items[0]
        if ob["type"] == "invoice":
            return (
                f"Добрый день! 🧾\n\n"
                f"Напоминаем об оплате по договору «{ob.get('agreement_summary', '—')}».\n"
                f"Сумма: {ob.get('amount', 0):,.0f}₽\n"
                f"Срок: {ob['due_at'][:10]}\n\n"
                f"Если уже оплатили — проигнорируйте это сообщение."
            )
        elif ob["type"] == "agreement":
            amt = f"\nСумма: {ob.get('amount', 0):,.0f}₽" if ob.get("amount") else ""
            return (
                f"Добрый день! 📝\n\n"
                f"Напоминаем о подписании договора «{ob.get('summary', '—')}».\n"
                f"{amt}\n"
                f"Срок: {ob['due_at'][:10]}\n\n"
                f"Если есть вопросы — напишите, поможем."
            )
        elif ob["type"] == "client_task":
            return (
                f"Добрый день! 📋\n\n"
                f"Напоминаем: {ob.get('description', '—')}\n"
                f"Срок: {ob['due_at'][:10]}\n\n"
                f"Если уже сделали — проигнорируйте."
            )

    # Multiple obligations — aggregated
    lines = []
    for ob in items:
        if ob["type"] == "invoice":
            lines.append(f"🧾 Оплата {ob.get('amount', 0):,.0f}₽ до {ob['due_at'][:10]}")
        elif ob["type"] == "agreement":
            lines.append(f"📝 Подписание «{ob.get('summary', '—')}» до {ob['due_at'][:10]}")
        elif ob["type"] == "client_task":
            lines.append(f"📋 {ob.get('description', '—')} до {ob['due_at'][:10]}")

    items_text = "\n".join(lines)
    return (
        f"Добрый день! 📋\n\n"
        f"Напоминаем:\n{items_text}\n\n"
        f"Если уже всё сделали — проигнорируйте."
    )


def _summarize_obligations(items: list[dict]) -> str:
    parts = []
    for ob in items:
        if ob["type"] == "invoice":
            parts.append(f"оплата {ob.get('amount', 0):,.0f}₽")
        elif ob["type"] == "agreement":
            parts.append(f"подписание «{ob.get('summary', '')[:30]}»")
        elif ob["type"] == "client_task":
            parts.append(f"{ob.get('description', '')[:40]}")
    return ", ".join(parts)


async def _send_owner_fallback(bot, items: list[dict], db_path: str):
    """Уведомить owner'а что нужно напомнить клиенту (нет прав на ответ)."""
    contact_name = items[0].get("contact_name", "Клиент")
    what = _summarize_obligations(items)
    due = items[0].get("due_at", "")[:10]
    text = (
        f"📋 Напомни {contact_name} — {what}\n\n"
        f"📅 Срок: {due}\n"
        f"⚠️ Не могу написать сам (нет прав на ответ). Напишите клиенту."
    )
    try:
        await bot.send_message(chat_id=_get_owner_id(), text=text)
    except Exception as e:
        logger.error(f"Owner fallback notify failed: {e}")


# =========================================================
# 3. PROGRESS CHECK-IN — owner подтверждает прогресс
# =========================================================

async def send_progress_checkins(bot, brain, db_path: str):
    """Задачи due в 1-5 дней, не проверяли >2 дня → check-in owner'у."""
    tasks = _get_checkin_candidates(5, 2, db_path)

    for task in tasks:
        days_left = _days_until(task["due_at"])
        contact_name = task.get("contact_name", "—")

        # Собираем контекст
        deal_info = ""
        conn = get_db(db_path)
        try:
            if task.get("related_type") == "contact" and task.get("related_id"):
                deal = conn.execute("""
                    SELECT amount, stage FROM deals
                    WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
                    ORDER BY updated_at DESC LIMIT 1
                """, (task["related_id"],)).fetchone()
                if deal:
                    amt = f"{deal['amount']:,.0f}₽" if deal.get("amount") else ""
                    deal_info = f"\n💰 Сделка: {amt} ({deal['stage']})"

            # Last messages
            last_msgs = ""
            chat_thread = conn.execute("""
                SELECT id FROM chat_threads WHERE contact_id = ?
                ORDER BY created_at DESC LIMIT 1
            """, (task.get("related_id"),)).fetchone() if task.get("related_id") else None

            if chat_thread:
                msgs = conn.execute("""
                    SELECT text, from_user_id FROM messages
                    WHERE chat_thread_id = ?
                      AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
                    ORDER BY sent_at DESC LIMIT 2
                """, (chat_thread["id"],)).fetchall()
                if msgs:
                    last_msgs = "\nПоследнее в чате:\n" + "\n".join(
                        f"  {'Я' if m['from_user_id']=='self' else 'Клиент'}: {m['text'][:60]}"
                        for m in reversed(msgs)
                    )
        finally:
            conn.close()

        status_emoji = "🔨" if task["status"] == "in_progress" else "📋"
        text = (
            f"📋 Check-in: {task['description'][:80]} [task:{task['id']}]\n\n"
            f"👤 Клиент: {contact_name}"
            f"{deal_info}\n"
            f"📅 Дедлайн: {task['due_at'][:10]} (осталось {days_left}д)\n"
            f"{status_emoji} Статус: {task['status']}"
            f"{last_msgs}\n\n"
            f"Как дела? Напишите статус или опишите что происходит."
        )

        try:
            await bot.send_message(
                chat_id=_get_owner_id(),
                text=text,
            )
            # Обновляем last_progress_check
            conn = get_db(db_path)
            try:
                conn.execute("""
                    UPDATE tasks SET updated_at = datetime('now')
                    WHERE id = ?
                """, (task["id"],))
                conn.commit()
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Progress check-in failed: {e}")

    if tasks:
        logger.info(f"Progress check-ins sent: {len(tasks)}")


# =========================================================
# SQL HELPERS — queries для proactive checks
# =========================================================

def _get_upcoming_tasks(days: int, db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT t.*, c.name as contact_name FROM tasks t
            LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
            WHERE t.due_at BETWEEN datetime('now') AND datetime('now', ? || ' days')
              AND t.status NOT IN ('done', 'cancelled')
            ORDER BY t.due_at ASC
        """, (str(days),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_upcoming_agreements(days: int, db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT a.*, c.name as contact_name FROM agreements a
            JOIN contacts c ON a.contact_id = c.id
            WHERE a.due_at BETWEEN date('now') AND date('now', ? || ' days')
              AND a.status NOT IN ('signed', 'completed', 'cancelled')
            ORDER BY a.due_at ASC
        """, (str(days),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_upcoming_deal_closures(days: int, db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT d.*, c.name as contact_name FROM deals d
            JOIN contacts c ON d.contact_id = c.id
            WHERE d.expected_close_at BETWEEN date('now') AND date('now', ? || ' days')
              AND d.stage NOT IN ('closed_won', 'closed_lost')
            ORDER BY d.expected_close_at ASC
        """, (str(days),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_upcoming_invoices(days: int, db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT i.*, a.summary as agreement_summary, c.name as contact_name
            FROM invoices i
            JOIN agreements a ON i.agreement_id = a.id
            JOIN contacts c ON a.contact_id = c.id
            WHERE i.due_at BETWEEN date('now') AND date('now', ? || ' days')
              AND i.status NOT IN ('paid', 'cancelled')
            ORDER BY i.due_at ASC
        """, (str(days),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_client_obligations(days: int, db_path: str) -> list[dict]:
    """Obligations клиента: неоплаченные счета, неподписанные договоры, клиентские задачи."""
    conn = get_db(db_path)
    results = []
    try:
        # Unpaid invoices
        rows = conn.execute("""
            SELECT 'invoice' as type, i.id as entity_id,
                   i.amount, i.due_at, i.status,
                   a.summary as agreement_summary,
                   c.id as contact_id, c.name as contact_name
            FROM invoices i
            JOIN agreements a ON i.agreement_id = a.id
            JOIN contacts c ON a.contact_id = c.id
            WHERE i.due_at BETWEEN date('now') AND date('now', ? || ' days')
              AND i.status IN ('sent', 'overdue')
            ORDER BY i.due_at ASC
        """, (str(days),)).fetchall()
        results.extend([dict(r) for r in rows])

        # Unsigned agreements
        rows = conn.execute("""
            SELECT 'agreement' as type, a.id as entity_id,
                   a.amount, a.due_at, a.status, a.summary,
                   c.id as contact_id, c.name as contact_name
            FROM agreements a
            JOIN contacts c ON a.contact_id = c.id
            WHERE a.due_at BETWEEN date('now') AND date('now', ? || ' days')
              AND a.status IN ('sent', 'draft')
            ORDER BY a.due_at ASC
        """, (str(days),)).fetchall()
        results.extend([dict(r) for r in rows])

        # Client-facing tasks
        rows = conn.execute("""
            SELECT 'client_task' as type, t.id as entity_id,
                   t.description, t.due_at, t.priority,
                   c.id as contact_id, c.name as contact_name
            FROM tasks t
            LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
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
    finally:
        conn.close()

    return results


def _get_checkin_candidates(max_days: int, min_since_check: int,
                             db_path: str) -> list[dict]:
    """Задачи для progress check-in: due в 1-max_days, не проверяли >min_since_check дней."""
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT t.*, c.name as contact_name FROM tasks t
            LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
            WHERE t.due_at BETWEEN datetime('now') AND datetime('now', ? || ' days')
              AND t.status NOT IN ('done', 'cancelled')
              AND (t.updated_at < datetime('now', ? || ' days') OR t.updated_at IS NULL)
            ORDER BY t.due_at ASC
            LIMIT 5
        """, (str(max_days), f"-{min_since_check}")).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =========================================================
# DEDUP HELPERS
# =========================================================

def _was_sent(entity_type: str, entity_id: int, alert_type: str,
              db_path: str) -> bool:
    """Проверить, отправляли ли уже это proactive-напоминание."""
    conn = get_db(db_path)
    try:
        row = conn.execute("""
            SELECT id FROM pending_notifications
            WHERE entity_type = ? AND entity_id = ?
              AND escalation_type = ?
              AND status = 'sent'
              AND created_at > datetime('now', '-3 days')
        """, (entity_type, entity_id, alert_type)).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_sent_proactive(entity_type: str, entity_id: int,
                          alert_type: str, db_path: str):
    """Записать что proactive-напоминание отправлено."""
    conn = get_db(db_path)
    try:
        conn.execute("""
            INSERT INTO pending_notifications
                (entity_type, entity_id, escalation_type, notify_at, status)
            VALUES (?, ?, ?, datetime('now'), 'sent')
        """, (entity_type, entity_id, alert_type))
        conn.commit()
    finally:
        conn.close()


# =========================================================
# UTILITY
# =========================================================

def _days_until(date_str: str) -> int:
    """Дней до даты."""
    try:
        target = datetime.fromisoformat(date_str[:10])
        delta = target - datetime.now()
        return max(0, delta.days)
    except Exception:
        return 0


def _get_owner_id() -> int:
    from config import settings
    return settings.owner_id
