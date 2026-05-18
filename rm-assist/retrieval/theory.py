"""FAQ-style theory/education content the chatbot can retrieve from.

The structured fund DB covers numeric fund data. This module covers the
8 RM-flagged theory topics (what is MF, SIP, taxation, etc.), the Bajaj-
specific topics (About Bajaj, Direct vs Regular), and the research-process
topic, sourced from ``data/theory.json``.

Each entry carries:

* ``bajaj_verified`` — True only when content has been explicitly signed
  off by Bajaj research/compliance. False for generic education content.
* ``pending`` — True when no content exists yet; tool returns the
  ``pending_message`` so the bot can surface it cleanly instead of
  hallucinating Bajaj-specific positioning.
* ``disclaimer`` — surface-line caveat when content is generic and
  unverified (the bot is instructed to prepend this to its answer).

Public surface:

* ``get_education_content(topic) -> dict`` — match a topic query against
  entry titles + aliases. Tries substring match first (fast, deterministic),
  then falls back to embedding-based semantic similarity IF
  ``sentence-transformers`` is installed (catches paraphrases the substring
  matcher misses, e.g. "explain MFs" -> "what is a mutual fund"). The
  embedding fallback is opt-in via the optional dependency — if not
  installed, only substring matching runs. Returns the entry payload or a
  no-match envelope listing available topics so the model can refine.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_THEORY_PATH = Path(__file__).resolve().parents[1] / "data" / "theory.json"


_THEORY_CACHE: Optional[List[Dict[str, Any]]] = None


# Embedding-based fallback state. Lazy-loaded on first miss-from-substring
# call. If ``sentence-transformers`` isn't installed, ``_EMBEDDING_AVAILABLE``
# stays False and only substring matching runs — graceful degradation.
_EMBEDDING_MODEL: Any = None  # SentenceTransformer instance or None
_TOPIC_VECTORS: Optional[List[tuple]] = None  # list of (entry, np.ndarray)
_EMBEDDING_INITIALISED: bool = False
_EMBEDDING_AVAILABLE: bool = False  # True if sentence-transformers imported OK


# Cosine-similarity threshold for the embedding fallback. Empirical pick —
# entries are short (title + ~7 aliases concatenated), so legitimate
# semantic matches typically land in the 0.55–0.85 range; paraphrases of
# completely unrelated topics land below 0.4. The 0.5 cutoff catches the
# real paraphrases while staying below the noise floor.
_EMBEDDING_THRESHOLD: float = 0.50


# Embedding model identifier. ``all-MiniLM-L6-v2`` is ~80 MB on-disk and
# ~80 MB RAM; suitable for CPU inference on the Oracle Cloud Free Tier
# ARM VM. If we ever need higher quality we can switch to ``bge-small``
# (similar size, slightly better on retrieval benchmarks).
_EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"


def _load_topics() -> List[Dict[str, Any]]:
    """Load and cache theory.json at first call. Survives missing-file."""
    global _THEORY_CACHE
    if _THEORY_CACHE is not None:
        return _THEORY_CACHE

    try:
        raw = _THEORY_PATH.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        logger.error("theory.json not found at %s", _THEORY_PATH)
        _THEORY_CACHE = []
        return _THEORY_CACHE
    except json.JSONDecodeError as exc:
        logger.error("theory.json is malformed: %s", exc)
        _THEORY_CACHE = []
        return _THEORY_CACHE

    topics = data.get("topics", [])
    if not isinstance(topics, list):
        logger.error("theory.json 'topics' is not a list (got %s)", type(topics))
        _THEORY_CACHE = []
    else:
        _THEORY_CACHE = topics
    return _THEORY_CACHE


def _format_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the entry down to the tool's contract shape.

    Keeps the fields the model needs (content + flags + disclaimer) and
    drops internal fields (aliases). Including aliases would just pad
    the tool result without helping the model write a better answer.
    """
    return {
        "matched": True,
        "topic_id": entry.get("topic_id"),
        "title": entry.get("title"),
        "content": entry.get("content"),
        "bajaj_verified": bool(entry.get("bajaj_verified", False)),
        "pending": bool(entry.get("pending", False)),
        "disclaimer": entry.get("disclaimer"),
        "pending_message": entry.get("pending_message"),
        "last_verified_at": entry.get("last_verified_at"),
    }


def _matches(needle: str, haystack: str) -> bool:
    """Cheap fuzzy match: substring in either direction.

    Real-RM queries paraphrase aggressively ("what's an SIP" vs "SIP" vs
    "systematic investment plan"). Direct substring matching catches the
    common cases without a real fuzzy library.
    """
    if not haystack:
        return False
    return needle in haystack or haystack in needle


