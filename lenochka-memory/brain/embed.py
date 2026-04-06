"""Эмбеддинги: sentence-transformers с fallback на char n-grams."""

import hashlib
import struct
import sys

from brain._config import EMBEDDING_DIM

_embed_model = None


def _get_embed_model():
    """Ленивая загрузка sentence-transformers."""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer

        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        return _embed_model
    except Exception as e:
        print(
            f"⚠️ sentence-transformers недоступен ({e}), использую char n-gram fallback",
            file=sys.stderr,
        )
        return None


def embed_text(text):
    """
    Получить эмбеддинг текста.
    Возвращает list[float] длиной EMBEDDING_DIM.
    """
    model = _get_embed_model()
    if model is not None:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    # Fallback: char 3-gram TF, дополненный нулями до EMBEDDING_DIM
    return _embed_fallback(text)


def embed_texts_batch(texts):
    """
    Batch-эмбеддинг списка текстов.
    ~7x быстрее чем embed_text() в цикле для N>5.
    Возвращает list[list[float]].
    """
    if not texts:
        return []
    model = _get_embed_model()
    if model is not None:
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]
    # Fallback: поштучно
    return [_embed_fallback(t) for t in texts]


def _embed_fallback(text):
    """Char 3-gram TF embedding с fallback на EMBEDDING_DIM."""
    text = text.lower().strip()
    grams = {}
    for i in range(len(text) - 2):
        g = text[i : i + 3]
        grams[g] = grams.get(g, 0) + 1
    if not grams:
        return [0.0] * EMBEDDING_DIM
    # Deterministic hash-проекция в EMBEDDING_DIM (SHA-256, не Python hash())
    vec = [0.0] * EMBEDDING_DIM
    for gram, count in grams.items():
        h = (
            int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16)
            % EMBEDDING_DIM
        )
        vec[h] += count
    # Нормализация
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def vec_to_blob(vec):
    """Конвертировать list[float] в bytes для sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def blob_to_vec(blob):
    """Конвертировать bytes обратно в list[float]."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(vec1, vec2):
    """Косинусное сходство между двумя векторами."""
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def similarity(text1, text2):
    """
    Сравнение двух текстов: cosine similarity по эмбеддингам.
    """
    emb1 = embed_text(text1)
    emb2 = embed_text(text2)
    return cosine_similarity(emb1, emb2)
