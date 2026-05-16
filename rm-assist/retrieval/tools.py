"""Tool definitions and dispatcher for the LLM tool-use loop.

The chatbot LLM doesn't talk to SQLite directly. Instead, it picks from five
named tools and the dispatcher in this module runs them. Each tool returns a
JSON-encoded string so the result can be embedded verbatim in the next
``tool`` message without further marshalling.

Public surface:

* ``TOOLS`` — OpenAI-style tool schema list, ready to pass to
  ``LLMClient.chat(tools=...)``.
* ``execute_tool(name, arguments) -> str`` — dispatch by name. Always returns
  a string; never raises (failures are encoded as ``{"error": ...}`` JSON so
  the model can read and react to them).

The five tools:

* ``query_db`` — thin wrapper over the read-only ``db_query.query_db``.
* ``lookup_scheme`` — fuzzy substring search over ``schemes`` so the LLM can
  canonicalise names before constructing SQL.
* ``compare_schemes`` — purpose-built side-by-side comparison so the model
  doesn't have to hand-roll the same join + filter SQL each time it sees a
  "compare X vs Y" question.
* ``get_market_state`` — current Indian-index levels + recent moves; for
  market-timing questions.
* ``get_education_content`` — FAQ-style theory/Bajaj-positioning content.

Schema is NOT a tool. The full curated schema lives in the SYSTEM_PROMPT
(see ``app/prompts.py``); embedding it there saves one inference round-trip
per question vs. fetching it via a tool call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from retrieval.db_query import query_db

logger = logging.getLogger(__name__)


# Default metric set used by compare_schemes when the caller omits `metrics`.
# Picked to cover return + risk-adjusted return + cost + scale on a single
# row — these are the four dimensions an RM almost always wants in a comp.
_DEFAULT_COMPARE_METRICS: List[str] = [
    "return_1y",
    "return_3y",
    "sharpe_3y",
    "std_dev_3y",
    "expense_ratio",
    "fund_aum_cr",
]


# Columns the model is allowed to ask for in compare_schemes. We allowlist
# explicitly rather than passing the metric name straight into SQL — even
# though the SQL goes through `query_db`'s DDL guard, an allowlist keeps the
# error message friendly when the model hallucinates a column.
_COMPARE_ALLOWED_METRICS = {
    "expense_ratio", "fund_aum_cr",
    "return_1m", "return_3m", "return_6m",
    "return_1y", "return_2y", "return_3y", "return_5y", "return_10y",
    "return_since_inception",
    "sharpe_1y", "sharpe_3y",
    "std_dev_1y", "std_dev_3y",
    "beta_1y", "beta_3y",
    "sortino_1y", "sortino_3y",
    "treynor_1y", "treynor_3y",
    "info_ratio_1y", "info_ratio_3y",
    "up_capture_1y", "up_capture_3y",
    "down_capture_1y", "down_capture_3y",
    "tracking_error_1y", "tracking_error_3y",
    "r_square_1y", "r_square_3y",
    "large_cap_pct", "mid_cap_pct", "small_cap_pct",
    "portfolio_pe", "portfolio_pb", "portfolio_div_yield",
    "modified_duration",
    "drawdown_pct", "drawdown_duration_days",
    "total_securities", "avg_mkt_cap_cr", "median_mkt_cap_cr",
}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_query_db(arguments: Dict[str, Any]) -> str:
    """Execute one read-only SELECT and return up to 100 rows as JSON.

    Returns either ``json.dumps([...rows...])`` on success or
    ``json.dumps({"error": "...", "message": "..."})`` on failure. We catch
    both ``ValueError`` (raised by ``query_db`` when it spots a DDL/DML
    keyword) and ``sqlite3.Error`` (raised by SQLite for malformed SQL or
    missing tables). Anything else also gets caught so a tool result is
    always a string — the dispatch loop must never raise.
    """
    sql = arguments.get("sql", "")
    if not isinstance(sql, str) or not sql.strip():
        return json.dumps({"error": "bad_arguments", "message": "Missing 'sql' string."})

    try:
        rows = query_db(sql)
    except ValueError as exc:
        # Read-only-refusal: DDL/DML keyword detected.
        return json.dumps({"error": "read_only_refusal", "message": str(exc)})
    except sqlite3.Error as exc:
        return json.dumps({"error": "sql_error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("Unexpected error in query_db tool")
        return json.dumps({"error": "unexpected", "message": str(exc)})

    # Truncate so a runaway SELECT can't blow the LLM context window.
    truncated = rows[:100]
    return json.dumps(truncated, default=str)


def _tool_lookup_scheme(arguments: Dict[str, Any]) -> str:
    """Find up to 10 schemes whose name fuzzy-matches the substring.

    Returns a JSON list of ``{scheme_id, scheme_name, amc, category}`` dicts.
    When nothing matches, returns ``{"matches": [], "message": "..."}`` so the
    LLM has a clearly-shaped no-match signal it can pattern-match on.
    """
    needle = arguments.get("name_substring", "")
    if not isinstance(needle, str) or not needle.strip():
        return json.dumps(
            {"error": "bad_arguments", "message": "Missing 'name_substring'."}
        )

    sql = (
        "SELECT scheme_id, scheme_name, amc, category "
        "FROM schemes "
        "WHERE LOWER(scheme_name) LIKE LOWER(?) "
        "ORDER BY scheme_name LIMIT 10"
    )
    try:
        rows = query_db(sql, (f"%{needle}%",))
    except sqlite3.Error as exc:
        return json.dumps({"error": "sql_error", "message": str(exc)})

    if not rows:
        return json.dumps(
            {"matches": [], "message": f"No scheme found matching '{needle}'"}
        )
    return json.dumps(rows, default=str)


def _fetch_latest_snapshot(scheme_id: int) -> Optional[Dict[str, Any]]:
    """Return the latest non-superseded snapshot for a scheme, or None."""
    sql = (
        "SELECT * FROM fund_snapshots "
        "WHERE scheme_id = ? AND superseded_at IS NULL "
        "ORDER BY report_month DESC, revision DESC LIMIT 1"
    )
    rows = query_db(sql, (scheme_id,))
    return rows[0] if rows else None


def _fuzzy_lookup_scheme(name_substring: str) -> Optional[Dict[str, Any]]:
    """Return the first ``schemes`` row matching the substring, or None."""
    sql = (
        "SELECT scheme_id, scheme_name FROM schemes "
        "WHERE LOWER(scheme_name) LIKE LOWER(?) "
        "ORDER BY scheme_name LIMIT 1"
    )
    rows = query_db(sql, (f"%{name_substring}%",))
    return rows[0] if rows else None


def _tool_compare_schemes(arguments: Dict[str, Any]) -> str:
    """Build a side-by-side comparison of N schemes on M metrics.

    For each requested scheme name we fuzzy-match against ``schemes`` (first
    hit wins, same behaviour as ``lookup_scheme``), then pull the latest
    non-superseded snapshot and project the requested metric columns. Schemes
    we can't find come back as ``{"scheme_name": "<query>", "error": "not
    found"}`` rather than being silently dropped, so the model can mention
    them in its answer.
    """
    raw_names = arguments.get("scheme_names")
    if not isinstance(raw_names, list) or not raw_names:
        return json.dumps(
            {"error": "bad_arguments", "message": "'scheme_names' must be a non-empty list."}
        )

    metrics = arguments.get("metrics") or list(_DEFAULT_COMPARE_METRICS)
    if not isinstance(metrics, list) or not metrics:
        metrics = list(_DEFAULT_COMPARE_METRICS)

    # Drop unknown metric names — better than crashing on a typo from the LLM.
    metrics = [m for m in metrics if isinstance(m, str) and m in _COMPARE_ALLOWED_METRICS]
    if not metrics:
        metrics = list(_DEFAULT_COMPARE_METRICS)

    comparison: List[Dict[str, Any]] = []
    for query_name in raw_names:
        if not isinstance(query_name, str) or not query_name.strip():
            comparison.append({"scheme_name": str(query_name), "error": "not found"})
            continue

        try:
            match = _fuzzy_lookup_scheme(query_name)
        except sqlite3.Error as exc:
            comparison.append({
                "scheme_name": query_name,
                "error": f"sql_error: {exc}",
            })
            continue

        if not match:
            comparison.append({"scheme_name": query_name, "error": "not found"})
            continue

        try:
            snap = _fetch_latest_snapshot(int(match["scheme_id"]))
        except sqlite3.Error as exc:
            comparison.append({
                "scheme_name": match["scheme_name"],
                "error": f"sql_error: {exc}",
            })
            continue

        if not snap:
            comparison.append({
                "scheme_name": match["scheme_name"],
                "error": "no_snapshot",
            })
            continue

        metric_values = {m: snap.get(m) for m in metrics}
        comparison.append({
            "scheme_name": match["scheme_name"],
            "as_of_date": snap.get("as_of_date"),
            "metrics": metric_values,
        })

    return json.dumps({"comparison": comparison}, default=str)


def _tool_get_education_content(arguments: Dict[str, Any]) -> str:
    """Wrapper around theory.get_education_content for the LLM tool surface.

    Returns the matched FAQ entry as JSON. On no match, returns a
    'matched=False' envelope with the available topic list so the model
    can refine the query or refuse cleanly.
    """
    topic = arguments.get("topic", "")
    if not isinstance(topic, str) or not topic.strip():
        return json.dumps({
            "error": "bad_arguments",
            "message": "'topic' must be a non-empty string",
        })

    try:
        # Lazy import keeps tools.py importable even if data/theory.json
        # is missing — the tool just always returns 'no match' instead
        # of breaking import.
        from retrieval.theory import get_education_content  # noqa: WPS433
        result = get_education_content(topic)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("get_education_content crashed")
        return json.dumps({"error": "unexpected", "message": str(exc)})

    return json.dumps(result, default=str)


def _tool_get_market_state(arguments: Dict[str, Any]) -> str:
    """Fetch current Indian-market index levels + recent moves.

    Thin wrapper around ``market_data.get_market_state``. Used by the LLM to
    answer market-timing questions ("is this the right time to invest?", etc.)
    that the structured fund DB can't address on its own.
    """
    indices = arguments.get("indices")
    if indices is not None and not isinstance(indices, list):
        return json.dumps({
            "error": "bad_arguments",
            "message": "'indices' must be a list of index names or omitted.",
        })

    try:
        # Lazy import — keeps this module importable in environments without
        # yfinance installed (e.g. mock-only test runs).
        from retrieval.market_data import get_market_state  # noqa: WPS433
        result = get_market_state(indices=indices if indices else None)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("get_market_state crashed")
        return json.dumps({"error": "unexpected", "message": str(exc)})

    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DISPATCH: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "query_db": _tool_query_db,
    "lookup_scheme": _tool_lookup_scheme,
    "compare_schemes": _tool_compare_schemes,
    "get_market_state": _tool_get_market_state,
    "get_education_content": _tool_get_education_content,
}


def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Dispatch a tool call by name. Always returns a string.

    Unknown tool names come back as ``{"error": "unknown_tool", ...}`` rather
    than raising so the LLM can recover in-loop. Same goes for any exception
    raised by an individual tool implementation — we trap it here as a
    last-resort guard so the tool-use loop in the chatbot never has to handle
    exceptions.
    """
    if not isinstance(arguments, dict):
        return json.dumps({
            "error": "bad_arguments",
            "message": "arguments must be a dict",
        })

    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({
            "error": "unknown_tool",
            "message": f"No tool named '{name}'. Available: {sorted(_DISPATCH)}",
        })

    try:
        return handler(arguments)
    except Exception as exc:  # noqa: BLE001 — last-resort guard, see docstring
        logger.exception("Unhandled error in tool %s", name)
        return json.dumps({"error": "unexpected", "message": str(exc)})


