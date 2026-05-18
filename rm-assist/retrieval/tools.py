"""Tool definitions and dispatcher for the LLM tool-use loop.

The chatbot LLM doesn't talk to SQLite directly. Instead, it picks from six
named tools and the dispatcher in this module runs them. Each tool returns a
JSON-encoded string so the result can be embedded verbatim in the next
``tool`` message without further marshalling.

Public surface:

* ``TOOLS`` — OpenAI-style tool schema list, ready to pass to
  ``LLMClient.chat(tools=...)``.
* ``execute_tool(name, arguments) -> str`` — dispatch by name. Always returns
  a string; never raises (failures are encoded as ``{"error": ...}`` JSON so
  the model can read and react to them).

The six tools:

* ``query_db`` — thin wrapper over the read-only ``db_query.query_db``;
  used for cross-fund queries (rankings, filters, category lists,
  holdings searches).
* ``lookup_scheme`` — fuzzy substring search over ``schemes``; mainly
  used for disambiguation when the user's wording matches multiple funds.
* ``compare_schemes`` — purpose-built side-by-side comparison for
  ``compare X vs Y`` questions.
* ``get_full_snapshot`` — fuzzy-match plus the full per-fund picture
  (snapshot row + benchmark + alpha + top holdings + sectors + managers +
  drawdown) in one call; the preferred path for single-fund questions.
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
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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


# Brand abbreviation expansion map. Keys are lowercased; matching is on whole
# word-tokens (not substrings) so "abs" doesn't accidentally trigger "absl".
# Values are the canonical AMC name fragments — they get tokenised and added
# to the user's query before scoring, so the word-overlap scorer naturally
# picks up matches in ``schemes.amc``.
#
# This is intentionally small: the long-form AMC names already match via
# word tokens (e.g. "Aditya Birla" matches without expansion). The map exists
# only for genuinely opaque abbreviations RMs use ("ABSL", "PPFAS", "MOSL").
_BRAND_ABBREVIATIONS: Dict[str, str] = {
    "absl":         "aditya birla sun life",
    "abslmf":       "aditya birla sun life",
    "abi":          "aditya birla",
    "absc":         "aditya birla sun life",
    "icicipru":     "icici prudential",
    "iciciprudential": "icici prudential",
    "ipru":         "icici prudential",
    "hdfcamc":      "hdfc",
    "tatamf":       "tata",
    "miraeasset":   "mirae asset",
    "nipponindia":  "nippon india",
    "parag":        "parag parikh",
    "ppfas":        "parag parikh",
    "ppfasmf":      "parag parikh",
    "whiteoak":     "white oak capital",
    "wocm":         "white oak capital",
    "wocml":        "white oak capital",
    "mosl":         "motilal oswal",
    "motilal":      "motilal oswal",
    "edel":         "edelweiss",
    "barodabnp":    "baroda bnp paribas",
    "bnpparibas":   "baroda bnp paribas",
}


# Tokens that match too many schemes to discriminate usefully. Two groups:
#   (1) English connectives ("of", "the") — show up in queries but not as
#       identifying tokens.
#   (2) Domain-specific suffixes ("fund", "scheme", "regular", "direct",
#       "growth", "idcw") — appear in nearly every Bajaj-recommended scheme
#       name. If we DON'T filter these, a bogus query like
#       "nonexistent-fund-xyzzy" matches every scheme (via "fund") and the
#       fuzzy-lookup no-match path never fires.
_TOKEN_STOPWORDS: Set[str] = {
    # connectives
    "of", "the", "an", "is", "at", "in", "on", "to", "by", "and",
    # domain-noise: appear in virtually every scheme name
    "fund", "scheme", "mf",
    # plan / option markers
    "regular", "direct", "growth", "idcw", "payout", "reinvestment",
}


def _tokenize(text: str) -> List[str]:
    """Lowercase, split on non-alphanumeric, filter stopwords + 1-char tokens.

    Tokens shorter than 2 characters are dropped — they match too many
    schemes spuriously (e.g. a stray "f" matches every "Fund" suffix).
    """
    if not text:
        return []
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 2 and t not in _TOKEN_STOPWORDS]


def _expand_abbreviations(query: str) -> str:
    """Expand brand abbreviations in ``query`` to canonical AMC forms.

    Whole-token replacement only — guards against partial-word collisions
    (e.g. the literal string "absl" embedded in a longer word doesn't
    trigger expansion).
    """
    tokens = re.findall(r"[a-zA-Z0-9]+", query.lower())
    expanded = []
    for tok in tokens:
        expanded.append(_BRAND_ABBREVIATIONS.get(tok, tok))
    return " ".join(expanded)


def _fetch_all_schemes() -> List[Dict[str, Any]]:
    """Return every scheme with a pre-built searchable surface.

    The searchable surface concatenates scheme_name + amc + category so
    word-token scoring picks up matches against any of them. E.g. a query
    of "DSP Multi Cap" scores 3 against "DSP Equity Opportunities Multi
    Cap" (matches DSP via amc, Multi + Cap via category).

    Not cached: 123 rows is cheap to re-fetch (~5ms), and caching across
    test runs would create stale-data hazards. If profiling later shows
    this matters, add caching with explicit per-DB-path invalidation.
    """
    sql = (
        "SELECT scheme_id, scheme_name, amc, category, sub_category, "
        "scheme_name || ' ' || amc || ' ' || category AS searchable "
        "FROM schemes"
    )
    return query_db(sql)


def _score_scheme_matches(
    query: str, limit: int = 10,
) -> List[Dict[str, Any]]:
    """Score every scheme against ``query`` and return the top-N by overlap.

    Algorithm: expand brand abbreviations in the query, tokenize, then
    count how many of those tokens appear in each scheme's searchable
    surface (scheme_name + amc + category). Filter to schemes with score
    >= 1, sort by score descending, tiebreak alphabetical for stability.
    """
    expanded = _expand_abbreviations(query)
    query_tokens = set(_tokenize(expanded))
    if not query_tokens:
        return []

    scored: List[tuple[int, Dict[str, Any]]] = []
    for row in _fetch_all_schemes():
        candidate_tokens = set(_tokenize(row.get("searchable", "")))
        overlap = len(query_tokens & candidate_tokens)
        if overlap > 0:
            scored.append((overlap, row))

    # Sort: score desc, then scheme_name alphabetical for stable tie-break.
    scored.sort(key=lambda item: (-item[0], item[1].get("scheme_name", "")))
    return [
        {
            "scheme_id":   row["scheme_id"],
            "scheme_name": row["scheme_name"],
            "amc":         row["amc"],
            "category":    row["category"],
            "match_score": score,
        }
        for score, row in scored[:limit]
    ]


def _tool_lookup_scheme(arguments: Dict[str, Any]) -> str:
    """Find up to 10 schemes whose name fuzzy-matches the substring.

    Uses word-token scoring against scheme_name + amc + category, with brand
    abbreviation expansion (ABSL -> Aditya Birla Sun Life, etc.). Tolerates
    word-order swaps, partial-name queries, and common abbreviations the
    naive ``LIKE '%X%'`` substring scan would miss.

    Returns a JSON list of ``{scheme_id, scheme_name, amc, category,
    match_score}`` dicts. When nothing matches, returns ``{"matches": [],
    "message": "..."}``  so the LLM has a clearly-shaped no-match signal it
    can pattern-match on.
    """
    needle = arguments.get("name_substring", "")
    if not isinstance(needle, str) or not needle.strip():
        return json.dumps(
            {"error": "bad_arguments", "message": "Missing 'name_substring'."}
        )

    try:
        results = _score_scheme_matches(needle, limit=10)
    except sqlite3.Error as exc:
        return json.dumps({"error": "sql_error", "message": str(exc)})

    if not results:
        return json.dumps(
            {"matches": [], "message": f"No scheme found matching '{needle}'"}
        )
    return json.dumps(results, default=str)


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
    """Return the highest-scoring ``schemes`` row matching the query, or None.

    Uses the same word-token scoring as ``_tool_lookup_scheme`` (brand
    abbreviation expansion + overlap counting) so ``compare_schemes`` and
    ``get_full_snapshot`` benefit from the same matching tolerance as the
    LLM-facing lookup tool.
    """
    results = _score_scheme_matches(name_substring, limit=1)
    return results[0] if results else None


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


def _drop_nulls(d: Dict[str, Any]) -> Dict[str, Any]:
    """Remove keys whose values are ``None`` from a dict.

    Used by ``_tool_get_full_snapshot`` to trim equity-only / debt-only
    field NULLs out of the JSON payload. Saves ~20-30% of payload tokens
    on a typical snapshot section with no information loss — a key being
    absent and being explicitly ``null`` carry the same meaning to the
    model, but the absent version costs ~6 fewer tokens.
    """
    return {k: v for k, v in d.items() if v is not None}


# Sections that get_full_snapshot returns by default. Kept aligned with the
# tool description in the TOOLS schema; if you add a section here, mention
# it in the description so the model knows to ask for it.
_FULL_SNAPSHOT_DEFAULT_SECTIONS: List[str] = [
    "snapshot",
    "benchmark",
    "top_holdings",
    "sector_weights",
    "managers",
    "drawdown",
]

# Curated metric subset returned in the "snapshot" section. We do NOT return
# the full ~80-column fund_snapshots row because some columns are operational
# metadata (parser_version, parse_errors_json, etc.) the model doesn't need.
#
# Coverage matches the SYSTEM_PROMPT schema description in app/prompts.py —
# every column the model is told about should be retrievable from the snapshot
# (with NULL-trim hiding the inapplicable ones per fund type).
#
# Deliberately excluded:
#   - overview, min_investment, exit_load — TEXT/prose columns rarely needed
#     and verbose; fetched on demand via query_db when asked.
#   - composition_json, risk_rating_json, investment_style_json — JSON blob
#     columns; surfaced as separate parsed sections OR via json_extract in
#     query_db.
#   - fund_managers_json — surfaced as the "managers" section.
_FULL_SNAPSHOT_METRIC_COLUMNS: List[str] = [
    # Identifiers
    "as_of_date", "report_month",
    # Header
    "expense_ratio", "fund_aum_cr", "inception_date", "fund_age",
    # Fund trailing returns — full ladder (NULL-trimmed per fund age)
    "return_1m", "return_3m", "return_6m",
    "return_1y", "return_2y", "return_3y", "return_5y", "return_10y",
    "return_since_inception",
    # 1Y risk metrics
    "sharpe_1y", "std_dev_1y", "beta_1y", "r_square_1y",
    "treynor_1y", "info_ratio_1y", "sortino_1y",
    "up_capture_1y", "down_capture_1y", "tracking_error_1y",
    # 3Y risk metrics
    "sharpe_3y", "std_dev_3y", "beta_3y", "r_square_3y",
    "treynor_3y", "info_ratio_3y", "sortino_3y",
    "up_capture_3y", "down_capture_3y", "tracking_error_3y",
    # Market cap composition (equity-side)
    "large_cap_pct", "mid_cap_pct", "small_cap_pct",
    # Portfolio characteristics
    "total_securities", "avg_mkt_cap_cr", "median_mkt_cap_cr",
    "portfolio_pe", "portfolio_pb", "portfolio_div_yield",
    "modified_duration",
    # Debt-side characteristics (NULL-trimmed for equity funds)
    "avg_maturity_years", "yield_to_maturity",
]


def _tool_get_full_snapshot(arguments: Dict[str, Any]) -> str:
    """Return the full per-fund picture in one call.

    Collapses the canonicalize -> fetch snapshot -> maybe-fetch-holdings/sectors
    sequence into a single tool call for single-fund questions. The model
    receives every section it typically needs to answer "is X a buy?" /
    "rationale for X" / "snapshot of X" without further round-trips.

    Arguments:
        scheme_hint: partial or full scheme name. Fuzzy-matched (first-hit
            wins) against the ``schemes`` table.
        include: optional list of section names to return. Defaults to all
            six. Trim when you only need a subset (e.g. ['snapshot'] for a
            pure return question).

    Returns a JSON envelope with ``matched`` (bool) and, on a hit, the
    requested sections plus ``scheme`` metadata. On a miss returns
    ``{"matched": False, "scheme_hint": ..., "message": ...}`` so the model
    can route to an unknown_scheme refusal.
    """
    scheme_hint = arguments.get("scheme_hint", "")
    if not isinstance(scheme_hint, str) or not scheme_hint.strip():
        return json.dumps({
            "error": "bad_arguments",
            "message": "'scheme_hint' must be a non-empty string",
        })

    include_raw = arguments.get("include")
    if include_raw is not None and not isinstance(include_raw, list):
        return json.dumps({
            "error": "bad_arguments",
            "message": "'include' must be a list of section names or omitted",
        })
    requested = set(include_raw) if include_raw else set(_FULL_SNAPSHOT_DEFAULT_SECTIONS)

    try:
        match = _fuzzy_lookup_scheme(scheme_hint)
    except sqlite3.Error as exc:
        return json.dumps({"error": "sql_error", "message": str(exc)})

    if not match:
        return json.dumps({
            "matched": False,
            "scheme_hint": scheme_hint,
            "message": f"No scheme matched '{scheme_hint}'",
        })

    scheme_id = int(match["scheme_id"])

    # Pull full scheme metadata (amc, category, sub_category) — the
    # _fuzzy_lookup_scheme helper returns only scheme_id + scheme_name.
    try:
        scheme_rows = query_db(
            "SELECT scheme_id, scheme_name, amc, category, sub_category, scheme_uid "
            "FROM schemes WHERE scheme_id = ?",
            (scheme_id,),
        )
    except sqlite3.Error as exc:
        return json.dumps({"error": "sql_error", "message": str(exc)})

    scheme_meta = scheme_rows[0] if scheme_rows else dict(match)

    result: Dict[str, Any] = {
        "matched": True,
        "scheme": scheme_meta,
    }

    try:
        snap = _fetch_latest_snapshot(scheme_id)
    except sqlite3.Error as exc:
        result["error"] = f"sql_error: {exc}"
        return json.dumps(result, default=str)

    if not snap:
        # Scheme exists in the master list but has no snapshot loaded yet.
        # Surface this clearly so the model can route to a no_data refusal.
        result["snapshot"] = None
        result["message"] = "scheme matched but no snapshot loaded"
        return json.dumps(result, default=str)

    snapshot_id = int(snap["snapshot_id"])

    if "snapshot" in requested:
        # Drop NULL fields — equity funds carry NULL on debt-only metrics
        # (avg_maturity_years, yield_to_maturity) and vice versa. Without
        # filtering, the snapshot section runs ~30 keys regardless of fund
        # type. Trimming saves ~20-30% of the payload token cost.
        result["snapshot"] = _drop_nulls({
            col: snap.get(col) for col in _FULL_SNAPSHOT_METRIC_COLUMNS
        })

    if "benchmark" in requested:
        alpha: Dict[str, Optional[float]] = {}
        for period in ("1y", "3y", "5y"):
            fund_r = snap.get(f"return_{period}")
            bm_r = snap.get(f"return_{period}_bm")
            if fund_r is not None and bm_r is not None:
                try:
                    alpha[period] = round(float(fund_r) - float(bm_r), 2)
                except (TypeError, ValueError):
                    pass  # skip — leave the period out of alpha entirely
        result["benchmark"] = _drop_nulls({
            "name":         snap.get("benchmark"),
            "return_1y_bm": snap.get("return_1y_bm"),
            "return_3y_bm": snap.get("return_3y_bm"),
            "return_5y_bm": snap.get("return_5y_bm"),
            "alpha":        alpha or None,  # drop the alpha sub-dict if all NULL
        })

    if "drawdown" in requested:
        result["drawdown"] = _drop_nulls({
            "pct":           snap.get("drawdown_pct"),
            "duration_days": snap.get("drawdown_duration_days"),
            "peak_date":     snap.get("drawdown_peak_date"),
            "valley_date":   snap.get("drawdown_valley_date"),
            "recovery_date": snap.get("drawdown_recovery_date"),
        })

    if "managers" in requested:
        managers_raw = snap.get("fund_managers_json")
        managers_list: List[Dict[str, Any]] = []
        if managers_raw:
            try:
                parsed = json.loads(managers_raw) if isinstance(managers_raw, str) else managers_raw
                if isinstance(parsed, list):
                    # Each manager entry can carry NULL qualification or
                    # experience_years — drop those keys per entry.
                    managers_list = [
                        _drop_nulls(m) if isinstance(m, dict) else m
                        for m in parsed
                    ]
            except (json.JSONDecodeError, TypeError):
                managers_list = []
        result["managers"] = managers_list

    if "top_holdings" in requested:
        try:
            holdings_rows = query_db(
                "SELECT security_name, weight_pct, sector, market_cap, instrument_type "
                "FROM holdings "
                "WHERE scheme_id = ? AND report_month = ? "
                "ORDER BY weight_pct DESC LIMIT 10",
                (scheme_id, snap.get("report_month")),
            )
            # Many holdings have NULL market_cap (e.g. derivatives, cash) —
            # trim per-row to keep the payload tight.
            result["top_holdings"] = [_drop_nulls(h) for h in holdings_rows]
        except sqlite3.Error as exc:
            result["top_holdings"] = {"error": f"sql_error: {exc}"}

    if "sector_weights" in requested:
        try:
            sector_rows = query_db(
                "SELECT sector, weight_pct FROM sector_weights "
                "WHERE snapshot_id = ? ORDER BY weight_pct DESC",
                (snapshot_id,),
            )
            result["sector_weights"] = [_drop_nulls(s) for s in sector_rows]
        except sqlite3.Error as exc:
            result["sector_weights"] = {"error": f"sql_error: {exc}"}

    return json.dumps(result, default=str)


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
    "get_full_snapshot": _tool_get_full_snapshot,
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
    {
        "type": "function",
        "function": {
            "name": "get_full_snapshot",
            "description": (
                "Return the FULL per-fund picture in ONE call: latest "
                "snapshot row (returns, Sharpe, std_dev, expense, AUM, "
                "beta, capture ratios, market-cap composition, portfolio "
                "PE/PB, modified duration / YTM), benchmark name + "
                "benchmark returns + alpha (fund minus benchmark) for "
                "1Y/3Y/5Y, top 10 holdings, all sector weights, manager "
                "bios, drawdown. The scheme name is fuzzy-matched "
                "internally — no need to call lookup_scheme first. "
                "PREFER this over lookup_scheme + query_db for ANY "
                "question about ONE specific scheme: 'is X a buy?', "
                "'rationale for X', 'snapshot of X', 'what sectors does "
                "X hold?', 'who manages X?', 'how is X performing vs its "
                "benchmark?'. Do NOT use for cross-fund queries "
                "(rankings, filters, category lists, holdings searches "
                "across funds) — those go to query_db. Do NOT use for "
                "multi-fund comparisons — use compare_schemes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheme_hint": {
                        "type": "string",
                        "description": (
                            "Partial or full scheme name to fuzzy-match. "
                            "Example: 'Canara Robeco Multi Cap'."
                        ),
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of section names. Defaults to "
                            "all six: ['snapshot', 'benchmark', "
                            "'top_holdings', 'sector_weights', 'managers', "
                            "'drawdown']. Trim when you only need a "
                            "subset (e.g. ['snapshot'] for a pure return "
                            "question)."
                        ),
                    },
                },
                "required": ["scheme_hint"],
            },
        },
    },
]
