"""Live integration tests for the Groq backend.

These tests make real network calls to Groq and consume free-tier quota.
They auto-skip when GROQ_API_KEY is not set in the environment, so they
are safe in CI/dev environments without the key.

Run only these:
    pytest tests/test_llm_client_integration.py -v

Run everything except these:
    pytest tests/ -v --ignore=tests/test_llm_client_integration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieval.llm_client import LLMClient  # noqa: E402

EXPECTED_KEYS = {
    "content",
    "tool_calls",
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "model",
    "finish_reason",
}

# Load .env so GROQ_API_KEY is available even when invoked outside the venv shell.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _has_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _has_key(),
    reason="Requires GROQ_API_KEY in env or .env to hit the live Groq API",
)


def test_groq_basic_chat():
    """Real Groq call: ask for a one-word answer and verify response shape."""
    client = LLMClient(provider="groq")
    response = client.chat(
        [{"role": "user", "content": "Reply with exactly the word OK."}]
    )

    assert set(response.keys()) == EXPECTED_KEYS, response.keys()
    assert response["content"], "Groq returned empty content"
    assert response["model"], "Model field empty"
    assert response["finish_reason"] in {"stop", "length", "tool_calls"}
    assert response["tokens_in"] > 0
    assert response["tokens_out"] > 0
    assert response["latency_ms"] > 0
    assert isinstance(response["tool_calls"], list)


def test_groq_tool_use_shape():
    """Real Groq call: pass a tool definition and verify the response normalizes
    tool_calls correctly — most importantly, arguments must be a dict (parsed
    from JSON), not a string. This is the contract Phase 5 relies on.
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "query_db",
                "description": "Execute a read-only SQL query against the mutual fund DB.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "A SELECT-only SQL query.",
                        }
                    },
                    "required": ["sql"],
                },
            },
        }
    ]

    client = LLMClient(provider="groq")
    response = client.chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a database assistant. Always use the query_db tool "
                    "to answer. The DB has a table fund_snapshots with columns "
                    "expense_ratio, scheme_id."
                ),
            },
            {
                "role": "user",
                "content": "What are the expense ratios in fund_snapshots?",
            },
        ],
        tools=tools,
    )

    assert response["finish_reason"] == "tool_calls"
    assert len(response["tool_calls"]) >= 1, "Expected at least one tool call"

    tc = response["tool_calls"][0]
    assert tc["name"] == "query_db"
    assert isinstance(tc["arguments"], dict), (
        f"arguments must be parsed dict, got {type(tc['arguments']).__name__}"
    )
    assert "sql" in tc["arguments"], "arguments missing 'sql' key"
    assert isinstance(tc["arguments"]["sql"], str)
    assert tc["arguments"]["sql"].strip().lower().startswith("select")
    assert tc.get("id"), "tool_call missing id"
