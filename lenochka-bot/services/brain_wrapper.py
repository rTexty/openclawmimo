"""
Brain Wrapper — daemon-mode обёртка над brain.py и mem.py.
Модель эмбеддингов загружается ОДИН раз, живёт в памяти.
Нет холодного старта 6.6с на каждый вызов.
"""
import sys
import sqlite3
import logging
import hashlib
import json
from pathlib import Path

logger = logging.getLogger("lenochka.brain")

# Добавляем lenochka-memory в path
_BRAIN_DIR = Path(__file__).resolve().parent.parent / "lenochka-memory"
sys.path.insert(0, str(_BRAIN_DIR))


class BrainWrapper:
    """Ленивая инициализация brain.py — модель один раз в памяти."""

    def __init__(self):
        self._classify_message = None
        self._classify_batch = None
        self._extract_entities = None
        self._embed_text = None
        self._embed_texts_batch = None
        self._store = None
        self._chaos_store = None
        self._recall = None
        self._build_context_packet = None
        self._generate_daily_digest = None
        self._generate_weekly_digest = None
        self._consolidate = None
        self._similarity = None
        self._extract_json = None
        self._call_llm = None
        self._initialized = False
        self.db_path: Path = _BRAIN_DIR / "db" / "lenochka.db"

    async def initialize(self):
        """Загрузить brain.py один раз. Модель эмбеддингов живёт в памяти."""
        if self._initialized:
            return

        import brain
        import mem

        self._classify_message = brain.classify_message
        self._classify_batch = brain.classify_batch
        self._extract_entities = brain.extract_entities
        self._embed_text = brain.embed_text
        self._embed_texts_batch = brain.embed_texts_batch
        self._store = mem.store
        self._chaos_store = mem.chaos_store
        self._recall = mem.recall
        self._build_context_packet = brain.build_context_packet
        self._generate_daily_digest = brain.generate_daily_digest
        self._generate_weekly_digest = brain.generate_weekly_digest
        self._consolidate = mem.consolidate
        self._similarity = brain.similarity
        self._extract_json = brain._extract_json
        self._call_llm = brain._call_llm
        self.db_path = mem.DB_PATH
        self._initialized = True

        # Прогрев модели эмбеддингов
        logger.info("Loading embedding model (first call)...")
        self._embed_text("warmup")
        logger.info("Brain initialized — model loaded, ready")

    def is_ready(self) -> bool:
        return self._initialized

    # --- Delegates ---

    def classify_message(self, text: str, chat_context: str | None = None):
        return self._classify_message(text, chat_context)

    def classify_batch(self, texts: list[str], chat_contexts: list[str] | None = None):
        """Batch classify: N сообщений → 1 LLM-вызов."""
        return self._classify_batch(texts, chat_contexts)

    def extract_entities(self, text: str, label: str | None = None,
                         chat_context: str | None = None):
        return self._extract_entities(text, label, chat_context)

    def embed_text(self, text: str):
        return self._embed_text(text)

    def embed_texts_batch(self, texts: list[str]):
        return self._embed_texts_batch(texts)

    def store_memory(self, **kwargs):
        return self._store(**kwargs)

    def chaos_store(self, **kwargs):
        return self._chaos_store(**kwargs)

    def recall(self, **kwargs):
        return self._recall(**kwargs)

    def build_context_packet(self, **kwargs):
        return self._build_context_packet(**kwargs)

    def daily_digest(self, date=None):
        return self._generate_daily_digest(date)

    def weekly_digest(self):
        return self._generate_weekly_digest()

    def consolidate(self):
        return self._consolidate()

    def similarity(self, text1: str, text2: str):
        return self._similarity(text1, text2)


# --- DB helpers (non-blocking via sync, wrapped in asyncio.to_thread later) ---

def get_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
