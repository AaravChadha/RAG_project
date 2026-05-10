"""Chatbot vertical-slice spine (Phase 1.5).

ONE question shape is wired end-to-end: "expense ratio of <scheme>". The
pattern matcher extracts the scheme substring, fires a parameterised
SQL against the read-only `query_db` surface, and formats the answer
with a Python f-string template (NOT the LLM — that arrives in Phase 5
with tool-use). Every call — answer or refusal — is recorded in
`query_log` for audit.

CLI:
    python -m app.chatbot "What is the expense ratio of <scheme>?"

If invoked with no argument, the CLI reads one question from stdin.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Pattern, Tuple

# Allow `python -m app.chatbot` invocation as well as direct script use.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from retrieval.db_query import log_query, query_db  # noqa: E402

logger = logging.getLogger(__name__)


# Exact verification footer per PLANNING.md 5.2.1.6 — em-dash, no trailing
# newline. Centralised so any future tweak happens in one place.
VERIFICATION_FOOTER = (
    "This is research output — please verify against your own "
    "analysis before advising clients."
)

# Pattern → SQL template. Phase 1 has exactly one entry; widening this list
# is the Phase 5 tool-use work, not the spine.
_EXPENSE_RATIO_RE: Pattern[str] = re.compile(
    r"(?:expense ratio|expense)\s+(?:of|for)\s+(.+?)(?:\?|$)",
    re.IGNORECASE,
)

_EXPENSE_RATIO_SQL = """\
SELECT s.scheme_name, fs.expense_ratio, fs.as_of_date
FROM fund_snapshots fs
JOIN schemes s ON s.scheme_id = fs.scheme_id
WHERE LOWER(s.scheme_name) LIKE LOWER(?) AND fs.superseded_at IS NULL
ORDER BY fs.report_month DESC, fs.revision DESC
LIMIT 1
"""

PATTERNS: list[Tuple[Pattern[str], str]] = [
    (_EXPENSE_RATIO_RE, _EXPENSE_RATIO_SQL),
]

# Refusal text constants — kept here so tests can assert against them and
# the same wording shows up in every code path that hits the same case.
_REFUSAL_NO_HANDLER = (
    "I don't have a hardcoded answer for that question yet. "
    "(Phase 1 spine only handles 'expense ratio of <scheme>'.)"
)
_REFUSAL_NO_DATA = "I don't have data for that question."

# Phase 1 doesn't call the LLM — formatting is a Python f-string. We still
# record a `model_name` so query_log rows are queryable; the explicit
# `no-llm-spine` sentinel makes Phase-1 rows easy to filter out later.
_PHASE1_MODEL_NAME = "no-llm-spine"


def _format_answer(scheme_name: str, expense_ratio: float, as_of_date: str) -> str:
    """Render the canonical answer body + citation + verification footer.

    The footer string is the exact PLANNING.md 5.2.1.6 wording (em-dash).
    """
    return (
        f"The expense ratio of {scheme_name} is {expense_ratio}% "
        f"(as on {as_of_date}).\n\n"
        f"Source: {scheme_name}, as on {as_of_date}.\n\n"
        f"{VERIFICATION_FOOTER}"
    )


def ask(question: str, user_id: Optional[str] = None) -> str:
    """Answer one question. Always returns a string; never raises on miss.

    Three branches:
      1. No pattern match -> refusal with `no_handler_phase1`.
      2. Pattern match but DB has no rows -> refusal with `no_data`.
      3. Pattern match + DB hit -> formatted answer + citation + footer.

    Every branch writes one row to `query_log`.
    """
    start = time.perf_counter()

    # --- Branch 1: pattern match -----------------------------------------
    matched: Optional[Tuple[Pattern[str], str, re.Match]] = None
    for pat, sql_template in PATTERNS:
        m = pat.search(question)
        if m:
            matched = (pat, sql_template, m)
            break

    if matched is None:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info("ask: no pattern matched for question %r", question)
        log_query(
            question=question,
            sql=None,
            answer=_REFUSAL_NO_HANDLER,
            model_name=_PHASE1_MODEL_NAME,
            latency_ms=latency_ms,
            refusal_reason="no_handler_phase1",
            user_id=user_id,
        )
        return _REFUSAL_NO_HANDLER

    _, sql_template, m = matched
    scheme_substring = m.group(1).strip()
    bind_param = f"%{scheme_substring}%"

    # --- Branch 2: DB returns no rows ------------------------------------
    rows = query_db(sql_template, (bind_param,))
    if not rows:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info("ask: pattern matched but DB empty for %r", scheme_substring)
        log_query(
            question=question,
            sql=sql_template,
            answer=_REFUSAL_NO_DATA,
            model_name=_PHASE1_MODEL_NAME,
            latency_ms=latency_ms,
            refusal_reason="no_data",
            user_id=user_id,
        )
        return _REFUSAL_NO_DATA

    # --- Branch 3: hit ---------------------------------------------------
    row = rows[0]
    answer = _format_answer(
        scheme_name=row["scheme_name"],
        expense_ratio=row["expense_ratio"],
        as_of_date=row["as_of_date"],
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    log_query(
        question=question,
        sql=sql_template,
        answer=answer,
        model_name=_PHASE1_MODEL_NAME,
        latency_ms=latency_ms,
        user_id=user_id,
    )
    return answer


def _cli(argv: list[str] | None = None) -> int:
    """CLI entrypoint: `python -m app.chatbot "<question>"`.

    Prompts on stdin if no positional arg is supplied (single question,
    no loop — interactive REPLs come later with the UI).
    """
    parser = argparse.ArgumentParser(
        description="Ask the Bajaj MF research bot one question (Phase 1 spine).",
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
    # The CLI is the one explicit print() path — chatbot.ask never prints.
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
