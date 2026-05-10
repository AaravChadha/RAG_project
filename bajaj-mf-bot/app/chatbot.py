"""Chatbot tool-use loop (Phase 5.3).

Replaces the Phase 1 hardcoded pattern matcher with a real LLM tool-use
loop. The flow per question:

    1. Build the conversation: ``[system, user]``.
    2. Loop (up to ``MAX_ITERATIONS``):
        a. Send the conversation + ``TOOLS`` to ``LLMClient.chat``.
        b. If the model asks for tool calls: execute each via
           ``execute_tool``, append the assistant + tool messages to the
           conversation, and loop again.
        c. If the model returns plain text (``finish_reason != tool_calls``
           or empty ``tool_calls``): that text is the final answer.
    3. If the loop exhausts ``MAX_ITERATIONS`` without producing a final
       answer, return the ``loop_exceeded`` refusal.
    4. Always log one row to ``query_log`` capturing the full tool trace,
       cumulative tokens/latency, refusal reason (if any), and model name.

CLI:
    python -m app.chatbot "<question>"

Prints the final answer only (logs go to stderr at WARNING by default).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow `python -m app.chatbot` invocation as well as direct script use.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from app.prompts import SYSTEM_PROMPT  # noqa: E402
from retrieval.db_query import log_query  # noqa: E402
from retrieval.llm_client import LLMClient  # noqa: E402
from retrieval.tools import TOOLS, execute_tool  # noqa: E402

logger = logging.getLogger(__name__)


# Hard cap on tool-use turns. 6 is generous — a typical workflow is
# lookup_scheme -> get_schema -> query_db -> answer (3-4 turns). The
# extra headroom lets the model recover from one failed tool call.
MAX_ITERATIONS: int = 6


# Refusal message emitted when the loop runs out of iterations. Returned
# verbatim to the user; also stored as `final_answer` in `query_log`.
_REFUSAL_LOOP_EXCEEDED: str = (
    "I couldn't complete the analysis. The model didn't reach a final "
    "answer within the allowed tool-use steps."
)


# Refusal-substring heuristics. The system prompt asks the model to use
# specific phrasing for each refusal class; we match on those phrases so
# `query_log.refusal_reason` is set without needing a second model call.
# The lookup is "first match wins" — order matters: the more-specific
# scheme-unknown phrasing is checked before the generic no-data one.
_REFUSAL_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "unknown_scheme",
        (
            "unknown_scheme",
            "don't have data for scheme",
            "scheme not found",
        ),
    ),
    (
        "no_data",
        (
            "no_data",
            "i don't have data",
            "no rows",
            "could not find data",
        ),
    ),
    (
        "out_of_scope",
        (
            "out of scope",
            "outside",
            "i only answer",
        ),
    ),
]


# Cap on the per-tool-result string we keep in `query_log.tool_calls_json`.
# Holdings tables can be ~50KB of JSON which would bloat the log row; the
# model still saw the full payload during the live turn — this truncation
# only affects the audit record.
_TOOL_RESULT_LOG_CAP: int = 2000


def _infer_refusal_reason(answer: str) -> Optional[str]:
    """Match the final answer against the refusal-phrase patterns.

    Case-insensitive substring search. Returns one of ``unknown_scheme``,
    ``no_data``, ``out_of_scope``, or ``None`` (= not a refusal).
    """
    lowered = answer.lower()
    for reason, needles in _REFUSAL_PATTERNS:
        for needle in needles:
            if needle in lowered:
                return reason
    return None


def _build_assistant_message(
    content: str,
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert our canonical tool-call dicts back into the OpenAI-on-the-wire shape.

    The provider expects each assistant turn that triggered tools to
    carry the tool_calls list with ``arguments`` re-stringified as JSON;
    that's how the provider matches subsequent ``tool`` messages by
    ``tool_call_id``. ``LLMClient`` decodes arguments to dicts for our
    code; here we re-encode them on the way back out.
    """
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("arguments", {})),
                },
            }
            for tc in tool_calls
        ],
    }


def _truncate(s: str, cap: int = _TOOL_RESULT_LOG_CAP) -> str:
    """Bound a string at ``cap`` chars with an explicit ``...`` marker.

    We mark truncation explicitly so a future log reader doesn't mistake
    a chopped JSON for the model's full view of the data.
    """
    if len(s) <= cap:
        return s
    return s[:cap] + f"...[truncated, {len(s) - cap} more chars]"


