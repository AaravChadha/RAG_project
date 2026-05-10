"""Tests for the provider-agnostic LLMClient.

These tests are deliberately self-contained: the mock backend is exercised
end-to-end, and the Groq backend is only checked for its construction-time
guard (we never make a real network call here).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add the bajaj-mf-bot package root to sys.path so `import config` and
# `from retrieval.llm_client import ...` resolve regardless of where pytest
# is invoked from.
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

QUERY_DB_TOOL = {
    "type": "function",
    "function": {
        "name": "query_db",
        "description": "Execute a read-only SQL query.",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Prevent ambient env vars from bleeding into tests."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    yield


def test_mock_client_basic():
    client = LLMClient(provider="mock")
    resp = client.chat([{"role": "user", "content": "hello"}])

    assert set(resp.keys()) == EXPECTED_KEYS
    assert "[mock response]" in resp["content"]
    assert resp["tool_calls"] == []
    assert resp["finish_reason"] == "stop"
    assert resp["model"] == "mock-deterministic"


def test_mock_client_tool_call_path():
    client = LLMClient(provider="mock")
    resp = client.chat(
        messages=[
            {"role": "user", "content": "What is the expense ratio of fund X?"}
        ],
        tools=[QUERY_DB_TOOL],
    )

    assert resp["content"] == ""
    assert resp["finish_reason"] == "tool_calls"
    assert len(resp["tool_calls"]) == 1

    tc = resp["tool_calls"][0]
    assert tc["name"] == "query_db"
    # Crucial contract: arguments must be a dict, not a JSON string.
    assert isinstance(tc["arguments"], dict)
    assert "sql" in tc["arguments"]
    assert tc["id"]


def test_mock_client_tool_response_path():
    client = LLMClient(provider="mock")
    messages = [
        {"role": "user", "content": "What is the expense ratio of fund X?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "mock_call_1",
                    "type": "function",
                    "function": {
                        "name": "query_db",
                        "arguments": '{"sql": "SELECT ..."}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "mock_call_1",
            "name": "query_db",
            "content": '{"expense_ratio": 1.85}',
        },
    ]
    resp = client.chat(messages)

    assert "1.85" in resp["content"]
    assert "verify against your own" in resp["content"]
    assert resp["tool_calls"] == []
    assert resp["finish_reason"] == "stop"


def test_groq_client_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        LLMClient(provider="groq")
    assert "GROQ_API_KEY not set" in str(excinfo.value)


def test_unknown_provider():
    with pytest.raises(ValueError):
        LLMClient(provider="invalid")


def test_normalized_response_shape():
    client = LLMClient(provider="mock")
    resp = client.chat([{"role": "user", "content": "anything"}])
    assert set(resp.keys()) == EXPECTED_KEYS
