"""Scheduler — дайджесты, consolidate, abandoned check."""
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


async def _weekly_report(bot, brain):
    from services.digest import generate_and_send_weekly
    await generate_and_send_weekly(bot, brain)


async def _consolidate(brain):
    from services import memory as mem
    await asyncio.to_thread(mem.run_consolidation, settings.db_path, brain)


async def _check_abandoned(bot, brain):
    from services.digest import check_abandoned
    await check_abandoned(bot, brain)
