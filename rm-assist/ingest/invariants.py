"""Snapshot invariant checks (Phase 3.3).

Each check is a pure function that takes a `Snapshot` and returns
`(passes, reason_if_fails)`. Checks that find the relevant field absent
return `(True, "n/a — field missing")` so missing data is treated as
"skip" rather than "fail".

The `run_all` helper runs every check and returns named results;
`run_all_and_capture` returns a (all_passed, failure_reasons) tuple
shaped for direct append to `parse_errors_json`.

Invariant failures are warnings (informational for downstream review),
never blockers — they get appended to `parse_errors_json` and the row
is still inserted.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import date
from typing import Callable, List, Optional, Tuple

from ingest.models import Snapshot


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def returns_in_range(snap: Snapshot) -> Tuple[bool, str]:
    """Every non-None `return_*` field must be within [-100, 1000]."""
    bad: list[str] = []
    seen_any = False
    for f in fields(snap):
        if not f.name.startswith("return_"):
            continue
        val = getattr(snap, f.name)
        if val is None:
            continue
        seen_any = True
        if not isinstance(val, (int, float)):
            continue
        if val < -100.0 or val > 1000.0:
            bad.append(f"{f.name}={val}")
    if not seen_any:
        return True, "n/a — field missing"
    if bad:
        return False, f"out-of-range return values: {', '.join(bad)}"
    return True, ""


def composition_sums_to_100(snap: Snapshot) -> Tuple[bool, str]:
    """If `composition_json` is set, parsed values sum to 100 ± 1.0.

    Negative values (arbitrage derivatives margin) are counted toward the
    sum — that's the spec.
    """
    if not snap.composition_json:
        return True, "n/a — field missing"
    try:
        parsed = json.loads(snap.composition_json)
    except (TypeError, ValueError) as e:
        return False, f"composition_json unparseable: {e}"
    if not isinstance(parsed, dict) or not parsed:
        return False, "composition_json empty or not a dict"
    total = sum(float(v) for v in parsed.values() if isinstance(v, (int, float)))
    if abs(total - 100.0) > 1.0:
        return False, f"composition sum {total:.2f} not within 100±1.0"
    return True, ""


def sector_weights_sum_in_range(snap: Snapshot) -> Tuple[bool, str]:
    """Sector weights sum should land in a sanity range of [50, 110].

    Why not "sum to 100"? Finalyca reports sector weights with semantics
    that vary by fund type:
      * Pure-equity funds: sectors sum to ~equity_pct (typically 90–100).
      * Balanced Advantage / Arbitrage funds: sectors sum to *gross*
        equity exposure (can sit in the 90–105 range even when net
        equity is 60–70%, because of derivative-hedged positions).
      * Hybrid/debt funds with row-extraction contamination: sectors
        can exceed 110 when credit-rating buckets (Aa, Government, A1+)
        leak in from the adjacent Risk Rating block on page 2.
      * Pure-debt funds (Liquid, Gilt, Bond) ALSO have a "Sector Wts"
        block, but those rows are bond *issuer* sectors (Government,
        Telecommunication, Others, …) and the sum mirrors gross debt
        exposure — for a leveraged liquid fund this is comfortably >110.
        Skip the invariant for debt-heavy funds (composition.Equity < 30
        or missing entirely) because the [50, 110] range was calibrated
        against equity-template accounting, not debt-template accounting.

    The invariant therefore flags only the two bug-shaped extremes —
    row loss (<<50) and credit-rating contamination (>110) — and stays
    silent on the legitimate-variability middle band, and is skipped
    entirely for pure-debt funds where the sum semantics differ.
    """
    if not snap.sector_weights:
        return True, "n/a — field missing"

    # Skip pure-debt funds: their sector_weights sum mirrors gross debt
    # exposure, not equity exposure, so the equity-calibrated range
    # doesn't apply.
    equity_pct: Optional[float] = None
    if snap.composition_json:
        try:
            comp = json.loads(snap.composition_json)
            if isinstance(comp, dict):
                eq = comp.get("Equity")
                if isinstance(eq, (int, float)):
                    equity_pct = float(eq)
        except (TypeError, ValueError):
            pass
    if equity_pct is None or equity_pct < 30.0:
        return True, "n/a — debt-heavy or no-equity fund (range calibrated for equity)"

    total = 0.0
    for row in snap.sector_weights:
        w = row.get("weight_pct") if isinstance(row, dict) else None
        if isinstance(w, (int, float)):
            total += float(w)
    if total < 50.0 or total > 110.0:
        return False, f"sector weights sum {total:.2f} outside [50, 110]"
    return True, ""


def mkt_cap_composition_sums_to_equity_pct(snap: Snapshot) -> Tuple[bool, str]:
    """large+mid+small + unclassified_equity ≈ composition.Equity ± 5.

    Large/Mid/Small cap percentages are % of *total portfolio* (not % of
    equity), so they sum to the Equity portion of `composition_json` —
    *minus* any equity Finalyca leaves uncategorized in the cap grid.
    Foreign stocks (Microsoft, Nvidia, Alphabet, Sony, …) and REIT/InvIT
    holdings routinely land outside Large/Mid/Small and appear in the
    holdings table with `market_cap = NULL`. The invariant therefore
    adds the weight of those uncategorized-equity holdings before
    comparing against `Equity`.

    Skipped (n/a) when any of the 3 cap fields is None, composition_json
    is missing/unparseable, or composition lacks an "Equity" key.
    """
    parts = (snap.large_cap_pct, snap.mid_cap_pct, snap.small_cap_pct)
    if any(p is None for p in parts):
        return True, "n/a — field missing"
    if not snap.composition_json:
        return True, "n/a — composition_json missing"
    try:
        comp = json.loads(snap.composition_json)
    except (TypeError, ValueError):
        return True, "n/a — composition_json unparseable"
    equity_pct = comp.get("Equity") if isinstance(comp, dict) else None
    if not isinstance(equity_pct, (int, float)):
        return True, "n/a — composition has no Equity key"
    cap_sum = sum(float(p) for p in parts)

    # Equity holdings that Finalyca didn't classify into Large/Mid/Small
    # (typically foreign stocks + REIT/InvIT). If holdings failed to parse
    # entirely, `unclassified` stays 0 and we fall back to the bare cap_sum
    # check — the failure will still surface if the gap is wide.
    unclassified = 0.0
    if snap.full_holdings:
        for h in snap.full_holdings:
            if not isinstance(h, dict):
                continue
            if h.get("instrument_type") != "Equity":
                continue
            if h.get("market_cap"):  # has a Large/Mid/Small bucket already
                continue
            w = h.get("weight_pct")
            if isinstance(w, (int, float)):
                unclassified += float(w)

    total = cap_sum + unclassified
    if abs(total - float(equity_pct)) > 5.0:
        suffix = ""
        if unclassified > 0:
            suffix = f" (cap_sum {cap_sum:.2f} + unclassified equity {unclassified:.2f})"
        return False, (
            f"mkt cap total {total:.2f}{suffix} not within "
            f"composition.Equity {equity_pct:.2f} ± 5.0"
        )
    return True, ""


def holdings_min_count(snap: Snapshot) -> Tuple[bool, str]:
    """If `full_holdings` is set, has at least N entries (N varies by shape).

    Diversified active funds (Multi Cap, Flexi Cap, Large Cap, Mid Cap,
    Small Cap, Focused, etc.) hold 30-200 securities — a count under 5
    indicates a parser miss. But passively-managed Gold funds and
    Fund-of-Fund structures (Gold ETF FoFs, Income Plus Arbitrage FoFs,
    International FoFs) legitimately hold 1-3 securities (a single ETF,
    a single underlying mutual fund, plus a cash sliver). For those we
    only require ≥ 1, which still catches a wholesale parse_holdings_full
    failure (0 rows) while not false-flagging legitimate concentration.
    """
    if not snap.full_holdings:
        return True, "n/a — field missing"
    n = len(snap.full_holdings)

    name_lc = (snap.scheme_name or "").lower()
    sub_lc = (snap.sub_category or "").lower()
    is_concentrated_by_design = (
        "fund of fund" in name_lc
        or "fund of funds" in name_lc
        or "gold" in name_lc
        or "gold" in sub_lc
    )
    min_count = 1 if is_concentrated_by_design else 5
    if n < min_count:
        return False, f"full_holdings has only {n} entries (min {min_count})"
    return True, ""


def expense_ratio_sane(snap: Snapshot) -> Tuple[bool, str]:
    """If `expense_ratio` is set, value is between 0.0 and 5.0."""
    er = snap.expense_ratio
    if er is None:
        return True, "n/a — field missing"
    if er < 0.0 or er > 5.0:
        return False, f"expense_ratio {er} outside [0.0, 5.0]"
    return True, ""


def inception_before_as_of(snap: Snapshot) -> Tuple[bool, str]:
    """If both inception_date and as_of_date are set, inception <= as_of_date."""
    inc = snap.inception_date
    aod = snap.as_of_date
    if inc is None or aod is None:
        return True, "n/a — field missing"
    if not isinstance(inc, date) or not isinstance(aod, date):
        return True, "n/a — non-date values"
    if inc > aod:
        return False, f"inception_date {inc.isoformat()} after as_of_date {aod.isoformat()}"
    return True, ""


# ---------------------------------------------------------------------------
# Registry & runner
# ---------------------------------------------------------------------------

ALL_CHECKS: list[Tuple[str, Callable[[Snapshot], Tuple[bool, str]]]] = [
    ("returns_in_range", returns_in_range),
    ("composition_sums_to_100", composition_sums_to_100),
    ("sector_weights_sum_in_range", sector_weights_sum_in_range),
    ("mkt_cap_composition_sums_to_equity_pct", mkt_cap_composition_sums_to_equity_pct),
    ("holdings_min_count", holdings_min_count),
    ("expense_ratio_sane", expense_ratio_sane),
    ("inception_before_as_of", inception_before_as_of),
]


def run_all(snap: Snapshot) -> List[Tuple[str, bool, str]]:
    """Run every check; return [(name, passed, reason), ...]."""
    out: List[Tuple[str, bool, str]] = []
    for name, fn in ALL_CHECKS:
        try:
            ok, reason = fn(snap)
        except Exception as e:  # pragma: no cover — defensive
            ok, reason = False, f"check raised: {e}"
        out.append((name, ok, reason))
    return out


def run_all_and_capture(snap: Snapshot) -> Tuple[bool, List[str]]:
    """Run every check; return (all_passed, [failure_reasons]).

    Each failure reason is shaped as "<check_name>: <reason>" so callers
    appending to `parse_errors_json` get a single human-readable string
    per failure. Passing checks (including n/a skips) are not surfaced.
    """
    failures: List[str] = []
    for name, ok, reason in run_all(snap):
        if not ok:
            failures.append(f"{name}: {reason}")
    return (not failures), failures
