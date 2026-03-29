"""
Business Handlers — обработка business_connection и business_message.
Главный источник данных для CRM.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted

from filters.business import IsBusinessMessage

router = Router(name="business")
logger = logging.getLogger("lenochka.business")


@router.business_connection()
async def on_business_connection(bc: BusinessConnection, **kwargs):
    """Бот подключён/отключён от Business-аккаунта."""
    from config import settings
    from services import memory as mem

    rights = bc.rights
    can_reply = rights.can_reply if rights else False
    can_read = rights.can_read_messages if rights else True

    logger.info(
        f"Business connection: user={bc.user.id}, status={bc.status}, "
        f"can_reply={can_reply}, can_read={can_read}"
    )

    if bc.status == "active":
        mem.register_business_connection(
            bc.user.id, bc.id, can_reply, can_read, settings.db_path
        )
    elif bc.status == "revoked":
        mem.revoke_business_connection(bc.id, settings.db_path)


@router.message(IsBusinessMessage())
async def on_business_message(message: Message, pipeline, **kwargs):
    """
    Главный обработчик: бизнес-сообщение → ingest pipeline.
    
    ВСЕ сообщения сохраняются — включая свои (sender_business_bot)
    и автоответы (is_from_offline). CRM должна знать всё.
    
    Антипетля работает только на УРОВНЕ ОТВЕТА (should_respond),
    а НЕ на уровне записи. Не сейчас — но логика такая:
    если message.sender_business_bot → НЕ генерируем ответ.
    """
    await pipeline.enqueue(
        message=message,
        source="business",
        business_connection_id=message.business_connection_id,
    )


@router.edited_business_message()
async def on_business_edited(message: Message, pipeline, **kwargs):
    """Отредактированное → supersede. Все сообщения, включая свои."""
    await pipeline.enqueue(
        message=message,
        source="business_edited",
        business_connection_id=message.business_connection_id,
    )


@router.deleted_business_messages()
async def on_business_deleted(deleted: BusinessMessagesDeleted, pipeline, **kwargs):
    """Удалённые → soft-delete."""
    await pipeline.handle_deleted(
        business_connection_id=deleted.business_connection_id,
        chat_id=deleted.chat.id,
        message_ids=deleted.message_ids,
    )
