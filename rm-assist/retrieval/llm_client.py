"""Provider-agnostic LLM client for the Bajaj MF research bot.

Exposes a single public class, `LLMClient`, that hides the choice of backend
behind a uniform `chat(messages, tools=None) -> dict` method. Three backends
are shipped:

* `_GroqClient` — production backend backed by the official `groq` SDK.
* `_GeminiClient` — backend backed by Google's `google-genai` SDK. Translates
  OpenAI-style messages/tools to Gemini's shape (and back) so the chatbot
  loop in `app/chatbot.py` is provider-agnostic.
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

        # Lazy-import the error class so the mock path stays importable in
        # environments without the SDK installed.
        from groq import BadRequestError  # noqa: WPS433

        start = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            # Llama-3.3-70b on Groq sometimes emits `<function=name {json}>`
            # pseudo-XML markup instead of structured tool_calls; the API
            # then 400s with `code=tool_use_failed` and stashes the raw
            # markup in `failed_generation`. We can usually parse that
            # markup back into a valid tool call and let the loop keep
            # going. If the markup is malformed beyond recovery, we
            # re-raise so the caller's last-resort guard handles it.
            recovered = _recover_tool_call_from_groq_error(exc)
            if recovered is None:
                raise
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.warning(
                "_GroqClient.chat: recovered tool_call from "
                "tool_use_failed (name=%s)", recovered["name"],
            )
            return {
                "content": "",
                "tool_calls": [recovered],
                "tokens_in": 0,
                "tokens_out": 0,
                "latency_ms": latency_ms,
                "model": self._model,
                "finish_reason": "tool_calls",
            }
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


# Matches Llama-3.3's malformed tool-call markup, e.g.:
#     <function=query_db {"sql": "SELECT ..."}</function>
#     <function=query_db{"sql": "..."}</function>
# The optional space between name and `{` and the optional trailing space
# before `</function>` are both observed in practice.
_GROQ_FAILED_TOOL_RE = re.compile(
    r"<function=(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?P<args>\{.*\})\s*</function>",
    re.DOTALL,
)


def _recover_tool_call_from_groq_error(exc: Any) -> Optional[Dict[str, Any]]:
    """Try to parse Llama's malformed tool markup out of a Groq 400.

    Returns a normalized tool_call dict on success, ``None`` if the error
    body doesn't carry a `failed_generation` field or the markup can't
    be parsed. We deliberately don't raise on parse failure — the caller
    re-raises the original BadRequestError instead, so the loop's
    last-resort guard kicks in with a clean refusal message.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    if err.get("code") != "tool_use_failed":
        return None

    failed = err.get("failed_generation")
    if not isinstance(failed, str):
        return None

    m = _GROQ_FAILED_TOOL_RE.search(failed)
    if not m:
        return None

    name = m.group("name")
    raw_args = m.group("args")
    try:
        args = json.loads(raw_args)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None

    return {
        "name": name,
        "arguments": args,
        # The provider never assigned an id, so synthesise one. Any
        # unique-ish string works; the conversation only needs it to
        # match the assistant turn with the follow-up tool message.
        "id": f"recovered_{abs(hash(failed)) % 10_000_000}",
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


def _gemini_retry_delay_seconds(exc: Any, default: float) -> float:
    """Pull `retryDelay` out of a Gemini 429 error body, else fall back.

    The error body shape (post-SDK normalisation) is
    ``{"error": {"details": [..., {"@type": ".../RetryInfo",
    "retryDelay": "7s"}, ...]}}``. We accept either `.details` on the
    exception or a dict body, and parse the trailing `s` suffix.
    """
    bodies: List[Any] = []
    body = getattr(exc, "details", None)
    if body is not None:
        bodies.append(body)
    body = getattr(exc, "response_json", None) or getattr(exc, "body", None)
    if isinstance(body, dict):
        bodies.append(body)

    for b in bodies:
        details = None
        if isinstance(b, dict):
            err = b.get("error") if isinstance(b.get("error"), dict) else b
            details = err.get("details") if isinstance(err, dict) else None
        elif isinstance(b, list):
            details = b
        if not isinstance(details, list):
            continue
        for d in details:
            if not isinstance(d, dict):
                continue
            if "RetryInfo" not in str(d.get("@type", "")):
                continue
            raw = d.get("retryDelay")
            if isinstance(raw, str) and raw.endswith("s"):
                try:
                    return float(raw[:-1])
                except ValueError:
                    pass
    return float(default)


class _GeminiClient:
    """Backend for Google's Gemini API via the `google-genai` SDK.

    Translates between OpenAI-style messages/tools (what the rest of the
    codebase speaks) and Gemini's native format:

    * OpenAI `system` role → Gemini `system_instruction` (passed separately).
    * OpenAI `user` role → Gemini `Content(role="user", parts=[Part(text=...)])`.
    * OpenAI `assistant` with text → Gemini `Content(role="model", parts=[Part(text=...)])`.
    * OpenAI `assistant` with tool_calls → Gemini `Content(role="model",
      parts=[Part(function_call=...)])`.
    * OpenAI `tool` role → Gemini `Content(role="user",
      parts=[Part(function_response=...)])`.

    Tool schemas pass through as JSON-Schema dicts wrapped in
    `types.Tool(function_declarations=[...])` — Gemini accepts the same
    subset of JSON Schema that OpenAI does for our tool definitions.

    Response parts are walked to extract any text + function_calls; output
    is normalised to the canonical `chat()` return shape.
    """

    def __init__(self, model: str):
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        # Lazy import — keeps the module importable in environments that
        # don't have google-genai installed (e.g. CI running mock-only tests).
        from google import genai  # noqa: WPS433 (intentional lazy import)

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from google.genai import errors as genai_errors  # noqa: WPS433
        from google.genai import types  # noqa: WPS433 (lazy, see __init__)

        system_instruction, contents = self._build_contents(messages, types)
        gemini_tools = self._build_tools(tools, types) if tools else None

        config_kwargs: Dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools

        # Retry-with-backoff on 429. Gemini's free tier is 5 RPM on
        # gemini-2.5-flash; a tool-use loop fires 3-6 API calls per
        # question, so we trip the limit fast. The error response carries
        # a `retryDelay` we honour; fall back to exponential backoff
        # capped at 30s if it's missing or unparseable.
        max_attempts = 5
        start = time.perf_counter()
        last_exc: Optional[Exception] = None
        response = None
        for attempt in range(max_attempts):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs) if config_kwargs else None,
                )
                break
            except genai_errors.ClientError as exc:
                last_exc = exc
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if status != 429 or attempt == max_attempts - 1:
                    raise
                delay_s = _gemini_retry_delay_seconds(exc, default=min(2 ** attempt, 30))
                logger.warning(
                    "_GeminiClient.chat: 429 rate-limit on attempt %d/%d, sleeping %.1fs",
                    attempt + 1, max_attempts, delay_s,
                )
                time.sleep(delay_s)
        if response is None:
            # Defensive: the loop always either breaks or raises, but
            # `mypy`/readers shouldn't have to prove that.
            assert last_exc is not None
            raise last_exc
        latency_ms = int((time.perf_counter() - start) * 1000)

        text_chunks: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or [] if content else []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    text_chunks.append(text)
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    raw_args = getattr(fc, "args", None) or {}
                    # Newer SDK returns args as a dict-like; older as a
                    # protobuf Struct. dict() works for both.
                    try:
                        args = dict(raw_args)
                    except (TypeError, ValueError):
                        args = {}
                    tool_calls.append({
                        "name": fc.name,
                        "arguments": args,
                        # Gemini doesn't issue tool_call ids — synthesise a
                        # stable-ish one so the assistant→tool turn pairing
                        # works downstream when we echo this back.
                        "id": f"gemini_{abs(hash((fc.name, json.dumps(args, sort_keys=True, default=str)))) % 10_000_000}",
                    })

        final_text = "".join(text_chunks)
        finish_reason = "tool_calls" if tool_calls else "stop"

        usage = getattr(response, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", 0) if usage else 0
        tokens_out = getattr(usage, "candidates_token_count", 0) if usage else 0

        return {
            "content": final_text,
            "tool_calls": tool_calls,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "latency_ms": latency_ms,
            "model": self._model,
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _build_contents(
        messages: List[Dict[str, Any]],
        types: Any,
    ) -> tuple:
        """Convert OpenAI-style messages to (system_instruction, [Content,...])."""
        system_instruction: Optional[str] = None
        contents: List[Any] = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                # Gemini takes the system prompt out-of-band via
                # GenerateContentConfig(system_instruction=...). Concatenate
                # if there are multiple (shouldn't happen in this codebase
                # but harmless).
                txt = msg.get("content") or ""
                system_instruction = (
                    txt if system_instruction is None
                    else f"{system_instruction}\n\n{txt}"
                )
                continue

            if role == "user":
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=msg.get("content") or "")],
                ))
                continue

            if role == "assistant":
                parts: List[Any] = []
                txt = msg.get("content") or ""
                if txt:
                    parts.append(types.Part(text=txt))
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or ""
                    raw_args = fn.get("arguments")
                    if raw_args is None:
                        raw_args = tc.get("arguments")
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    elif isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        args = {}
                    parts.append(types.Part(
                        function_call=types.FunctionCall(name=name, args=args),
                    ))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            if role == "tool":
                # Tool results come in as a JSON string; Gemini wants a dict
                # under FunctionResponse.response. Wrap non-dict payloads so
                # the contract holds even if a tool returns a bare value.
                content_str = msg.get("content") or ""
                try:
                    response_payload = json.loads(content_str)
                except (json.JSONDecodeError, TypeError):
                    response_payload = {"result": content_str}
                if not isinstance(response_payload, dict):
                    response_payload = {"result": response_payload}
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(function_response=types.FunctionResponse(
                        name=msg.get("name") or "",
                        response=response_payload,
                    ))],
                ))
                continue

            # Unknown role — log and skip rather than failing the whole call.
            logger.warning("_GeminiClient: skipping unknown role %r", role)

        return system_instruction, contents

    @staticmethod
    def _build_tools(
        tools: List[Dict[str, Any]],
        types: Any,
    ) -> List[Any]:
        """Convert OpenAI-style tool list to a single Gemini Tool wrapper.

        OpenAI groups each function as its own tool; Gemini groups all
        function_declarations under one Tool. We collect every function
        declaration into one Tool object — that's the idiomatic shape.
        """
        declarations: List[Any] = []
        for tool in tools:
            fn = tool.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            declarations.append(types.FunctionDeclaration(
                name=name,
                description=fn.get("description") or "",
                parameters=fn.get("parameters") or {"type": "object", "properties": {}},
            ))
        if not declarations:
            return []
        return [types.Tool(function_declarations=declarations)]


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
        elif chosen == "gemini":
            # Default to gemini-2.5-flash if LLM_MODEL still points at a
            # non-Gemini name (e.g. the Groq default `openai/gpt-oss-120b`).
            # Caller can override via LLM_MODEL env var.
            gemini_model = model or os.environ.get("LLM_MODEL", "")
            if not gemini_model or not gemini_model.startswith("gemini"):
                gemini_model = "gemini-2.5-flash"
            self._backend = _GeminiClient(model=gemini_model)
        elif chosen == "mock":
            self._backend = _MockClient()
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER: {chosen}. Supported: groq, gemini, mock."
            )

        self.provider = chosen

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return self._backend.chat(messages, tools=tools)
