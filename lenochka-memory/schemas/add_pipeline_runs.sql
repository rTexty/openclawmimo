-- Migration: Pipeline Observability — трекинг latency по шагам
-- Запускать: sqlite3 lenochka.db < add_pipeline_runs.sql

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage       TEXT NOT NULL,       -- normalize / routing / store / ingest / crm / response
    status      TEXT NOT NULL,       -- ok / error / skip
    duration_ms INTEGER NOT NULL,
    message_id  INTEGER,             -- source_msg_id если известен
    error       TEXT,                -- краткий текст ошибки при status=error
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_stage
    ON pipeline_runs(stage, created_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created
    ON pipeline_runs(created_at);
