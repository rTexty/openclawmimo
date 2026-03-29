"""
Pipeline Processor — async ingest queue с батчингом.
Не блокирует Telegram API при LLM-вызовах.
"""
import asyncio
import sqlite3
import logging
from dataclasses import dataclass
from aiogram.types import Message

from services.normalizer import normalize_message, NormalizedMessage
from services.contact_resolver import resolve_contact
from services.crm_upsert import crm_upsert
from services import memory as mem_svc

logger = logging.getLogger("lenochka.pipeline")


@dataclass
class PipelineItem:
    message: Message
    source: str  # "business", "business_edited", "direct"
    business_connection_id: str | None = None


class PipelineProcessor:
    """
    Async ingest pipeline с батчингом.
    Принимает сообщения через enqueue(), обрабатывает в фоне.
    """

    def __init__(self, brain, db_path: str, batch_size: int = 10,
                 batch_interval: float = 3.0):
        self.brain = brain
        self.db_path = db_path
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.queue: asyncio.Queue[PipelineItem] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._process_loop())
        logger.info("Pipeline processor started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, message: Message, source: str,
                      business_connection_id: str | None = None):
        """Добавить сообщение в очередь (non-blocking)."""
        await self.queue.put(PipelineItem(
            message=message,
            source=source,
            business_connection_id=business_connection_id,
        ))

    async def handle_deleted(self, business_connection_id: str,
                             chat_id: int, message_ids: list[int]):
        """Обработать удалённые сообщения."""
        mem_svc.soft_delete_messages(chat_id, message_ids, self.db_path)

    async def _process_loop(self):
        """Фоновый цикл: собирает батч и обрабатывает."""
        while True:
            try:
                # Ждём первое сообщение
                item = await asyncio.wait_for(
                    self.queue.get(), timeout=self.batch_interval
                )
                batch = [item]

                # Собираем остальные из очереди (до 1 сек)
                deadline = asyncio.get_event_loop().time() + 1.0
                while len(batch) < self.batch_size:
                    timeout = max(0.01, deadline - asyncio.get_event_loop().time())
                    if timeout <= 0.01:
                        break
                    try:
                        item = await asyncio.wait_for(
                            self.queue.get(), timeout=timeout
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break

                # Обработка
                await self._process_batch(batch)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Pipeline loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_batch(self, batch: list[PipelineItem]):
        """Обработать батч сообщений."""
        for item in batch:
            try:
                await self._process_one(item)
            except Exception as e:
                logger.error(f"Pipeline item error: {e}", exc_info=True)

    async def _process_one(self, item: PipelineItem):
        """Обработать одно сообщение — полный ingest pipeline."""
        msg = item.message

        # 1. Normalize
        nm = normalize_message(msg)
        if not nm.text or nm.text.startswith("[unsupported"):
            return

        # 2. Dedup
        ch = mem_svc.dedup_check(msg, nm.text, self.db_path)
        if ch is None:
            return  # Duplicate

        # 3. Resolve contact + chat thread
        contact_id, chat_thread_id = resolve_contact(msg, item.source, self.db_path)

        # 4. Store raw message
        message_id = mem_svc.store_message(
            chat_thread_id=chat_thread_id,
            from_user_id=str(msg.from_user.id) if msg.from_user else "self",
            text=nm.text,
            sent_at=msg.date,
            content_type=nm.content_type,
            meta=nm.metadata,
            source_msg_id=msg.message_id,
            content_hash_val=ch,
            db_path=self.db_path,
        )

        # 5. Classify (heuristic first, LLM only if unsure)
        full_text = nm.full_text
        chat_context = await asyncio.to_thread(
            self._get_chat_context, chat_thread_id
        )
        label, conf, reason = self.brain.classify_message(
            full_text, chat_context=chat_context
        )

        # 6. Extract entities (skip for noise/chit-chat to save LLM costs)
        entities = {}
        if label not in ("noise", "chit-chat"):
            entities = self.brain.extract_entities(
                full_text, label=label, chat_context=chat_context
            )

        # 7-8. Store memory + CHAOS (only important types)
        if label in ("task", "decision", "lead-signal", "risk"):
            importance = 0.8 if label in ("decision", "risk") else 0.6
            await asyncio.to_thread(
                self.brain.store_memory,
                content=f"[{label}] {nm.text[:200]}",
                mem_type="episodic",
                importance=importance,
                contact_id=contact_id,
                chat_thread_id=chat_thread_id,
                source_message_id=message_id,
                content_hash=ch,
                auto_associate=False,  # skip для скорости, сделаем в consolidate
            )
            await asyncio.to_thread(
                self.brain.chaos_store,
                content=nm.text[:200],
                category=label,
                priority=importance,
                contact_id=contact_id,
            )

        # 9. CRM upsert
        if entities and label not in ("noise", "chit-chat"):
            await asyncio.to_thread(
                crm_upsert, entities, contact_id, chat_thread_id,
                message_id, self.db_path
            )

        # 10. Mark analyzed + update classification
        await asyncio.to_thread(
            self._mark_analyzed, message_id, label
        )

        logger.info(
            f"Ingest: msg#{message_id} [{label}] conf={conf:.2f} "
            f"contact={contact_id} type={nm.content_type}"
        )

    def _get_chat_context(self, chat_thread_id: int) -> str:
        """Последние 5 сообщений из чата для контекста классификации."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT text, from_user_id, sent_at FROM messages
                   WHERE chat_thread_id = ?
                   ORDER BY sent_at DESC LIMIT 5""",
                (chat_thread_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return ""

        lines = []
        for r in reversed(rows):
            author = "Я" if r["from_user_id"] == "self" else "Клиент"
            text = r["text"][:100] if r["text"] else ""
            lines.append(f"[{author}: {text}]")
        return " ".join(lines)

    def _mark_analyzed(self, message_id: int, label: str):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE messages SET analyzed = 1, classification = ? WHERE id = ?",
                (label, message_id),
            )
            conn.commit()
        finally:
            conn.close()
