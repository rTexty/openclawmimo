# MEMORY.md — Long-Term Memory

## Project: Lenochka

**Что это:** Персональный AI-ассистент в Telegram, работающий как невидимая CRM поверх мессенджера.

**Владелец:** Камиль (GMT+8), AI архитектор в стартапе.

**Стек:** Python 3.12 / Aiogram 3.26 / SQLite+WAL / sentence-transformers (all-MiniLM-L6-v2, 384-dim) / sqlite-vec 0.1.7 / LLM: MiMo V2 Pro (OpenAI-совместимый)

**Репозиторий:** https://github.com/rTexty/openclawmimo

### Архитектура (3 слоя памяти)

1. **CRM-БД (SQLite):** contacts, companies, chat_threads, messages, leads, deals, tasks, agreements, invoices, payments, business_connections, pending_notifications
2. **Agent Memory:** memories + vec_memories (sqlite-vec) + associations + raptor_nodes + memories_fts (FTS5 trigram)
3. **CHAOS Memory:** chaos_entries + vec_chaos + chaos_fts (FTS5 trigram)

### Основные модули

- **lenochka-memory/mem.py** (1518 строк) — CLI: store, recall, ingest, chaos, context, digest, consolidate, /find
- **lenochka-memory/brain.py** (1213 строк) — classify, classify_batch, extract_entities, embed_text, build_context_packet, _call_llm
- **lenochka-bot/** (26 файлов, ~4000 строк) — Telegram бот:
  - handlers: business.py (Business API), commands.py (/status, /leads, /tasks, /find, /digest, /weekly), errors.py
  - services: pipeline.py (async ingest), brain_wrapper.py (daemon mode), normalizer.py, crm_upsert.py, response_engine.py, fact_queries.py, notifier.py, proactive.py, progress.py, response_context.py, scheduler.py, memory.py, digest.py, contact_resolver.py
  - middlewares: owner.py, throttling.py, logging.py
  - config.py (pydantic-settings, LEN_ prefix)

### Pipeline Flow
```
message → normalize → dedup → store_message → combined classify+route (1 LLM)
→ extract_entities (+ entity context) → store_memory + chaos_store
→ crm_upsert (contacts/deals/tasks/leads) → response handling
  ├─ respond_fact → SQL query → LLM response → send via business API
  ├─ escalate → pending notification → 30min timer → owner alert
  └─ skip → молча ingest
```

### Scheduler (GMT+8)
- 03:00 consolidate, 08:00 daily digest, 08:30 proactive owner, 09:00 client reminders, 10:00 progress check-in, */4h abandoned, Sun 18:00 weekly

### Key Design Decisions
1. Business API > Userbot (легально)
2. Combined classify+route = primary (1 LLM вместо 2, -90% cost)
3. Fact-based response: SQL для фактов, LLM только для понимания + формулировки
4. Escalation > direct response: сложные вопросы → owner'у
5. Entity expansion (FK traversal) > graph RAG
6. Brain wrapper (daemon) > CLI calls (нет cold start 6.6с)
7. All messages saved (включая свои), anti-loop only at response level
8. ~$10/мес LLM cost при 500 msg/day

### Readiness Assessment
- Ядро (ingest + память + CRM + classify + extract + digest): ~90%
- Продукт (response engine + proactive + follow-up): ~40%
- MVP = "CRM-дневник" работает, но бот не отвечает клиентам на сложные вопросы

### What's NOT done
- End-to-end тест с реальным .env и Telegram
- Voice transcription / OCR
- Multi-user изоляция
- Integration tests
- Webhook mode не тестирован с реальным сервером

### Сессии (история)
- 03-29 16:21-17:08: mem.py v2, brain.py v2, init.sql, аудит
- 03-29 23:02-00:00: Telegram бот (14 файлов), архитектура
- 03-30 00:14-01:04: 11 фиксов (schema, batch classify, consolidate vec ANN, webhook, supersede)
- 03-30 02:16-02:59: 6 критических фиксов, entity expansion, sqlite-vec + sentence-transformers
- 03-30 03:09-04:01: аудит 16 issues, все resolved
- 03-30 04:07-04:35: Response Engine v3 architecture
- 03-30 14:50-15:07: Response Engine audit + bug fixes
- 03-30 15:41-16:35: 5 gaps fixed + 5 issues fixed (aggregate notify, memories FTS, progress.py, response_context.py, owner/group/CRM context/night mode/last_progress_check)
- 03-30 16:57: THIS SESSION — cloned repo, deep study
