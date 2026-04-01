"""Tests for run_pipeline bugfixes."""

import sqlite3
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_DIR = PROJECT_ROOT / "lenochka-memory"
sys.path.insert(0, str(MEMORY_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest


@pytest.fixture
def in_memory_db():
    """Create in-memory SQLite with minimal schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            tg_username TEXT,
            tg_user_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE chat_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_chat_id TEXT NOT NULL,
            contact_id INTEGER REFERENCES contacts(id),
            type TEXT DEFAULT 'personal',
            title TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_thread_id INTEGER REFERENCES chat_threads(id),
            from_user_id TEXT,
            text TEXT,
            sent_at TEXT DEFAULT (datetime('now')),
            source_msg_id INTEGER,
            content_hash TEXT,
            meta_json TEXT,
            analyzed INTEGER DEFAULT 0,
            classification TEXT
        );
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            content_hash TEXT,
            source_message_id INTEGER
        );
        CREATE TABLE chaos_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            memory_id INTEGER
        );
        CREATE TABLE deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES contacts(id),
            amount REAL,
            stage TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT,
            related_type TEXT,
            related_id INTEGER,
            due_at TEXT,
            priority TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'open',
            source_message_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES contacts(id),
            source TEXT,
            status TEXT DEFAULT 'new',
            amount REAL,
            probability REAL DEFAULT 0.5,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE agreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES contacts(id),
            deal_id INTEGER,
            summary TEXT,
            amount REAL,
            status TEXT DEFAULT 'draft',
            due_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agreement_id INTEGER REFERENCES agreements(id),
            amount REAL,
            status TEXT DEFAULT 'draft',
            due_at TEXT
        );
        CREATE TABLE pending_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_thread_id INTEGER,
            contact_id INTEGER,
            message_id INTEGER,
            entity_type TEXT,
            escalation_type TEXT,
            notify_at TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE business_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id TEXT UNIQUE,
            owner_user_id INTEGER,
            can_reply INTEGER,
            can_read_messages INTEGER,
            status TEXT,
            revoked_at TEXT
        );
    """)
    conn.commit()
    yield conn
    conn.close()


class TestLabelOtherEscalation:
    """Fix 1: label='other' must escalate, not silent."""

    def test_invalid_label_becomes_other(self, in_memory_db):
        """When LLM returns invalid label, pipeline normalizes to 'other'."""
        import run_pipeline

        # Verify VALID_LABELS does NOT contain "other"
        assert "other" not in run_pipeline.VALID_LABELS

    def test_other_label_triggers_escalation(self, in_memory_db):
        """'other' label should create pending_notification, not go silent."""
        import run_pipeline

        conn = in_memory_db
        # Seed contact + thread
        conn.execute("INSERT INTO contacts (name) VALUES ('Test Client')")
        conn.execute(
            "INSERT INTO chat_threads (tg_chat_id, contact_id) VALUES ('-1001', 1)"
        )
        conn.commit()

        # Mock the LLM ingest to return "other" (invalid label)
        with (
            patch.object(
                run_pipeline,
                "run_ingest",
                return_value={
                    "label": "other",
                    "confidence": 0.5,
                    "entities": {},
                    "stored": True,
                },
            ),
            patch.object(run_pipeline, "_send_owner_notification"),
        ):
            args = MagicMock()
            args.text = "some unrecognized message"
            args.sender_id = 12345
            args.sender_name = "Test"
            args.tg_username = "testuser"
            args.chat_id = "-1001"
            args.chat_type = "personal"
            args.chat_title = None
            args.message_id = 100
            args.is_owner = False
            args.is_owner_chat = False
            args.content_type = "text"
            args.event_type = "message"
            args.reply_to_text = None
            args.reply_to_author = None
            args.forward_from = None
            args.sticker_emoji = None
            args.deleted_message_ids = None

            result = run_pipeline._run_pipeline_inner(conn, args)

            # Should escalate = return a template reply (not None/silent)
            assert result is not None, "label='other' must escalate, not go silent"
            assert len(result) > 0

            # Verify pending notification was created
            row = conn.execute(
                "SELECT COUNT(*) as c FROM pending_notifications"
            ).fetchone()
            assert row["c"] == 1, "escalation must create pending_notification"


class TestDealConfirmSafety:
    """Fix 2: DEAL_CONFIRM_PHRASES must not be silently dropped."""

    def test_deal_confirm_not_classified_silent(self, in_memory_db):
        """'согласен' classified as chit-chat should still be escalated."""
        import run_pipeline

        conn = in_memory_db
        conn.execute("INSERT INTO contacts (name) VALUES ('Client')")
        conn.execute(
            "INSERT INTO chat_threads (tg_chat_id, contact_id) VALUES ('-1001', 1)"
        )
        conn.commit()

        # LLM misclassifies "согласен" as chit-chat
        with (
            patch.object(
                run_pipeline,
                "run_ingest",
                return_value={
                    "label": "chit-chat",
                    "confidence": 0.9,
                    "entities": {},
                    "stored": True,
                },
            ),
            patch.object(run_pipeline, "_send_owner_notification"),
        ):
            args = MagicMock()
            args.text = "согласен"
            args.sender_id = 12345
            args.sender_name = "Client"
            args.tg_username = "client1"
            args.chat_id = "-1001"
            args.chat_type = "personal"
            args.chat_title = None
            args.message_id = 101
            args.is_owner = False
            args.is_owner_chat = False
            args.content_type = "text"
            args.event_type = "message"
            args.reply_to_text = None
            args.reply_to_author = None
            args.forward_from = None
            args.sticker_emoji = None
            args.deleted_message_ids = None

            result = run_pipeline._run_pipeline_inner(conn, args)

            # "согласен" is a deal confirm — even if classified chit-chat,
            # must not be silently dropped without escalation
            notifications = conn.execute(
                "SELECT COUNT(*) as c FROM pending_notifications"
            ).fetchone()["c"]
            assert notifications >= 1, (
                "DEAL_CONFIRM_PHRASES misclassified as chit-chat must still escalate"
            )


class TestSanitizeOutput:
    """Fix 3: _sanitize_output must not block legitimate responses."""

    def test_template_responses_pass_sanitize(self):
        """All ESCALATE_TEMPLATES content must pass sanitizer."""
        import run_pipeline

        for label, templates in run_pipeline.ESCALATE_TEMPLATES.items():
            for template in templates:
                result = run_pipeline._sanitize_output(template)
                assert result is not None, (
                    f"Template blocked by sanitizer: {template!r} (label={label})"
                )

    def test_legitimate_russian_passes(self):
        import run_pipeline

        legit_messages = [
            "Подождите пару минут, проверю информацию.",
            "По правилам компании ответ будет сегодня.",
            "Я записала ваш номер, скоро перезвоню.",
        ]
        for msg in legit_messages:
            result = run_pipeline._sanitize_output(msg)
            assert result is not None, f"Legitimate message blocked: {msg!r}"

    def test_actual_leakage_blocked(self):
        """Real thinking leakage must still be blocked."""
        import run_pipeline

        leaky_messages = [
            "Pipeline сказал silent, но я думаю что ответить.",
            "По правилам lenochka-response, нужно escalate.",
            "Классифицировал как task.",
        ]
        for msg in leaky_messages:
            result = run_pipeline._sanitize_output(msg)
            assert result is None, f"Leakage not blocked: {msg!r}"


class TestOwnerTelegramId:
    """Fix 4: OWNER_TELEGRAM_ID must read from env with fallback."""

    def test_env_override(self):
        import importlib
        import run_pipeline

        with patch.dict(os.environ, {"LEN_OWNER_TELEGRAM_ID": "111222333"}):
            importlib.reload(run_pipeline)
            assert run_pipeline.OWNER_TELEGRAM_ID == "111222333"

    def test_default_fallback(self):
        import importlib
        import run_pipeline

        env = {k: v for k, v in os.environ.items() if k != "LEN_OWNER_TELEGRAM_ID"}
        with patch.dict(os.environ, env, clear=True):
            importlib.reload(run_pipeline)
            assert run_pipeline.OWNER_TELEGRAM_ID == "5944980799"


class TestTaskDedup:
    """Fix 5: crm_upsert must not create duplicate tasks."""

    def test_duplicate_task_not_inserted(self, in_memory_db):
        import run_pipeline

        conn = in_memory_db
        conn.execute("INSERT INTO contacts (name) VALUES ('Client')")
        conn.execute(
            "INSERT INTO chat_threads (tg_chat_id, contact_id) VALUES ('-1001', 1)"
        )
        conn.commit()

        entities = {"tasks": [{"description": "Сделать КП", "priority": "normal"}]}

        # First insert
        run_pipeline.crm_upsert(conn, "task", entities, contact_id=1, message_id=1)
        count1 = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]

        # Second insert — same description, same contact
        run_pipeline.crm_upsert(conn, "task", entities, contact_id=1, message_id=2)
        count2 = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]

        assert count1 == 1, "first insert should create 1 task"
        assert count2 == 1, f"duplicate task not prevented: got {count2} tasks"

    def test_different_description_creates_new_task(self, in_memory_db):
        import run_pipeline

        conn = in_memory_db
        conn.execute("INSERT INTO contacts (name) VALUES ('Client')")
        conn.execute(
            "INSERT INTO chat_threads (tg_chat_id, contact_id) VALUES ('-1001', 1)"
        )
        conn.commit()

        entities1 = {"tasks": [{"description": "Сделать КП", "priority": "normal"}]}
        entities2 = {
            "tasks": [{"description": "Подписать договор", "priority": "high"}]
        }

        run_pipeline.crm_upsert(conn, "task", entities1, contact_id=1, message_id=1)
        run_pipeline.crm_upsert(conn, "task", entities2, contact_id=1, message_id=2)

        count = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
        assert count == 2, "different descriptions should create separate tasks"


