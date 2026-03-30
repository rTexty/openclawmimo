"""Scheduler — дайджесты, consolidate, abandoned, proactive checks."""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config import settings


def create_scheduler(bot, brain) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # Дайджест — ежедневно 08:00 GMT+8
    scheduler.add_job(
        _daily_digest,
        CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute,
                    timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="daily_digest",
    )

    # Proactive owner alerts — 08:30
    scheduler.add_job(
        _proactive_owner_check,
        CronTrigger(hour=8, minute=30, timezone="Asia/Shanghai"),
        args=[bot],
        id="proactive_owner",
    )

    # Client reminders — 09:00
    scheduler.add_job(
        _proactive_client_check,
        CronTrigger(hour=9, minute=0, timezone="Asia/Shanghai"),
        args=[bot],
        id="proactive_client",
    )

    # Progress check-in — 10:00
    scheduler.add_job(
        _progress_checkin,
        CronTrigger(hour=10, minute=0, timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="progress_checkin",
    )

    # Недельный — Sunday 18:00
    scheduler.add_job(
        _weekly_report,
        CronTrigger(day_of_week="sun", hour=18, minute=0,
                    timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="weekly_report",
    )

    # Consolidate — 03:00 (низкая нагрузка)
    scheduler.add_job(
        _consolidate,
        CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
        args=[brain],
        id="consolidate",
    )

    # Abandoned check — каждые 4 часа
    scheduler.add_job(
        _check_abandoned,
        CronTrigger(hour="*/4", timezone="Asia/Shanghai"),
        args=[bot, brain],
        id="abandoned_check",
    )

    return scheduler


async def _daily_digest(bot, brain):
    from services.digest import generate_and_send_daily
    await generate_and_send_daily(bot, brain)


async def _proactive_owner_check(bot):
    from services.proactive import send_owner_alerts
    await send_owner_alerts(bot, settings.db_path)


async def _proactive_client_check(bot):
    from services.proactive import send_client_reminders
    await send_client_reminders(bot, settings.db_path)


async def _progress_checkin(bot, brain):
    from services.proactive import send_progress_checkins
    await send_progress_checkins(bot, brain, settings.db_path)


async def _weekly_report(bot, brain):
    from services.digest import generate_and_send_weekly
    await generate_and_send_weekly(bot, brain)


async def _consolidate(brain):
    from services import memory as mem
    await asyncio.to_thread(mem.run_consolidation, settings.db_path, brain)


async def _check_abandoned(bot, brain):
    from services.digest import check_abandoned
    await check_abandoned(bot, brain)
