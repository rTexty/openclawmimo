from mem._config import DB_PATH, SCHEMA_PATH, SCHEMA_VERSION, EMBEDDING_DIM
from mem._db import get_db, _load_vec


def init():
    """Инициализация базы данных."""
    conn = get_db()

    if not DB_PATH.exists():
        # Новая БД — создаём полную схему
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)

        # Создать векторные таблицы через sqlite-vec
        if _load_vec(conn):
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[{EMBEDDING_DIM}])"
            )
            print("✅ Векторные таблицы (sqlite-vec) созданы")
        else:
            print("⚠️ sqlite-vec недоступен — векторный поиск отключён")

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        conn.close()
        print(f"✅ БД создана: {DB_PATH}")
    else:
        # БД существует — проверяем миграции
        _migrate_db(conn)
        conn.close()


def _migrate_db(conn):
    """Миграции схемы по PRAGMA user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current >= SCHEMA_VERSION:
        print(f"✅ Схема актуальная (v{current})")
        return

    print(f"🔄 Миграция: v{current} → v{SCHEMA_VERSION}")

    if current < 2:
        # v2: content_hash в messages, tg_user_id в contacts
        _migrate_v2(conn)

    if current < 4:
        # v4: last_progress_check в tasks
        _migrate_v4(conn)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    print(f"✅ Миграция завершена → v{SCHEMA_VERSION}")


def _migrate_v2(conn):
    """v1 → v2: content_hash + tg_user_id."""
    # messages.content_hash
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_content_hash ON messages(content_hash)"
        )
        print("   + messages.content_hash")

    # contacts.tg_user_id
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
    if "tg_user_id" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN tg_user_id TEXT UNIQUE")
        print("   + contacts.tg_user_id")
    print(f"✅ БД создана: {DB_PATH}")


def _migrate_v4(conn):
    """v3 → v4: last_progress_check в tasks."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "last_progress_check" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_progress_check DATETIME")
        print("   + tasks.last_progress_check")
