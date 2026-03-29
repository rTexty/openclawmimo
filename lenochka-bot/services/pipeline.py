"""
Pipeline Processor — async ingest queue с batch classify и batch embeddings.

Архитектурные решения:
1. Batch classify: N сообщений → 1 LLM-вызов вместо N (экономия ~60% токенов)
2. Batch embed: N текстов → 1 forward pass через sentence-transformers (~7x быстрее)
3. Async queue с неблокирующей обработкой через asyncio.to_thread для sync операций
4. Каждый item обрабатывается независимо — ошибка в одном не ломает батч
"""
import asyncio
import sqlite3
import json
import logging
from dataclasses import dataclass, field
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
    normalized: NormalizedMessage | None = None
    content_hash: str | None = None
    contact_id: int | None = None
    chat_thread_id: int | None = None
    message_id: int | None = None  # internal DB id


class PipelineProcessor:
    """
    Async ingest pipeline с batch processing.
    
    Флоу батча:
    1. normalize (sync, мгновенно)
    2. dedup (sync, SQL)
    3. resolve contact + chat_thread (sync, SQL)
    4. store raw messages (sync, SQL) — все сразу
    5. batch classify (1 LLM-вызов на N сообщений)
    6. batch embed (1 forward pass на N текстов)
    7. extract entities (только для важных, поштучно — LLM)
    8. store memories + CHAOS + CRM (sync, SQL)
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
        await asyncio.to_thread(
            mem_svc.soft_delete_messages, chat_id, message_ids, self.db_path
        )

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

                # Обработка батча
                await self._process_batch(batch)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Pipeline loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _process_batch(self, batch: list[PipelineItem]):
        """
        Обработать батч сообщений с batch classify и batch embeddings.
        
        Критические операции через asyncio.to_thread чтобы не блокировать
        event loop Telegram API.
        """
        # Phase 1: Normalize + dedup + store messages (sync, fast)
        valid_items: list[PipelineItem] = []

        for item in batch:
            try:
                await self._normalize_and_store(item)
                if item.normalized and item.message_id:
                    valid_items.append(item)
            except Exception as e:
                logger.error(f"Normalize/store error: {e}", exc_info=True)

        if not valid_items:
            return

        # Phase 2: Batch classify — ОДИН LLM-вызов на весь батч
        texts = [item.normalized.full_text for item in valid_items]
        chat_contexts = []

        for item in valid_items:
            ctx = await asyncio.to_thread(
                self._get_chat_context, item.chat_thread_id
            )
            chat_contexts.append(ctx)

        classifications = await asyncio.to_thread(
            self._batch_classify, texts, chat_contexts
        )

        # Phase 3: Batch embed — ОДИН forward pass на весь батч
        # Собираем тексты для эмбеддинга (только важные типы)
        important_texts = []
        important_indices = []
        for i, (label, _, _) in enumerate(classifications):
            if label in ("task", "decision", "lead-signal", "risk"):
                important_texts.append(texts[i])
                important_indices.append(i)

        embeddings = {}
        if important_texts and self.brain.is_ready():
            try:
                vecs = await asyncio.to_thread(
                    self.brain.embed_texts_batch, important_texts
                )
                for idx, vec in zip(important_indices, vecs):
                    embeddings[idx] = vec
            except Exception as e:
                logger.warning(f"Batch embed failed, falling back: {e}")

        # Phase 4: Extract + store (поштучно, только для важных)
        for i, item in enumerate(valid_items):
            try:
                label, conf, reason = classifications[i]
                await self._finalize_item(item, label, conf, reason,
                                          embeddings.get(i))
            except Exception as e:
                logger.error(f"Finalize error: {e}", exc_info=True)

    async def _normalize_and_store(self, item: PipelineItem):
        """
        Normalize + dedup/supersede + resolve contact + store raw message.
        
        Для business_edited: supersede существующую запись вместо создания дубля.
        """
        msg = item.message

        # 1. Normalize
        nm = normalize_message(msg)
        if not nm.text or nm.text.startswith("[unsupported"):
            return
        item.normalized = nm

        # 2. Resolve contact + chat thread (нужно для supersede lookup)
        contact_id, chat_thread_id = await asyncio.to_thread(
            resolve_contact, msg, item.source, self.db_path
        )
        item.contact_id = contact_id
        item.chat_thread_id = chat_thread_id

        # 3. Supersede для edited messages
        if item.source == "business_edited":
            existing_id = await asyncio.to_thread(
                mem_svc.supersede_message,
                chat_thread_id=chat_thread_id,
                source_msg_id=msg.message_id,
                new_text=nm.text,
                new_meta=nm.metadata,
                db_path=self.db_path,
            )
            if existing_id:
                # Нашли и обновили — pipeline обработает classification заново
                item.message_id = existing_id
                item.content_hash = mem_svc.content_hash(nm.text)
                logger.info(f"Supersede: msg#{existing_id} updated from edit")
                return
            # Не нашли — treat as new message, падаем в обычный flow ниже

        # 4. Dedup для новых сообщений
        ch = await asyncio.to_thread(
            mem_svc.dedup_check, msg, nm.text, self.db_path
        )
        if ch is None:
            item.normalized = None  # маркер дубликата
            return
        item.content_hash = ch

        # 5. Store raw message
        mid = await asyncio.to_thread(
            mem_svc.store_message,
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
        item.message_id = mid

    def _batch_classify(self, texts: list[str],
                        chat_contexts: list[str]) -> list[tuple]:
        """
        Batch classify: ОДИН LLM-вызов на N сообщений.
        
        Делегирует в brain.classify_batch() — чистый API.
        При недоступности LLM fallback на heuristic поштучно.
        """
        if not texts:
            return []

        if self.brain.is_ready():
            try:
                return self.brain.classify_batch(texts, chat_contexts)
            except Exception as e:
                logger.warning(f"Batch classify failed: {e}")

        # Fallback: heuristic поштучно
        from brain import _classify_heuristic
        return [_classify_heuristic(t) for t in texts]

    async def _finalize_item(self, item: PipelineItem, label: str,
                             conf: float, reason: str, embedding=None):
        """Extract entities + store memory + CHAOS + CRM для одного item."""
        nm = item.normalized

        # Extract entities (только для важных типов — экономим LLM)
        entities = {}
        if label not in ("noise", "chit-chat") and self.brain.is_ready():
            chat_ctx = await asyncio.to_thread(
                self._get_chat_context, item.chat_thread_id
            )

            # Enrich: entity expansion — LLM узнаёт существующие contacts/deals/tasks
            enriched_ctx = await asyncio.to_thread(
                self._enrich_extract_context, chat_ctx,
                item.contact_id, item.chat_thread_id
            )

            entities = await asyncio.to_thread(
                self.brain.extract_entities, nm.full_text, label, enriched_ctx
            )

        # Store memory + CHAOS (только для бизнес-типов)
        if label in ("task", "decision", "lead-signal", "risk"):
            importance = 0.8 if label in ("decision", "risk") else 0.6
            await asyncio.to_thread(
                self.brain.store_memory,
                content=f"[{label}] {nm.text[:200]}",
                mem_type="episodic",
                importance=importance,
                contact_id=item.contact_id,
                chat_thread_id=item.chat_thread_id,
                source_message_id=item.message_id,
                content_hash=item.content_hash,
                auto_associate=False,  # defer на nightly consolidate
            )
            await asyncio.to_thread(
                self.brain.chaos_store,
                content=nm.text[:200],
                category=label,
                priority=importance,
                contact_id=item.contact_id,
            )

        # CRM upsert
        if entities and label not in ("noise", "chit-chat"):
            await asyncio.to_thread(
                crm_upsert, entities, item.contact_id,
                item.chat_thread_id, item.message_id, self.db_path
            )

        # Mark analyzed
        await asyncio.to_thread(
            self._mark_analyzed, item.message_id, label
        )

        logger.info(
            f"Ingest: msg#{item.message_id} [{label}] conf={conf:.2f} "
            f"contact={item.contact_id} type={nm.content_type}"
        )

    def _get_chat_context(self, chat_thread_id: int) -> str:
        """
        Контекст последних 5 сообщений из чата для классификации.
        
        Архитектурное решение: контекст позволяет классифицировать
        короткие ответы ('да', 'нет', 'ок') корректно — reply на '150к?' = decision,
        reply на 'как дела?' = chit-chat.
        
        Используем chat_thread_id как FK из messages.chat_thread_id.
        """
        if not chat_thread_id:
            return ""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT text, from_user_id, sent_at FROM messages
                   WHERE chat_thread_id = ? AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
                   ORDER BY sent_at DESC LIMIT 5""",
                (chat_thread_id,),
            ).fetchall()
        except Exception:
            return ""
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

    def _enrich_extract_context(self, chat_ctx: str,
                                contact_id: int | None,
                                chat_thread_id: int | None) -> str:
        """
        Обогатить контекст для extract_entities.
        Добавляет существующую информацию о контакте, сделках и задачах,
        чтобы LLM корректно извлекал сущности (не дублировал, не терял связь).
        """
        parts = [chat_ctx] if chat_ctx else []

        if not contact_id:
            return " ".join(parts)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Contact info
            contact = conn.execute(
                "SELECT name, tg_username, company_id FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact:
                info = f"Контакт: {contact['name']}"
                if contact["tg_username"]:
                    info += f" (@{contact['tg_username']})"
                if contact["company_id"]:
                    comp = conn.execute(
                        "SELECT name FROM companies WHERE id = ?",
                        (contact["company_id"],),
                    ).fetchone()
                    if comp:
                        info += f", {comp['name']}"
                parts.append(info)

            # Active deals
            deals = conn.execute(
                """SELECT amount, stage FROM deals
                   WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
                   ORDER BY created_at DESC LIMIT 3""",
                (contact_id,),
            ).fetchall()
            for d in deals:
                amt = f"{d['amount']:,.0f}₽" if d["amount"] else "сумма не указана"
                parts.append(f"Активная сделка: {amt}, стадия: {d['stage']}")

            # Open tasks
            tasks = conn.execute(
                """SELECT description, priority, due_at FROM tasks
                   WHERE related_type = 'contact' AND related_id = ?
                     AND status NOT IN ('done', 'cancelled')
                   ORDER BY due_at ASC LIMIT 3""",
                (contact_id,),
            ).fetchall()
            for t in tasks:
                due = f" (до {t['due_at'][:10]})" if t.get("due_at") else ""
                parts.append(f"Задача: {t['description'][:60]}{due}")

        except Exception:
            pass
        finally:
            conn.close()

        return " | ".join(parts)

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
