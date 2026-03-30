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

    def __init__(self, brain, db_path: str, bot=None,
                 batch_size: int = 10, batch_interval: float = 3.0):
        self.brain = brain
        self.db_path = db_path
        self.bot = bot
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
                import time
                deadline = time.monotonic() + 1.0
                while len(batch) < self.batch_size:
                    timeout = max(0.01, deadline - time.monotonic())
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

        # Phase 2: COMBINED classify + route — ОДИН LLM-вызов на весь батч
        texts = [item.normalized.full_text for item in valid_items]
        chat_contexts = []

        for item in valid_items:
            ctx = await asyncio.to_thread(
                self._get_chat_context, item.chat_thread_id
            )
            chat_contexts.append(ctx)

        from services.response_engine import classify_and_route_batch
        decisions = await asyncio.to_thread(
            classify_and_route_batch, texts, chat_contexts, self.brain
        )

        # Phase 3: Batch embed — ОДИН forward pass на весь батч
        # Собираем тексты для эмбеддинга (только важные типы)
        important_texts = []
        important_indices = []
        for i, d in enumerate(decisions):
            label = d.get("label", "other")
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

        # Phase 4: Process each item — store + response handling
        for i, item in enumerate(valid_items):
            try:
                d = decisions[i] if i < len(decisions) else {"label": "other", "action": "escalate"}
                await self._process_decision(item, d, embeddings.get(i))
            except Exception as e:
                logger.error(f"Process decision error: {e}", exc_info=True)

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
            sent_at=msg.date.timestamp() if hasattr(msg.date, 'timestamp') else msg.date,
            content_type=nm.content_type,
            meta=nm.metadata,
            source_msg_id=msg.message_id,
            content_hash_val=ch,
            db_path=self.db_path,
        )
        item.message_id = mid

    async def _process_decision(self, item: PipelineItem, decision: dict,
                                embedding=None):
        """
        Обработать одно сообщение: store memory + response handling.
        Combined classify+route дал нам label + action + intent в одном объекте.
        """
        nm = item.normalized
        label = decision.get("label", "other")
        action = decision.get("action", "escalate")
        intent = decision.get("intent")
        conf = decision.get("confidence", 0.5)

        # --- Extract entities (только для важных типов) ---
        entities = {}
        if label not in ("noise", "chit-chat", "business-small") and self.brain.is_ready():
            chat_ctx = await asyncio.to_thread(
                self._get_chat_context, item.chat_thread_id
            )
            enriched_ctx = await asyncio.to_thread(
                self._enrich_extract_context, chat_ctx,
                item.contact_id, item.chat_thread_id
            )
            entities = await asyncio.to_thread(
                self.brain.extract_entities, nm.full_text, label, enriched_ctx
            )

        # --- Store memory + CHAOS (для бизнес-типов) ---
        if label in ("task", "decision", "lead-signal", "risk"):
            importance = 0.8 if label in ("decision", "risk") else 0.6
            content = f"[{label}] {nm.text[:200]}"

            if item.source == "business_edited":
                await asyncio.to_thread(
                    self._update_existing_memory,
                    item.message_id, content, item.content_hash, label, importance
                )
            else:
                await asyncio.to_thread(
                    self.brain.store_memory,
                    content=content, mem_type="episodic", importance=importance,
                    contact_id=item.contact_id, chat_thread_id=item.chat_thread_id,
                    source_message_id=item.message_id, content_hash=item.content_hash,
                    auto_associate=False,
                )
                await asyncio.to_thread(
                    self.brain.chaos_store,
                    content=nm.text[:200], category=label, priority=importance,
                    contact_id=item.contact_id,
                )

        # --- CRM upsert ---
        if entities and label not in ("noise", "chit-chat"):
            await asyncio.to_thread(
                crm_upsert, entities, item.contact_id,
                item.chat_thread_id, item.message_id, self.db_path
            )

        # --- Follow-up detection (для бизнес-типов) ---
        if label in ("task", "decision", "lead-signal", "risk", "business-small"):
            try:
                from services.response_engine import detect_followups
                chat_ctx = await asyncio.to_thread(
                    self._get_chat_context, item.chat_thread_id
                )
                followups = await asyncio.to_thread(
                    detect_followups, nm.full_text, chat_ctx, self.brain
                )
                for fu in followups:
                    await asyncio.to_thread(
                        _create_followup_task, fu, item.contact_id,
                        item.chat_thread_id, item.message_id, self.db_path
                    )
            except Exception as e:
                logger.warning(f"Follow-up detection error: {e}")

        # --- Mark analyzed ---
        await asyncio.to_thread(
            self._mark_analyzed, item.message_id, label
        )

        # --- Response handling ---
        if action == "respond_fact" and intent:
            await self._handle_fact_response(item, decision)
        elif action == "escalate":
            await self._handle_escalation(item, decision)
        # skip → ничего

        logger.info(
            f"Ingest: msg#{item.message_id} [{label}] action={action} "
            f"conf={conf:.2f} contact={item.contact_id}"
        )

    async def _handle_fact_response(self, item: PipelineItem, decision: dict):
        """Ответить клиенту фактами из БД. Template first, LLM fallback."""
        from services.response_engine import (
            response_guard, generate_fact_response, generate_fact_response_with_template,
            fast_dialog_ended,
        )
        from services.fact_queries import query_fact

        # Anti-spam check
        allowed, reason = response_guard.can_respond(item.chat_thread_id)
        if not allowed:
            logger.info(f"Response guard: {reason} for chat {item.chat_thread_id}")
            if reason == "max_consecutive":
                await self._handle_escalation(item, decision)
            return

        # Fast path: dialog ended
        if item.normalized and fast_dialog_ended(item.normalized.text):
            return

        # Query facts from DB
        intent = decision.get("intent", "")
        query_hint = decision.get("query_hint", "")
        facts = await asyncio.to_thread(
            query_fact, intent, query_hint,
            item.contact_id, item.chat_thread_id, self.db_path
        )

        if not facts:
            # Нет данных → escalate вместо выдумывания
            await self._handle_escalation(item, decision)
            return

        # Template first ($0 cost), LLM fallback
        response_text = generate_fact_response_with_template(intent, facts)
        if not response_text:
            contact_name = _get_contact_name_sync(item.contact_id, self.db_path)
            response_text = await asyncio.to_thread(
                generate_fact_response,
                item.normalized.text if item.normalized else "",
                facts, contact_name, self.brain
            )

        if not response_text:
            await self._handle_escalation(item, decision)
            return

        # Send response via business API
        biz_conn = await asyncio.to_thread(
            _get_active_biz_connection, self.db_path
        )
        if not biz_conn:
            await self._handle_escalation(item, decision)
            return

        chat_id = await asyncio.to_thread(
            _get_tg_chat_id, item.chat_thread_id, self.db_path
        )
        if not chat_id:
            return

        try:
            await self.bot.send_message(
                chat_id=int(chat_id),
                text=response_text,
                business_connection_id=biz_conn,
            )
            response_guard.record_response(item.chat_thread_id)
            logger.info(f"Fact response sent to chat {item.chat_thread_id}: {response_text[:60]}")
        except Exception as e:
            logger.error(f"Failed to send fact response: {e}")

    async def _handle_escalation(self, item: PipelineItem, decision: dict):
        """Эскалация → notifier (таймер → owner notification)."""
        try:
            from services.notifier import handle_escalation
            await handle_escalation(self.bot, item, decision, self.db_path)
        except Exception as e:
            logger.error(f"Escalation error: {e}")

    def _get_chat_context(self, chat_thread_id: int) -> str:
        """Контекст последних 5 сообщений из чата для классификации."""
        from services.response_context import build_chat_context
        return build_chat_context(chat_thread_id, self.db_path)

    def _enrich_extract_context(self, chat_ctx: str,
                                contact_id: int | None,
                                chat_thread_id: int | None) -> str:
        """
        Обогатить контекст для extract_entities.
        Добавляет существующую информацию о контакте, сделках и задачах.
        """
        from services.response_context import build_crm_context
        return build_crm_context(chat_ctx, contact_id, chat_thread_id, self.db_path)

    def _update_existing_memory(self, message_id: int, content: str,
                                content_hash: str, label: str, importance: float):
        """ISSUE-03: при re-process (edited) обновляем существующую memory, не создаём дубль."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT id FROM memories WHERE source_message_id = ?",
                (message_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE memories
                       SET content = ?, content_hash = ?, importance = ?,
                           last_accessed_at = datetime('now')
                       WHERE id = ?""",
                    (content, content_hash, importance, existing["id"])
                )
                # Обновляем chaos_entries привязанные к этой memory
                conn.execute(
                    "UPDATE chaos_entries SET content = ? WHERE memory_id = ?",
                    (content[:200], existing["id"])
                )
            else:
                # Нет existing memory — создаём как обычно
                conn.execute(
                    """INSERT INTO memories (content, content_hash, type, importance,
                                             strength, contact_id, chat_thread_id,
                                             source_message_id)
                       VALUES (?, ?, 'episodic', ?, 1.0, NULL, NULL, ?)""",
                    (content, content_hash, importance, message_id)
                )
                mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """INSERT INTO chaos_entries (content, category, priority, memory_id)
                       VALUES (?, ?, ?, ?)""",
                    (content[:200], label, importance, mid)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Update memory error: {e}")
        finally:
            conn.close()

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


