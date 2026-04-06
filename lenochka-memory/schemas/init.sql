-- Lenochka Memory v2 — Единая схема (CRM + Agent Memory + CHAOS + Vectors)
-- SQLite + sqlite-vec

-- ============================================
-- СЛОЙ 1: CRM-БД
-- ============================================

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tg_username TEXT UNIQUE,
    tg_user_id TEXT UNIQUE,
    phones TEXT,          -- JSON array
    emails TEXT,          -- JSON array
    company_id INTEGER REFERENCES companies(id),
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    inn TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id TEXT NOT NULL,
    contact_id INTEGER REFERENCES contacts(id),
    type TEXT CHECK(type IN ('personal', 'group', 'channel', 'supergroup')),
    title TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_threads_tg ON chat_threads(tg_chat_id);
CREATE INDEX IF NOT EXISTS idx_chat_threads_contact ON chat_threads(contact_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_thread_id INTEGER NOT NULL REFERENCES chat_threads(id),
    from_user_id TEXT,
    text TEXT,
    sent_at DATETIME NOT NULL,
    classification TEXT CHECK(classification IN ('noise', 'chit-chat', 'business-small', 'task', 'decision', 'lead-signal', 'risk', 'other')),
    analyzed INTEGER DEFAULT 0,
    source_msg_id INTEGER,
    content_hash TEXT,
    meta_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_sent ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_class ON messages(classification);
CREATE INDEX IF NOT EXISTS idx_messages_analyzed ON messages(analyzed);
CREATE INDEX IF NOT EXISTS idx_messages_content_hash ON messages(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup ON messages(chat_thread_id, source_msg_id)
    WHERE source_msg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    source TEXT,
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'contacted', 'qualified', 'proposal', 'negotiation', 'won', 'lost')),
    amount REAL,
    probability REAL,
    owner_id TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_contact_status ON leads(contact_id, status);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    stage TEXT DEFAULT 'discovery' CHECK(stage IN ('discovery', 'proposal', 'negotiation', 'contract', 'closed_won', 'closed_lost')),
    amount REAL,
    expected_close_at DATE,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deals_contact_stage ON deals(contact_id, stage);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT,
    description TEXT NOT NULL,
    related_type TEXT CHECK(related_type IN ('contact', 'lead', 'deal', 'agreement', 'invoice', 'other')),
    related_id INTEGER,
    due_at DATETIME,
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'done', 'cancelled')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('low', 'normal', 'high', 'urgent')),
    source_message_id INTEGER REFERENCES messages(id),
    last_progress_check DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_related ON tasks(related_type, related_id);

CREATE TABLE IF NOT EXISTS agreements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    deal_id INTEGER REFERENCES deals(id),
    summary TEXT,
    amount REAL,
    due_at DATE,
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'sent', 'signed', 'completed', 'cancelled')),
    source_message_id INTEGER REFERENCES messages(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id INTEGER NOT NULL REFERENCES agreements(id),
    amount REAL NOT NULL,
    due_at DATE,
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'sent', 'paid', 'overdue', 'cancelled')),
    issued_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount REAL NOT NULL,
    paid_at DATETIME,
    method TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'failed')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- СЛОЙ 1.5: TELEGRAM BUSINESS CONNECTIONS
-- ============================================

