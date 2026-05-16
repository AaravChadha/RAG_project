"""Tests for Phase 6 feedback wiring.

Covers:

1. ``update_query_feedback`` writes ``user_feedback`` for a real query_log row.
2. ``update_query_feedback`` also writes ``feedback_comment`` when supplied.
3. ``ask()`` returns a ``(str, int)`` tuple — the int is the new query_id.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from app.chatbot import ask  # noqa: E402
from retrieval.db_query import log_query, update_query_feedback  # noqa: E402


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """Pin the mock backend so tests stay offline + deterministic."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    yield


def _read_row(query_id: int):
    """Fetch one query_log row by primary key."""
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM query_log WHERE query_id = ?",
            (query_id,),
        ).fetchone()
    finally:
        conn.close()


def test_update_query_feedback_thumbs_up(seeded_db):
    """Inserting a log row then updating it writes user_feedback."""
    query_id = log_query(
        question="dummy",
        sql=None,
        answer="dummy answer",
        model_name="mock",
    )
    assert query_id > 0

    update_query_feedback(query_id, "thumbs_up")

    row = _read_row(query_id)
    assert row is not None
    assert row["user_feedback"] == "thumbs_up"
    # No comment supplied → column stays NULL.
    assert row["feedback_comment"] is None


def test_update_query_feedback_with_comment(seeded_db):
    """Thumbs-down with a comment writes both columns in one update."""
    query_id = log_query(
        question="dummy",
        sql=None,
        answer="dummy answer",
        model_name="mock",
    )

    update_query_feedback(
        query_id,
        "thumbs_down",
        comment="wrong scheme matched",
    )

    row = _read_row(query_id)
    assert row["user_feedback"] == "thumbs_down"
    assert row["feedback_comment"] == "wrong scheme matched"


def test_update_query_feedback_missing_row_raises(seeded_db):
    """A bogus query_id surfaces as ValueError, not a silent no-op."""
    with pytest.raises(ValueError):
        update_query_feedback(999_999_999, "thumbs_up")


def test_ask_returns_query_id_tuple(seeded_db):
    """ask() must return (str, int>0); the UI binds feedback off that int."""
    result = ask("say hi")
    assert isinstance(result, tuple)
    assert len(result) == 2
    answer, query_id = result
    assert isinstance(answer, str) and answer
    assert isinstance(query_id, int) and query_id > 0

    # And the row really exists in the DB at that ID.
    row = _read_row(query_id)
    assert row is not None
    assert row["question"] == "say hi"