# ---------------------------------------------------------------------------
# OpenAI-style tool schema (consumed by LLMClient.chat(tools=...))
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_db",
            "description": (
                "Execute a read-only SELECT against the mutual fund database "
                "and get back up to 100 rows as JSON. Use this for any "
                "question that doesn't fit compare_schemes — rankings, "
                "filters, sector tilts, holdings lookups, etc. The full "
                "schema is provided in the system prompt; refer to it "
                "directly when writing SQL. DDL/DML keywords (INSERT, "
                "UPDATE, DELETE, DROP, ALTER, CREATE) are rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "A SELECT statement. Always filter "
                            "fund_snapshots WHERE superseded_at IS NULL for "
                            "current data."
                        ),
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_scheme",
            "description": (
                "Canonicalise a fuzzy scheme name to its full row in the "
                "schemes table. Call this FIRST whenever the user mentions a "
                "scheme by partial name — it tells you the exact "
                "scheme_name, amc, category, and scheme_id you should use in "
                "follow-up queries. Returns up to 10 fuzzy matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name_substring": {
                        "type": "string",
                        "description": (
                            "A substring of the scheme name. Example: "
                            "'Canara Robeco Multi Cap' will match the "
                            "regular-growth variant."
                        ),
                    },
                },
                "required": ["name_substring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_education_content",
            "description": (
                "Retrieve FAQ-style theory/education content for "
                "non-fund-specific questions: 'what is a mutual fund', "
                "'what is SIP', 'MF risks', 'MF taxation', 'investment "
                "horizon', 'redemption / exit load', 'MF vs FD', "
                "'About Bajaj Capital', 'Direct vs Regular plans', and "
                "'Bajaj research process'. Returns content + flags: "
                "'bajaj_verified' (true only for officially-verified "
                "Bajaj content), 'pending' (true when no content exists "
                "yet — surface the 'pending_message'), 'disclaimer' "
                "(prepend this when content is generic-but-unverified). "
                "Do NOT use this for fund-specific numeric questions — "
                "those go to query_db / lookup_scheme / compare_schemes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Topic keywords from the user's question. "
                            "Examples: 'what is SIP', 'mutual fund "
                            "taxation', 'Direct vs Regular', 'About "
                            "Bajaj'."
                        ),
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_state",
            "description": (
                "Fetch current level and recent moves for headline Indian "
                "indices (default: NIFTY 50, Sensex, NIFTY 500). Returns "
                "current_level, change_1d/5d/1m/3m/6m/1y_pct, year_high, "
                "year_low, pct_off_52w_high, pct_off_52w_low, as_of. Cached "
                "for 15 min. Call this for market-timing questions ('is this "
                "the right time to invest?', 'should I redeem during this "
                "fall?', 'how long will the correction last?') and to give "
                "drawdown context to volatility/redemption questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "indices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of index display names. Supported: "
                            "['NIFTY 50', 'Sensex', 'NIFTY 500']. If omitted, "
                            "returns all three."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_schemes",
            "description": (
                "Compare multiple schemes side-by-side on selected metrics. "
                "Use this for any 'compare X vs Y' or 'how does X stack up "
                "against Y' question. More reliable than constructing the "
                "SQL yourself. Each scheme name is fuzzy-matched (first hit "
                "wins) so partial names are fine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheme_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of scheme name substrings to compare "
                            "(fuzzy matched). Example: "
                            "['Canara Robeco Multi Cap', 'ABSL Arbitrage']."
                        ),
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional column names from fund_snapshots. "
                            "Default: ['return_1y', 'return_3y', "
                            "'sharpe_3y', 'std_dev_3y', 'expense_ratio', "
                            "'fund_aum_cr']."
                        ),
                    },
                },
                "required": ["scheme_names"],
            },
        },
    },
]
