"""Handlers aggregator."""
from aiogram import Router
from handlers import business, commands, errors


def setup_routers() -> Router:
    router = Router()
    router.include_router(business.router)
    router.include_router(commands.router)
    router.include_router(errors.router)
    return router
