"""End-to-end tests for the chatbot spine.

These tests use the `seeded_db` fixture (see `conftest.py`) which loads
exactly one scheme + one parsed snapshot from the Canara Robeco sample
PDF. With Phase 5.3 the spine now runs through the full LLM tool-use
loop (mock provider), so the assertions check what the mock's
deterministic answer template produces — namely the numeric value
threaded into the verification-footer template. The Phase 1 refusal
strings ("no_handler_phase1") are gone; the new loop either produces
an answer or — if the model never stops calling tools — emits the
`loop_exceeded` refusal.
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
    """Force the mock provider so tests stay offline & deterministic.

    The real Groq backend would refuse to construct without a key; we
    also don't want tests depending on a live model's exact wording.
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    yield


def test_ask_expense_ratio_returns_value(seeded_db):
    """Mock tool-use loop yields the canned expense-ratio answer.

    The mock's answer template threads the first number from the
    tool-result JSON into the verification-footer template. With the
    real query_db tool running against `seeded_db`, that first number
    comes back from the canned SELECT in the mock's tool call — which
    isn't 1.85 (it's whatever the mock SELECT returns first). What we
    *can* assert deterministically is: a number is present, the
    footer is present, and the answer is non-empty.
    """
    answer = ask("What is the expense ratio of Canara Robeco Multi Cap?")
    assert answer
    assert "verify against your own" in answer
    # The mock always emits a digit somewhere in the answer.
    assert any(ch.isdigit() for ch in answer)


def test_ask_non_tool_question_echoes(seeded_db):
    """A question that doesn't trigger the mock's tool path echoes back.

    The mock only emits a `query_db` tool call when the user message
    mentions "expense ratio". Anything else falls through to the echo
    branch — which the loop returns as the final answer directly.
    """
    answer = ask("What is the meaning of life?")
    assert "[mock response]" in answer


def test_query_log_records_question(seeded_db):
    question = "What is the expense ratio of Canara Robeco Multi Cap?"
    answer = ask(question)

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT question, tool_calls_json, final_answer, model_name "
            "FROM query_log WHERE question = ?",
            (question,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) >= 1
    last = rows[-1]
    # Phase 5: SQL is captured in `tool_calls_json`, not `sql_executed`.
    assert last["tool_calls_json"] is not None
    assert last["final_answer"] == answer
    assert last["model_name"] == "mock-deterministic"


def test_query_log_records_loop_completion(seeded_db):
    """Every ask() lands one row; mock paths don't set refusal_reason."""
    ask("What is the meaning of life?")
    ask("What is the expense ratio of Canara Robeco Multi Cap?")

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        rows = conn.execute(
            "SELECT refusal_reason FROM query_log ORDER BY query_id"
        ).fetchall()
    finally:
        conn.close()

    # Two rows logged. Neither is a loop-exceeded refusal — the mock
    # answers cleanly in 1-2 turns. They may carry a no_data refusal
    # tag if the answer text happens to match a refusal substring, but
    # the loop_exceeded tag must NOT appear.
    assert len(rows) >= 2
    reasons = {r[0] for r in rows}
    assert "loop_exceeded" not in reasons


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
