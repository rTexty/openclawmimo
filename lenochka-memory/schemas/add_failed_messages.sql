-- Migration: Dead-Letter Queue для упавших сообщений пайплайна
-- Запускать: sqlite3 lenochka.db < add_failed_messages.sql

CREATE TABLE IF NOT EXISTS failed_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_payload TEXT,           -- JSON с аргументами пайплайна
    stage       TEXT NOT NULL,  -- на каком шаге упало: normalize/ingest/store/crm/response
    error       TEXT NOT NULL,  -- текст исключения
    retry_count INTEGER DEFAULT 0,
    resolved    INTEGER DEFAULT 0,  -- 1 = разобрано вручную
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_failed_messages_resolved
    ON failed_messages(resolved, created_at);
