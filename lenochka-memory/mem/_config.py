from pathlib import Path

# === CONFIG ===
DB_DIR = Path(__file__).parent.parent / "db"
DB_PATH = DB_DIR / "lenochka.db"
SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "init.sql"
EMBEDDING_DIM = 384

# Current schema version — increment on every schema change
SCHEMA_VERSION = 5
