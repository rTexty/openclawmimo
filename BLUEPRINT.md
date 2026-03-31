# BLUEPRINT — Lenochka AI (Карта проекта)

> **Статус:** Переход на Skills-архитектуру + OpenClaw channels
> **Последнее обновление:** 2026-03-31

---

## Что такое Lenochka

Персональный AI-ассистент в Telegram. Невидимая CRM. Пользователь живёт в мессенджере, CRM строится автоматически.

---

## Архитектура (текущая)

### Ядро (готово, работает)

| Компонент | Файл | Статус |
|-----------|------|--------|
| CRM Schema | `lenochka-memory/schemas/init.sql` | ✅ 15 таблиц + FTS5 + vec |
| Memory CLI | `lenochka-memory/mem.py` | ✅ store, recall, ingest, chaos, consolidate |
| Brain | `lenochka-memory/brain.py` | ✅ classify, extract, embed, RAPTOR |
| Skill: Memory | `skills/lenochka-memory/` | ✅ store + recall wrappers |
| Skill: CRM | `skills/lenochka-crm/` | ✅ deal + task wrappers |
| Skill: Response | `skills/lenochka-response/` | ✅ Правила ответов |

### Интеграция (настраивается)

| Компонент | Статус |
|-----------|--------|
| Telegram Business API → OpenClaw channel | ⏳ Настраивается |
| OpenClaw agent → skills pipeline | ⏳ Настраивается |
| Proactive reminders (cron) | ⏳ Настраивается |

---

## Pipeline (как я обрабатываю сообщения)

```
Telegram message
  → OpenClaw channel (получаю текст)
  → Нормализация (извлекаю текст из любого типа)
  → Dedup (content_hash + source_message_id)
  → Classify (LLM: noise/chit-chat/task/decision/lead-signal/risk)
  → Extract (LLM: contacts, amounts, dates, tasks)
  → Store (memories + CHAOS)
  → CRM upsert (contacts, deals, tasks, leads)
  → Response decision (skip / respond_fact / escalate)
```

---

## Scheduler (ежедневные задачи)

| Время (GMT+8) | Что | Кому |
|---------------|-----|------|
| 03:00 | Consolidate (decay + merge + RAPTOR) | Фон |
| 08:00 | Утренний дайджест | Owner |
| 08:30 | Proactive owner alerts | Owner |
| 09:00 | Client reminders | Клиент (biz API) |
| 10:00 | Progress check-in | Owner |
| */4ч | Abandoned dialogues | Owner |
| Sun 18:00 | Weekly report | Owner |

---

## Стоимость

| Компонент | $/мес |
|-----------|-------|
| LLM (MiMo V2 Pro, 500 msg/day) | ~$10 |
| SQLite + sentence-transformers | $0 |
| OpenClaw | $0 |

---

## Что работает (проверено в предыдущих сессиях)

- ✅ All 16 issues resolved (audit)
- ✅ 25+ .py файлов компилируются
- ✅ SQLite schema: 15 таблиц + FTS5 + vec + triggers
- ✅ mem.py: init, store, recall, ingest, chaos, consolidate — всё работает
- ✅ brain.py: classify, extract, embed, RAPTOR, context packets
- ✅ Entity expansion: FK-traversal chain
- ✅ Transactional store: try/rollback
- ✅ Vec ANN consolidate: O(n·k) вместо O(n²)
- ✅ Batch classify + batch embed
- ✅ Supersede cascade для edited messages

---

## Что нужно сделать

### Phase 1: Подключение (сейчас)
- [ ] Настроить Telegram channel в OpenClaw
- [ ] Настроить .env с BOT_TOKEN и LLM credentials
- [ ] End-to-end тест: сообщение → ingest → CRM → ответ

### Phase 2: Production
- [ ] Voice transcription (Whisper/groq)
- [ ] OCR для фото
- [ ] Aggregate notifications
- [ ] Integration tests

### Phase 3: Scale
- [ ] Multi-user изоляция
- [ ] Webhook mode
- [ ] Monitoring / metrics
