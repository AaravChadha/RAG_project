"""End-to-end tests for the Phase 1.5 chatbot spine.

These tests use the `seeded_db` fixture (see `conftest.py`) which loads
exactly one scheme + one parsed snapshot from the Canara Robeco sample
PDF. They exercise both the success path (real number returned) and
the two refusal paths, and validate that every call lands a row in
`query_log`.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make the bajaj-mf-bot package root importable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from app.chatbot import ask  # noqa: E402
from retrieval.db_query import query_db  # noqa: E402


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """The Phase 1 spine does not call the LLM, but config still reads
    LLM_PROVIDER on import. Force `mock` so a missing GROQ_API_KEY in a
    fresh checkout never fails a test for the wrong reason.
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    yield


def test_ask_expense_ratio_returns_value(seeded_db):
    answer = ask("What is the expense ratio of Canara Robeco Multi Cap?")
    assert "1.85" in answer
    assert "Canara Robeco" in answer
    assert "verify against your own" in answer
    assert "as on" in answer


def test_ask_unknown_scheme_refuses(seeded_db):
    answer = ask("What is the expense ratio of NonExistentFund?")
    assert "I don't have data" in answer


def test_ask_unknown_question_refuses(seeded_db):
    answer = ask("What is the meaning of life?")
    assert "don't have a hardcoded answer" in answer


def test_query_log_records_question(seeded_db):
    question = "What is the expense ratio of Canara Robeco Multi Cap?"
    answer = ask(question)

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT question, sql_executed, final_answer FROM query_log "
            "WHERE question = ?",
            (question,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) >= 1
    last = rows[-1]
    assert last["sql_executed"] is not None
    assert last["final_answer"] == answer


def test_query_log_records_refusal(seeded_db):
    # Trigger the no_handler_phase1 refusal path.
    ask("What is the meaning of life?")
    # And the no_data refusal path for good measure.
    ask("What is the expense ratio of NonExistentFund?")

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        rows = conn.execute(
            "SELECT refusal_reason FROM query_log WHERE refusal_reason IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    reasons = {r[0] for r in rows}
    assert reasons & {"no_handler_phase1", "no_data"}


def test_db_query_refuses_ddl(seeded_db):
    with pytest.raises(ValueError) as excinfo:
        query_db("DROP TABLE schemes")
    assert "DROP" in str(excinfo.value)


def test_db_query_refuses_dml(seeded_db):
    with pytest.raises(ValueError):
        query_db("INSERT INTO schemes (scheme_name) VALUES ('hack')")


def test_db_query_allows_select_with_created_at(seeded_db):
    # `created_at` contains the substring "created" — the regex must NOT
    # flag it as a CREATE statement.
    rows = query_db("SELECT created_at FROM schemes LIMIT 1")
    assert isinstance(rows, list)