def _init_embedding_fallback() -> None:
    """Lazy-init the sentence-transformer model + per-topic embeddings.

    Called on the first substring miss, not at module import — keeps cold
    starts fast for sessions where every query hits the substring path.
    If ``sentence-transformers`` isn't installed, we silently disable the
    fallback (``_EMBEDDING_AVAILABLE`` stays False) — substring-only
    matching remains in effect, which is the explicit graceful-degradation
    contract.
    """
    global _EMBEDDING_MODEL, _TOPIC_VECTORS, _EMBEDDING_INITIALISED, _EMBEDDING_AVAILABLE
    if _EMBEDDING_INITIALISED:
        return
    _EMBEDDING_INITIALISED = True  # mark before any heavy work so we only try once

    try:
        from sentence_transformers import SentenceTransformer  # noqa: WPS433
    except ImportError:
        logger.info(
            "sentence-transformers not installed; embedding fallback for "
            "theory matching is disabled (substring matching still works).",
        )
        return

    try:
        _EMBEDDING_MODEL = SentenceTransformer(_EMBEDDING_MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 — defensive against download / load failures
        logger.warning(
            "Failed to load sentence-transformers model %s: %s. "
            "Embedding fallback disabled.",
            _EMBEDDING_MODEL_NAME, exc,
        )
        return

    topics = _load_topics()
    vectors: List[tuple] = []
    for entry in topics:
        title = entry.get("title", "")
        aliases = entry.get("aliases", []) or []
        # Concatenate title + aliases with a separator the model handles
        # cleanly. The aliases are already authored as natural-language
        # variants ("how does mf work", "what is mf", etc.) so summing them
        # into the embedded surface gives the model maximum lexical signal
        # alongside the semantic embedding.
        surface = " | ".join(
            [title] + [a for a in aliases if isinstance(a, str) and a]
        ).strip()
        if not surface:
            continue
        try:
            vec = _EMBEDDING_MODEL.encode(surface, normalize_embeddings=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to embed topic %s: %s", entry.get("topic_id"), exc)
            continue
        vectors.append((entry, vec))

    _TOPIC_VECTORS = vectors
    _EMBEDDING_AVAILABLE = bool(vectors)
    logger.info(
        "Theory embedding fallback initialised with %d topic vectors "
        "(model=%s).",
        len(vectors), _EMBEDDING_MODEL_NAME,
    )


def _embedding_match(needle: str) -> Optional[Dict[str, Any]]:
    """Try embedding similarity. Returns the best entry above threshold, or None.

    Lazy-initialises the model on first call. Returns None if
    ``sentence-transformers`` isn't installed, the model fails to load,
    or no topic clears the ``_EMBEDDING_THRESHOLD`` cosine cutoff.
    """
    _init_embedding_fallback()
    if not _EMBEDDING_AVAILABLE or _EMBEDDING_MODEL is None or not _TOPIC_VECTORS:
        return None

    try:
        import numpy as np  # noqa: WPS433 — only needed when sentence-transformers loaded
    except ImportError:
        return None

    try:
        needle_vec = _EMBEDDING_MODEL.encode(needle, normalize_embeddings=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to embed query %r: %s", needle, exc)
        return None

    best_entry = None
    best_score = 0.0
    for entry, topic_vec in _TOPIC_VECTORS:
        score = float(np.dot(needle_vec, topic_vec))
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry is not None and best_score >= _EMBEDDING_THRESHOLD:
        logger.info(
            "Theory embedding fallback matched topic_id=%s (cosine=%.3f).",
            best_entry.get("topic_id"), best_score,
        )
        return best_entry
    logger.debug(
        "Theory embedding fallback no-match: best score %.3f below threshold %.2f",
        best_score, _EMBEDDING_THRESHOLD,
    )
    return None


def get_education_content(topic: str) -> Dict[str, Any]:
    """Match a topic query against the FAQ and return the entry.

    Parameters
    ----------
    topic : str
        Free-text topic keyword(s) from the LLM. Examples: "SIP",
        "what is a mutual fund", "taxation", "About Bajaj".

    Returns
    -------
    dict
        On match: ``_format_entry`` payload with ``matched=True``.
        On no match: ``{"matched": False, "topic_queried": ...,
        "available_topics": [...], "message": "..."}`` so the model can
        either refine the query or refuse cleanly.
    """
    if not isinstance(topic, str) or not topic.strip():
        return {
            "error": "bad_arguments",
            "message": "topic must be a non-empty string",
        }

    needle = topic.lower().strip()
    topics = _load_topics()

    # Fast path: substring match (in either direction) against title +
    # aliases. Catches the bulk of variants without invoking embeddings.
    for entry in topics:
        candidates = [entry.get("title", "").lower()]
        for alias in entry.get("aliases", []) or []:
            if isinstance(alias, str):
                candidates.append(alias.lower())
        if any(_matches(needle, c) for c in candidates if c):
            return _format_entry(entry)

    # Slow path: embedding-based semantic match for paraphrases the
    # substring matcher missed (e.g. "explain MFs" -> "what is a mutual
    # fund"). Returns None if sentence-transformers isn't installed —
    # in that case we proceed to the no-match envelope below.
    embedded_entry = _embedding_match(needle)
    if embedded_entry is not None:
        return _format_entry(embedded_entry)

    return {
        "matched": False,
        "topic_queried": topic,
        "available_topics": [
            {"topic_id": e.get("topic_id"), "title": e.get("title")}
            for e in topics
        ],
        "message": (
            "No matching FAQ entry. If the user is asking about a "
            "fund-specific topic, use lookup_scheme / query_db instead."
        ),
    }
