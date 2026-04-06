"""
Lenochka Brain v2 — Интеллектуальный модуль (пакет).
LLM-классификация, семантические эмбеддинги, RAPTOR, контекст-пакеты.
"""

from brain._config import (
    DB_PATH,
    EMBEDDING_DIM,
    GMT8,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    _now_gmt8,
)
from brain._db import _get_db
from brain.associate import auto_associate
from brain.classify import classify_batch, classify_message
from brain.context import build_context_packet
from brain.digest import generate_daily_digest, generate_weekly_digest
from brain.embed import (
    blob_to_vec,
    cosine_similarity,
    embed_text,
    embed_texts_batch,
    similarity,
    vec_to_blob,
)
from brain.extract import extract_entities
from brain.llm import _call_llm, _extract_json
from brain.raptor import build_raptor

__all__ = [
    "DB_PATH",
    "EMBEDDING_DIM",
    "GMT8",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "_now_gmt8",
    "_get_db",
    "auto_associate",
    "classify_message",
    "classify_batch",
    "build_context_packet",
    "generate_daily_digest",
    "generate_weekly_digest",
    "embed_text",
    "embed_texts_batch",
    "vec_to_blob",
    "blob_to_vec",
    "cosine_similarity",
    "similarity",
    "extract_entities",
    "_call_llm",
    "_extract_json",
    "build_raptor",
]
