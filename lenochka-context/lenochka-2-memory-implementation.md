# Реализация памяти Lenochka (Agent Memory Ultimate + CHAOS + CRM)

## Архитектура трёх слоёв

1. **CRM-БД** (PostgreSQL/SQLite) — источник истины
2. **Agent Memory Ultimate** — когнитивная память (episodic/semantic/procedural + RAPTOR)
3. **CHAOS Memory** — гибридный поиск (BM25 + вектора + граф + heat)

## CRM-БД таблицы

contacts, companies, chat_threads, messages, leads, deals, tasks, agreements, invoices, payments

## Протоколы

### Capture
- noise/chit-chat → только в messages
- task/decision/lead-signal/risk → CRM-БД + episodic memory + CHAOS

### Recall
- CRM-БД → Agent Memory Ultimate → CHAOS → слияние → контекст для LLM

### Maintenance
- Ночная консолидация (decay, cluster, merge, RAPTOR rebuild)
- Ретеншн-политика для сырых сообщений

## Стратифицированное хранение

- 0–7 дней: полные сообщения + все события
- 1–4 недели: daily summary
- 1–6 месяцев: weekly summary + агрегаты
- 6+ месяцев: monthly/quarterly + RAPTOR L2/L3