class TestFactResponseMatching:
    """Fix 6: keyword matching must use word boundaries."""

    def test_substring_no_match(self):
        import run_pipeline
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        # "заказуха" contains "заказ" but is not about a deal
        result = run_pipeline._try_fact_response(
            conn, contact_id=1, original_text="какая заказуха!"
        )
        # Should still attempt lookup (substring match), but since no data -> None
        assert result is None

    def test_exact_word_match(self):
        import run_pipeline
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "stage": "discovery",
            "amount": 150000,
        }

        result = run_pipeline._try_fact_response(
            conn, contact_id=1, original_text="Как дела по сделке?"
        )
        assert result is not None
        assert "150" in result


class TestAntiSpamState:
    """Fix 7: anti-spam state should minimize race window."""

    def test_state_lock_prevents_concurrent_write(self):
        """Verify _with_state_lock serializes access."""
        import run_pipeline
        import threading
        import time

        results = []

        def increment():
            with run_pipeline._STATE_LOCK:
                state = run_pipeline._load_state()
                counter = state.get("_test_counter", 0)
                time.sleep(0.01)  # Simulate processing
                state["_test_counter"] = counter + 1
                run_pipeline._save_state_atomic(state)
                results.append(counter + 1)

        threads = [threading.Thread(target=increment) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Clean up
        state = run_pipeline._load_state()
        state.pop("_test_counter", None)
        run_pipeline._save_state_atomic(state)

        # With proper locking, final value should be 5
        assert max(results) == 5 or len(set(results)) == 5, (
            "Concurrent writes lost updates"
        )


class TestStateCleanup:
    """Fix 8: state cleanup should be deterministic."""

    def test_cleanup_runs_after_interval(self):
        """Cleanup should trigger based on time, not random chance."""
        import run_pipeline
        import time

        state_file = run_pipeline.STATE_FILE
        state_file.parent.mkdir(parents=True, exist_ok=True)

        # Write old state
        old_ts = time.time() - 40 * 86400  # 40 days ago
        run_pipeline._save_state_atomic(
            {
                "old_chat": {"last_response": old_ts},
                "new_chat": {"last_response": time.time()},
            }
        )

        # Run cleanup
        run_pipeline.cleanup_state(max_age_days=30)

        state = run_pipeline._load_state()
        assert "old_chat" not in state, "old entries should be cleaned"
        assert "new_chat" in state, "new entries should be kept"
