# MEMORY.md — Долгосрочная память

## Проект: Lenochka

Персональный AI-ассистент в Telegram. Невидимая CRM поверх мессенджера.

**Владелец:** Камиль (GMT+8), AI архитектор в стартапе.
**Репозиторий:** https://github.com/rTexty/openclawmimo (branch: Antigravity)

### Архитектура

Я (OpenClaw agent = Леночка) работаю через Telegram channels. Мои skills — инструменты для работы с данными:

- **lenochka-memory** (mem.py + brain.py) — запись, поиск, классификация, эмбеддинги
- **lenochka-crm** (run_crm.py) — deals и tasks
- **lenochka-response** (SKILL.md) — правила ответов и эскалации

База данных: SQLite 15 таблиц (CRM + Agent Memory + CHAOS), sqlite-vec для векторного поиска, FTS5 trigram для полнотекстового.

### Стек

Python 3.12, SQLite+WAL, sqlite-vec 0.1.7, sentence-transformers (all-MiniLM-L6-v2, 384-dim), LLM: MiMo V2 Pro (OpenAI-совместимый)

### Ключевые решения

1. Skills-архитектура > монолитный бот — модульность, гибкость
2. OpenClaw channels > самописный бот — готовая инфраструктура
3. Entity expansion (FK traversal) > graph RAG — 80% пользы при 5% сложности
4. Combined classify+route — 1 LLM-вызов вместо 2, -90% cost
5. Fact-based response: SQL для фактов, LLM для формулировки
6. Escalation > direct response для сложных вопросов
7. Fast skip для "ок/согласен" — бесплатно, без LLM
8. ~$10/мес LLM cost при 500 msg/day

### Готовность

- Ядро (память + CRM + classify + extract): ~90%
- Интеграция (Telegram channel + proactive): ~30%
- Продукт (response engine + proactive): ~40%

### История

- 03-29: mem.py v2, brain.py v2, схема SQL, аудит
- 03-29: Telegram бот (14 файлов), архитектура
- 03-30: 20+ фиксов, response engine v3, proactive engine
- 03-31: Переход на skills-архитектуру + OpenClaw channels
