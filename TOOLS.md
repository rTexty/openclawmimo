# TOOLS.md — Локальные заметки

## Database

- Путь: `/root/.openclaw/workspace/lenochka-memory/db/lenochka.db`
- Schema: `/root/.openclaw/workspace/lenochka-memory/schemas/init.sql`
- Версия схемы: v4 (PRAGMA user_version = 4)

## Skills (как вызывать)

### lenochka-memory
```bash
python3 /root/.openclaw/workspace/skills/lenochka-memory/run_memory.py store --text "..." --importance 0.8 --label "decision"
python3 /root/.openclaw/workspace/skills/lenochka-memory/run_memory.py recall --query "..." --limit 5
```

### lenochka-crm
```bash
python3 /root/.openclaw/workspace/skills/lenochka-crm/run_crm.py deal --contact_id 42 --amount 150000 --stage closed_won
python3 /root/.openclaw/workspace/skills/lenochka-crm/run_crm.py task --contact_id 42 --description "..." --due_date "2026-04-01" --priority normal
```

### Прямой SQL
```bash
sqlite3 /root/.openclaw/workspace/lenochka-memory/db/lenochka.db "SELECT * FROM v_overdue_tasks"
```

## LLM Config

- Берётся из env: `LEN_LLM_BASE_URL`, `LEN_LLM_API_KEY`, `LEN_LLM_MODEL`
- Fallback: `LENOCHKA_LLM_*` (обратная совместимость)
- Модель: `mimo-v2-pro` по умолчанию

## Embeddings

- Основной: sentence-transformers all-MiniLM-L6-v2 (384-dim)
- Fallback: char 3-gram TF hash (детерминированный через SHA-256)
- Хранение: sqlite-vec (vec_memories, vec_chaos)
