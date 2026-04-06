from mem._db import get_db, _load_vec
from mem.migrations import init
from mem.store import store, _content_hash
from mem.search import recall, recall_assoc
from mem.crm import (
    crm_contact,
    crm_deals,
    crm_overdue_tasks,
    crm_abandoned,
    crm_leads,
    crm_daily_summary,
)
from mem.pipeline import ingest, context, digest, weekly
from mem.maintenance import consolidate, prune_messages, stats
from mem.import_batch import import_batch
# Expose DB_PATH for external users (brain.py reads it)
from mem._config import DB_PATH, EMBEDDING_DIM
