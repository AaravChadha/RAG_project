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

* ``get_education_content(topic) -> dict`` — fuzzy-match a topic query
  against entry titles + aliases, first match wins. Returns the entry
  payload or a no-match envelope listing available topics so the model
  can refine the query.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_THEORY_PATH = Path(__file__).resolve().parents[1] / "data" / "theory.json"


_THEORY_CACHE: Optional[List[Dict[str, Any]]] = None


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

    for entry in topics:
        candidates = [entry.get("title", "").lower()]
        for alias in entry.get("aliases", []) or []:
            if isinstance(alias, str):
                candidates.append(alias.lower())
        if any(_matches(needle, c) for c in candidates if c):
            return _format_entry(entry)

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