# =========================================================
# MODULE-LEVEL HELPERS (для response handling)
# =========================================================

def _get_contact_name_sync(contact_id: int | None, db_path: str) -> str:
    if not contact_id:
        return "Клиент"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT name FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return row["name"] if row else "Клиент"
    finally:
        conn.close()


def _get_active_biz_connection(db_path: str) -> str | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT connection_id FROM business_connections
            WHERE status = 'active' AND can_reply = 1
            ORDER BY connected_at DESC LIMIT 1
        """).fetchone()
        return row["connection_id"] if row else None
    finally:
        conn.close()


def _get_tg_chat_id(chat_thread_id: int | None, db_path: str) -> str | None:
    if not chat_thread_id:
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT tg_chat_id FROM chat_threads WHERE id = ?", (chat_thread_id,)
        ).fetchone()
        return row["tg_chat_id"] if row else None
    finally:
        conn.close()


def _create_followup_task(fu: dict, contact_id: int | None,
                           chat_thread_id: int | None,
                           message_id: int | None, db_path: str):
    """Создать task из follow-up obligation."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Проверяем дедуп: нет ли уже такой задачи
        existing = conn.execute("""
            SELECT id FROM tasks
            WHERE description LIKE ? AND related_id = ?
              AND status NOT IN ('done', 'cancelled')
        """, (f"%{fu.get('obligation', '')[:30]}%", contact_id)).fetchone()

        if existing:
            return

        conn.execute("""
            INSERT INTO tasks (description, related_type, related_id, due_at,
                               priority, source_message_id)
            VALUES (?, ?, ?, ?, 'normal', ?)
        """, (
            fu.get("obligation", "Follow-up"),
            "contact" if contact_id else "other",
            contact_id,
            fu.get("due_date"),
            message_id,
        ))
        conn.commit()
        logger.info(f"Follow-up task created: {fu.get('obligation', '?')[:40]}")
    except Exception as e:
        logger.error(f"Create follow-up task error: {e}")
    finally:
        conn.close()
