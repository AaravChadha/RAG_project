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
from typing import Callable, List, Tuple

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


def sector_weights_sum_close(snap: Snapshot) -> Tuple[bool, str]:
    """If `sector_weights` is set, weights sum to 100 ± 5.0."""
    if not snap.sector_weights:
        return True, "n/a — field missing"
    total = 0.0
    for row in snap.sector_weights:
        w = row.get("weight_pct") if isinstance(row, dict) else None
        if isinstance(w, (int, float)):
            total += float(w)
    if abs(total - 100.0) > 5.0:
        return False, f"sector weights sum {total:.2f} not within 100±5.0"
    return True, ""


def mkt_cap_composition_sums_to_100(snap: Snapshot) -> Tuple[bool, str]:
    """If all 3 of large/mid/small cap pct are set, sum is 100 ± 5.0.

    Loose tolerance because some funds have unrated / cash holdings that
    aren't counted in the cap breakdown. If any of the 3 is None, the
    check is skipped (n/a).
    """
    parts = (snap.large_cap_pct, snap.mid_cap_pct, snap.small_cap_pct)
    if any(p is None for p in parts):
        return True, "n/a — field missing"
    total = sum(float(p) for p in parts)
    if abs(total - 100.0) > 5.0:
        return False, f"mkt cap composition sum {total:.2f} not within 100±5.0"
    return True, ""


def holdings_min_count(snap: Snapshot) -> Tuple[bool, str]:
    """If `full_holdings` is set, has at least 5 entries."""
    if not snap.full_holdings:
        return True, "n/a — field missing"
    n = len(snap.full_holdings)
    if n < 5:
        return False, f"full_holdings has only {n} entries (min 5)"
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
    ("sector_weights_sum_close", sector_weights_sum_close),
    ("mkt_cap_composition_sums_to_100", mkt_cap_composition_sums_to_100),
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