CREATE TABLE IF NOT EXISTS business_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'revoked')),
    can_reply INTEGER DEFAULT 0,
    can_read_messages INTEGER DEFAULT 1,
    connected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    revoked_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_biz_conn_owner ON business_connections(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_biz_conn_status ON business_connections(status);

-- ============================================
-- СЛОЙ 2: AGENT MEMORY (Когнитивная память)
-- ============================================

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    content_hash TEXT,
    type TEXT NOT NULL CHECK(type IN ('episodic', 'semantic', 'procedural')),
    category TEXT DEFAULT 'other' CHECK(category IN ('decision', 'risk', 'policy', 'fact', 'event', 'task', 'lead-signal', 'other')),
    importance REAL DEFAULT 0.5 CHECK(importance >= 0 AND importance <= 1),
    strength REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    tenant_id TEXT,
    contact_id INTEGER REFERENCES contacts(id),
    chat_thread_id INTEGER REFERENCES chat_threads(id),
    deal_id INTEGER REFERENCES deals(id),
    source_message_id INTEGER REFERENCES messages(id),
    tags TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_contact ON memories(contact_id);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
CREATE INDEX IF NOT EXISTS idx_memories_strength ON memories(strength);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_source_msg ON memories(source_message_id);

CREATE TABLE IF NOT EXISTS associations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id_from INTEGER NOT NULL REFERENCES memories(id),
    memory_id_to INTEGER NOT NULL REFERENCES memories(id),
    relation_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_assoc_from ON associations(memory_id_from);
CREATE INDEX IF NOT EXISTS idx_assoc_to ON associations(memory_id_to);

CREATE TABLE IF NOT EXISTS raptor_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    parent_id INTEGER REFERENCES raptor_nodes(id),
    memory_ids TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_raptor_level ON raptor_nodes(level);
CREATE INDEX IF NOT EXISTS idx_raptor_parent ON raptor_nodes(parent_id);

-- ============================================
-- СЛОЙ 2.5: VECTOR EMBEDDINGS (sqlite-vec)
-- ============================================

-- Векторная таблица для семантического поиска memories
-- Создаётся программно через sqlite-vec при init:
-- CREATE VIRTUAL TABLE vec_memories USING vec0(embedding float[384])

-- ============================================
-- FTS для memories (быстрый поиск по контенту + category)
-- ============================================

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    category,
    content=memories,
    content_rowid=id,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
    INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
END;

-- ============================================
-- VIEWS
-- ============================================

CREATE VIEW IF NOT EXISTS v_active_deals AS
SELECT d.id, d.stage, d.amount, d.expected_close_at,
       c.name as contact_name, c.tg_username,
       l.source as lead_source
FROM deals d
JOIN contacts c ON d.contact_id = c.id
LEFT JOIN leads l ON d.lead_id = l.id
WHERE d.stage NOT IN ('closed_won', 'closed_lost');

CREATE VIEW IF NOT EXISTS v_overdue_tasks AS
SELECT t.*, c.name as contact_name
FROM tasks t
LEFT JOIN contacts c ON t.related_type = 'contact' AND t.related_id = c.id
WHERE t.due_at < datetime('now')
  AND t.status NOT IN ('done', 'cancelled');

CREATE VIEW IF NOT EXISTS v_abandoned_dialogues AS
SELECT ct.id as chat_thread_id, ct.title, ct.tg_chat_id,
       c.name as contact_name, c.tg_username,
       MAX(m.sent_at) as last_message_at,
       (julianday('now') - julianday(MAX(m.sent_at))) * 24 as hours_since
FROM chat_threads ct
JOIN messages m ON m.chat_thread_id = ct.id
LEFT JOIN contacts c ON ct.contact_id = c.id
WHERE m.from_user_id != 'self'
GROUP BY ct.id
HAVING hours_since > 24;

-- ============================================
-- ПРОАКТИВНЫЙ ДВИЖОК: pending notifications
-- ============================================

CREATE TABLE IF NOT EXISTS pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_thread_id INTEGER REFERENCES chat_threads(id),
    contact_id INTEGER REFERENCES contacts(id),
    message_id INTEGER REFERENCES messages(id),
    message_text TEXT,
    entity_type TEXT,           -- 'escalation', 'owner_task', 'owner_agreement', 'owner_deal', 'owner_invoice', 'client_invoice', 'client_agreement', 'client_task', 'checkin'
    entity_id INTEGER,          -- id в соответствующей таблице (tasks.id, agreements.id, etc.)
    escalation_type TEXT,       -- 'pricing', 'proposal', 'contract', 'meeting', 'complaint', 'other', 'task_due', etc.
    notify_at DATETIME NOT NULL,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'cancelled')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pending_notify_status ON pending_notifications(status, notify_at);
CREATE INDEX IF NOT EXISTS idx_pending_notify_entity ON pending_notifications(entity_type, entity_id);

-- ============================================
-- PROGRESS CHECK-IN tracking
-- ============================================

-- Добавляем в tasks: когда последний раз проверяли прогресс
-- (через ALTER при миграции, здесь для новых БД)
-- last_progress_check уже есть в schema v2 через миграцию

-- ============================================
-- LOAD SESSIONS: импорт истории чатов
-- ============================================

CREATE TABLE IF NOT EXISTS load_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting_files',
    files_path TEXT,
    messages_count INTEGER DEFAULT 0,
    contact_id INTEGER REFERENCES contacts(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_load_sessions_status ON load_sessions(status);
CREATE INDEX IF NOT EXISTS idx_load_sessions_chat ON load_sessions(chat_id, owner_id);

-- ============================================
-- Dead-Letter Queue
-- ============================================

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
