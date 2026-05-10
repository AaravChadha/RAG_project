"""Provider-agnostic LLM client for the Bajaj MF research bot.

Exposes a single public class, `LLMClient`, that hides the choice of backend
behind a uniform `chat(messages, tools=None) -> dict` method. Two backends are
shipped:

* `_GroqClient` — production backend backed by the official `groq` SDK.
* `_MockClient` — deterministic, network-free backend used to exercise the
  chatbot spine and tests before a real Groq key is available.

The provider is chosen via the `LLM_PROVIDER` env var (or the `provider=`
constructor arg). Tool-call shapes returned by the underlying provider are
normalized to ``[{"name": str, "arguments": dict, "id": str}]`` so callers
never need to know which backend produced them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)


# The exact verification footer mandated by PLANNING.md 5.2.1.6. Centralised
# here so future changes happen in one place.
VERIFICATION_FOOTER = (
    "This is research output — please verify against your own analysis "
    "before advising clients."
)


class _GroqClient:
    """Real backend: thin wrapper around the official `groq` Python SDK."""

    def __init__(self, model: str):
        # Fail loud at construction time if the API key is missing — we never
        # want a silent failure deep inside `chat()`.
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")

        # Lazy import: importing this module must NOT pull in `groq` unless we
        # actually intend to use the Groq backend. Keeps the mock path
        # importable in environments without the SDK installed.
        from groq import Groq  # noqa: WPS433 (intentional lazy import)

        self._client = Groq(api_key=api_key)
        self._model = model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        # Groq follows the OpenAI chat-completions schema, so messages and
        # tools pass through untouched.
        kwargs: Dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        start = time.perf_counter()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)

        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""

        tool_calls = _normalize_tool_calls(getattr(msg, "tool_calls", None))

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0

        return {
            "content": content,
            "tool_calls": tool_calls,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "latency_ms": latency_ms,
            "model": getattr(resp, "model", self._model),
            "finish_reason": getattr(choice, "finish_reason", "stop"),
        }


def _normalize_tool_calls(raw_tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Convert Groq/OpenAI tool-call objects into the canonical shape.

    Input items look like ``{"id": ..., "type": "function",
    "function": {"name": ..., "arguments": "<json string>"}}``. We flatten to
    ``{"name": str, "arguments": dict, "id": str}`` and parse the JSON
    arguments into a dict so callers don't have to. If parsing fails we log a
    warning and fall back to ``{"_raw": <original-string>}`` rather than
    raising — losing a tool call to a parser hiccup is worse than handing the
    raw payload back up the stack.
    """
    if not raw_tool_calls:
        return []

    normalized: List[Dict[str, Any]] = []
    for tc in raw_tool_calls:
        # Support both SDK objects (attribute access) and plain dicts so this
        # helper is easy to unit-test without instantiating real SDK types.
        if isinstance(tc, dict):
            tc_id = tc.get("id", "")
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "")
        else:
            tc_id = getattr(tc, "id", "")
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "") if fn else ""

        if isinstance(raw_args, dict):
            args: Dict[str, Any] = raw_args
        else:
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "Failed to JSON-decode tool_call arguments for %s: %s",
                    name,
                    exc,
                )
                args = {"_raw": raw_args}

        normalized.append({"name": name, "arguments": args, "id": tc_id})

    return normalized


class _MockClient:
    """Deterministic, network-free backend for tests and offline development.

    Behaves just enough like a real LLM to exercise the tool-use loop:

    * If the latest user turn mentions "expense ratio" and a `query_db` tool is
      available, we emit a `query_db` tool call with a canned SQL string.
    * If the latest turn is a tool result, we synthesise a final answer that
      threads the numeric value into the verification-footer template.
    * Otherwise we echo back a short marker so callers can confirm the mock
      was hit.
    """

    MODEL_NAME = "mock-deterministic"

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        last = messages[-1] if messages else {}
        last_role = last.get("role", "")
        last_content = last.get("content", "") or ""

        # Sum char-length of every textual message as a rough token-count
        # proxy. Good enough for tests that just need a non-zero value.
        tokens_in = sum(
            len(m.get("content") or "")
            for m in messages
            if m.get("content")
        )

        # ----- Branch 1: second turn — we just got a tool result back. -----
        if last_role == "tool":
            value = self._extract_number(last_content)
            content = (
                f"The expense ratio is {value} as per the data. "
                f"Source: <scheme>, as on <date>.\n\n{VERIFICATION_FOOTER}"
            )
            return {
                "content": content,
                "tool_calls": [],
                "tokens_in": tokens_in,
                "tokens_out": len(content),
                "latency_ms": 5,
                "model": self.MODEL_NAME,
                "finish_reason": "stop",
            }

        # ----- Branch 2: first turn — should we emit a tool call? -----
        wants_expense = "expense ratio" in last_content.lower()
        has_query_db = self._has_tool(tools, "query_db")
        if wants_expense and has_query_db:
            tool_calls = [
                {
                    "name": "query_db",
                    "arguments": {
                        "sql": (
                            "SELECT expense_ratio FROM fund_snapshots "
                            "WHERE 1=1 LIMIT 1"
                        ),
                    },
                    "id": "mock_call_1",
                }
            ]
            return {
                "content": "",
                "tool_calls": tool_calls,
                "tokens_in": tokens_in,
                "tokens_out": 0,
                "latency_ms": 5,
                "model": self.MODEL_NAME,
                "finish_reason": "tool_calls",
            }

        # ----- Branch 3: default echo. -----
        content = "[mock response] " + last_content[:80]
        return {
            "content": content,
            "tool_calls": [],
            "tokens_in": tokens_in,
            "tokens_out": len(content),
            "latency_ms": 5,
            "model": self.MODEL_NAME,
            "finish_reason": "stop",
        }

    @staticmethod
    def _has_tool(tools: Optional[List[Dict[str, Any]]], name: str) -> bool:
        if not tools:
            return False
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if fn and fn.get("name") == name:
                return True
        return False

    @staticmethod
    def _extract_number(payload: str) -> str:
        """Pull the first number out of a tool-result payload.

        Tool results are usually JSON (``{"expense_ratio": 1.85}``) but we fall
        back to a plain regex so a stringified number also works.
        """
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, (int, float)):
                    return str(v)
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                for v in first.values():
                    if isinstance(v, (int, float)):
                        return str(v)
            elif isinstance(first, (int, float)):
                return str(first)

        match = re.search(r"-?\d+(?:\.\d+)?", payload)
        return match.group(0) if match else "unknown"


class LLMClient:
    """Provider-agnostic chat client.

    Picks a backend based on ``LLM_PROVIDER`` (env or constructor arg) and
    exposes a single normalized ``chat`` method. Supported providers:
    ``groq`` and ``mock``.
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        chosen = (provider or os.environ.get("LLM_PROVIDER") or "groq").lower()

        if chosen == "groq":
            self._backend = _GroqClient(model=model or config.LLM_MODEL)
        elif chosen == "mock":
            self._backend = _MockClient()
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER: {chosen}. Supported: groq, mock."
            )

        self.provider = chosen

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return self._backend.chat(messages, tools=tools)
