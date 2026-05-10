"""Tests for the Phase 5.3 tool-use loop in ``app.chatbot.ask``.

These tests run against the deterministic mock backend so they don't
require a network connection or API key. They exercise:

1. Plain-text completion (no tool call) → loop exits, query logged.
2. Tool-call round-trip → query_db is hit, result threaded into the
   final answer, tool trace captured.
3. Tool error → loop tolerates the error envelope and still produces
   a final answer.
4. Loop-exceeded safety net → MockClient is monkeypatched to always
   return tool_calls; the loop bails after MAX_ITERATIONS with the
   `loop_exceeded` refusal.
5. ``tool_calls_json`` audit shape → JSON column parses back into the
   expected ``[{name, arguments, result}, ...]`` list.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from app import chatbot as chatbot_module  # noqa: E402
from app.chatbot import MAX_ITERATIONS, ask  # noqa: E402
from retrieval import llm_client as llm_client_module  # noqa: E402


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """Pin the mock backend for every test in this module."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    yield


def _read_last_log_row():
    """Return the most recent `query_log` row as a sqlite3.Row dict-alike."""
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM query_log ORDER BY query_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def test_loop_exits_on_no_tool_calls(seeded_db):
    """Question that doesn't trigger a tool call still terminates + logs."""
    answer = ask("say hi")
    # The mock's default branch echoes the user content back.
    assert "[mock response]" in answer

    last = _read_last_log_row()
    assert last is not None
    assert last["question"] == "say hi"
    assert last["final_answer"] == answer
    # No tool calls means we log `tool_calls_json = NULL`.
    assert last["tool_calls_json"] is None
    # Mock never returns a refusal phrase, so refusal_reason is None.
    assert last["refusal_reason"] is None


def test_loop_executes_tool_call(seeded_db):
    """Expense-ratio question round-trips through query_db once."""
    answer = ask("What is the expense ratio of Canara Robeco Multi Cap?")

    assert "verify against your own" in answer
    # The mock threads the first numeric value from the tool result
    # into the answer template; just check that *some* number lands.
    assert any(ch.isdigit() for ch in answer)

    last = _read_last_log_row()
    assert last is not None
    assert last["tool_calls_json"], "expected non-empty tool_calls_json"
    trace = json.loads(last["tool_calls_json"])
    assert isinstance(trace, list)
    assert trace, "trace list should have at least one entry"
    names = {entry["name"] for entry in trace}
    assert "query_db" in names


def test_loop_handles_tool_error(seeded_db, monkeypatch):
    """A tool that returns an error envelope doesn't crash the loop.

    We monkeypatch `execute_tool` to always return a sql_error envelope.
    The mock's branch-2 logic then sees the error JSON, picks no number
    out of it (falls back to a default), and still produces a final
    answer with the verification footer.
    """
    def _fake_execute_tool(name, arguments):
        return json.dumps({"error": "sql_error", "message": "simulated"})

    monkeypatch.setattr(chatbot_module, "execute_tool", _fake_execute_tool)

    answer = ask("What is the expense ratio of Canara Robeco Multi Cap?")

    assert answer, "loop must produce a non-empty final answer"
    # Footer still attached — the mock's branch-2 always emits it.
    assert "verify against your own" in answer

    last = _read_last_log_row()
    trace = json.loads(last["tool_calls_json"])
    # The injected error envelope is captured in the trace.
    first_result = json.loads(trace[0]["result"])
    assert first_result.get("error") == "sql_error"


def test_max_iterations_loop_exceeded(seeded_db, monkeypatch):
    """A model that never stops calling tools triggers the cap.

    We swap `_MockClient.chat` for one that always returns a tool_call.
    After MAX_ITERATIONS the loop must bail with the canned refusal +
    `refusal_reason='loop_exceeded'`.
    """
    call_count = {"n": 0}

    def _always_tool(self, messages, tools=None):
        call_count["n"] += 1
        return {
            "content": "",
            "tool_calls": [
                {
                    "name": "query_db",
                    "arguments": {"sql": "SELECT 1"},
                    "id": f"loop_call_{call_count['n']}",
                }
            ],
            "tokens_in": 0,
            "tokens_out": 0,
            "latency_ms": 1,
            "model": "mock-deterministic",
            "finish_reason": "tool_calls",
        }

    monkeypatch.setattr(llm_client_module._MockClient, "chat", _always_tool)

    answer = ask("force the loop to exceed")

    assert "couldn't complete" in answer.lower()
    # Exactly MAX_ITERATIONS chat() invocations happen before bailing.
    assert call_count["n"] == MAX_ITERATIONS

    last = _read_last_log_row()
    assert last["refusal_reason"] == "loop_exceeded"
    # Trace captures every tool call attempted before the cap.
    trace = json.loads(last["tool_calls_json"])
    assert len(trace) == MAX_ITERATIONS


def test_query_log_captures_trace(seeded_db):
    """After a successful tool-use answer, tool_calls_json is well-formed JSON."""
    ask("What is the expense ratio of Canara Robeco Multi Cap?")

    last = _read_last_log_row()
    assert last["tool_calls_json"], "expected tool trace in query_log"

    parsed = json.loads(last["tool_calls_json"])
    assert isinstance(parsed, list)
    assert parsed

    entry = parsed[0]
    # Contract shape per chatbot.ask: {name, arguments, result, ...}.
    for key in ("name", "arguments", "result"):
        assert key in entry, f"trace entry missing '{key}'"

    # `arguments` is a JSON-decoded dict, `result` is a string (possibly
    # truncated) — never re-wrapped JSON.
    assert isinstance(entry["arguments"], dict)
    assert isinstance(entry["result"], str)
    # query_db tool always returns valid JSON, so the result must parse.
    json.loads(entry["result"])
