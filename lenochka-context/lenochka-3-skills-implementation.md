# Skills Lenochka

## 1. lenochka-stream-triage
- Вход: batch нормализованных сообщений
- Классификация: noise / chit-chat / business-small / task / decision / lead-signal / risk
- Экстракция сущностей (contacts, leads, deals, tasks, agreements)
- Маппинг в CRM-БД (upsert, идемпотентность)
- Capture в память (mem.py store + chaos-cli store)

## 2. lenochka-reflection-briefings
- Фоновая рефлексия по CRM и памяти
- Секции: новые лиды, брошенные диалоги, просроченные задачи, ключевые договорённости/риски
- Расписание: daily 08:00, weekly Sunday 18:00

## 3. lenochka-dynamic-context
- Подбор контекста перед ответом LLM
- Режимы: core (pinned facts), search (keyword), recall (deep history)
- Источники: CRM-БД + Agent Memory Ultimate + CHAOS
- Выход: структурированный context-блок (facts, episodes, related, notes)
