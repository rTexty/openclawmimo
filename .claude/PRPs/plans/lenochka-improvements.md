# PRP Plan: Lenochka Improvements

**Created:** 2026-04-06
**Branch:** Antigravity
**Status:** in-progress

## Summary

7 улучшений для Lenochka CRM — надёжность, наблюдаемость, читаемость, тестирование, CRM-аналитика.

## Phases

### Phase 1 — Config Management [NEXT]
- Убрать хардкоды OWNER_ID, BOT_USERNAME, DB_PATH из run_pipeline.py
- Создать .env + .env.example
- Добавить python-dotenv в requirements
- Validation при старте

### Phase 2 — Dead-Letter Queue
- Таблица failed_messages в schemas/
- try/except на каждом шаге pipeline → INSERT failed_messages
- Вкладка в дашборде

### Phase 3 — Pipeline Observability
- Таблица pipeline_runs
- Декоратор @timed_stage
- API /api/pipeline-health
- Карточка в дашборде

### Phase 4 — Split mem.py + brain.py
- mem.py → memory/store.py, recall.py, vector.py, fts.py
- brain.py → brain/classify.py, embed.py, raptor.py
- Фасады для обратной совместимости

### Phase 5 — Test Coverage
- conftest.py с in-memory SQLite + mock LLM
- Тесты: routing gate, fast-skip, classify→store chain, DLQ
- pytest --cov порог 80%

### Phase 6 — Relationship Health Score
- contacts.health_score REAL
- recalc_health() после каждого ingest
- Дашборд: топ остывающих контактов

### Phase 7 — Proactive Follow-up Suggestions
- В heartbeat: SELECT contacts WHERE health_score < 0.3
- LLM черновик сообщения
- escalation с предложением

## Validation Commands

```bash
python3 -m pytest skills/lenochka-pipeline/test_pipeline.py -v
python3 -c "import mem; import brain; print('imports ok')"
python3 -m pytest --cov=skills/lenochka-pipeline --cov-report=term-missing
```