def ask(question: str, user_id: Optional[str] = None) -> str:
    """Answer ``question`` via an LLM tool-use loop.

    Returns the final answer string. Always logs one row to ``query_log``
    capturing the question, tool trace, model, cumulative tokens/latency,
    and any inferred refusal reason. The ``query_id`` is captured for
    future UI affordances (Phase 6 feedback buttons) but not returned —
    that wiring is for a later phase.
    """
    client = LLMClient()

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    # Cumulative telemetry across loop iterations. Each LLM hop adds to
    # these; tool execution time is rolled in via the wall clock around
    # `execute_tool` so the logged latency reflects the user's wait.
    tool_calls_trace: List[Dict[str, Any]] = []
    cumulative_latency_ms: int = 0
    cumulative_tokens_in: int = 0
    cumulative_tokens_out: int = 0
    last_model_name: str = ""

    final_answer: Optional[str] = None
    refusal_reason: Optional[str] = None
    loop_exceeded: bool = False

    loop_start = time.perf_counter()

    for iteration in range(MAX_ITERATIONS):
        try:
            response = client.chat(messages, tools=TOOLS)
        except Exception as exc:  # noqa: BLE001 — provider-side guard
            # Groq's llama-3.3-70b sometimes emits malformed `<function=...>`
            # markup instead of structured tool_calls, which the SDK
            # surfaces as a 400 `tool_use_failed`. Rather than crashing
            # the user-facing call, treat it as a non-recoverable model
            # error and emit a refusal-shaped final answer. The full
            # exception is captured in the log row for triage.
            logger.exception(
                "client.chat raised on iteration %d: %s", iteration, exc,
            )
            final_answer = (
                "I couldn't complete the analysis (model returned a "
                "malformed tool call). Try rephrasing the question."
            )
            refusal_reason = "loop_exceeded"
            break

        cumulative_latency_ms += int(response.get("latency_ms", 0) or 0)
        cumulative_tokens_in += int(response.get("tokens_in", 0) or 0)
        cumulative_tokens_out += int(response.get("tokens_out", 0) or 0)
        last_model_name = response.get("model", last_model_name) or last_model_name

        tool_calls = response.get("tool_calls") or []
        content = response.get("content") or ""
        finish_reason = response.get("finish_reason", "stop")

        if tool_calls:
            # The model wants to act. Echo the assistant turn back into
            # the conversation in the on-the-wire shape, then run each
            # tool and feed its result back as a `tool` message.
            messages.append(_build_assistant_message(content, tool_calls))

            for tc in tool_calls:
                tc_name = tc.get("name", "")
                tc_args = tc.get("arguments", {}) or {}
                tc_id = tc.get("id", "")

                tool_start = time.perf_counter()
                try:
                    result_str = execute_tool(tc_name, tc_args)
                except Exception as exc:  # noqa: BLE001 — last-resort guard
                    # execute_tool already wraps its own exceptions; this
                    # is belt-and-braces so the loop can never crash on
                    # a tool dispatch bug.
                    logger.exception("Tool %s raised", tc_name)
                    result_str = json.dumps(
                        {"error": "unexpected", "message": str(exc)}
                    )
                tool_latency_ms = int((time.perf_counter() - tool_start) * 1000)
                cumulative_latency_ms += tool_latency_ms

                tool_calls_trace.append({
                    "name": tc_name,
                    "arguments": tc_args,
                    "result": _truncate(result_str),
                    "iteration": iteration,
                    "latency_ms": tool_latency_ms,
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc_name,
                    "content": result_str,
                })

            # Loop again — let the model react to the tool results.
            continue

        # No tool calls: this is the final answer. (We don't strictly
        # require finish_reason == "stop" — some providers report "end"
        # or similar. Absence of tool_calls is the load-bearing signal.)
        final_answer = content
        logger.debug(
            "ask: loop terminated normally after %d iteration(s) (finish_reason=%s)",
            iteration + 1,
            finish_reason,
        )
        break

    if final_answer is None:
        loop_exceeded = True
        final_answer = _REFUSAL_LOOP_EXCEEDED
        refusal_reason = "loop_exceeded"
        logger.warning(
            "ask: exceeded MAX_ITERATIONS=%d for question %r",
            MAX_ITERATIONS,
            question,
        )
    elif refusal_reason is None:
        # Don't override an explicit refusal_reason set by the
        # provider-error guard above.
        refusal_reason = _infer_refusal_reason(final_answer)

    # Wall-clock fallback: if the backend reported zero latency (the
    # mock does this) we still want a non-zero number in the log row.
    wall_latency_ms = int((time.perf_counter() - loop_start) * 1000)
    logged_latency_ms = max(cumulative_latency_ms, wall_latency_ms)

    tool_calls_json = json.dumps(tool_calls_trace) if tool_calls_trace else None

    query_id = log_query(
        question=question,
        sql=None,  # Multiple SQLs land in tool_calls_json instead.
        answer=final_answer,
        model_name=last_model_name or "unknown",
        tool_calls_json=tool_calls_json,
        latency_ms=logged_latency_ms,
        tokens_in=cumulative_tokens_in,
        tokens_out=cumulative_tokens_out,
        refusal_reason=refusal_reason,
        user_id=user_id,
    )
    # query_id is captured for future Phase 6 thumbs-up/thumbs-down
    # wiring. We don't return it yet — extending the signature can wait
    # until the UI actually needs it.
    logger.debug(
        "ask: logged query_id=%d refusal=%s loop_exceeded=%s",
        query_id,
        refusal_reason,
        loop_exceeded,
    )

    return final_answer


def _cli(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m app.chatbot "<question>"``.

    Single-shot. Prints the final answer to stdout and nothing else.
    Logs go to stderr; raise --log-level to INFO/DEBUG to trace tool calls.
    """
    parser = argparse.ArgumentParser(
        description="Ask the Bajaj MF research bot one question (Phase 5 tool-use loop).",
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="The question to ask. If omitted, read one line from stdin.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Logging level (default: WARNING; use INFO/DEBUG for traces).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.question:
        question = args.question
    else:
        try:
            question = input("Question: ").strip()
        except EOFError:
            question = ""
        if not question:
            print("No question provided.", file=sys.stderr)
            return 2

    answer = ask(question)
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
