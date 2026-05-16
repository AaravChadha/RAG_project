"""Phase 3.4 golden-sample tests for `parse_pdf`.

Each golden JSON in `tests/golden/` records ~20 hand-extracted critical
fields from one sample PDF. These tests parse the corresponding PDF and
assert that `parse_pdf` produces matching values for every recorded
field. Floats use `pytest.approx(rel=1e-3)`; None must match None;
list-shaped fields (composition keys, full_holdings_count) are checked
by count and set-membership respectively.

The goal is to validate the parser against ground truth that was read
straight off the PDFs by hand, NOT against `parse_pdf`'s own output —
this catches silent regressions where a parser change shifts a value
but the snapshot/regression machinery would still happily diff against
its own previous output.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# Make the rm-assist package root importable regardless of where pytest
# is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.parse_finalyca import parse_pdf  # noqa: E402

# ---------------------------------------------------------------------------
# Golden-file ↔ PDF mapping
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

_CASES: list[tuple[str, Path, Path]] = [
    (
        "canara_robeco_multi_cap",
        _GOLDEN_DIR / "canara_robeco_multi_cap.json",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "Canara Robeco Multi Cap Fund.pdf"
        ),
    ),
    (
        "absl_arbitrage",
        _GOLDEN_DIR / "absl_arbitrage.json",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "2026-05 mutual funds/Aditya Birla SL Arbitrage Fund.pdf"
        ),
    ),
    (
        "dsp_multi_asset",
        _GOLDEN_DIR / "dsp_multi_asset.json",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "2026-05 mutual funds/DSP Multi Asset Allocation Fund.pdf"
        ),
    ),
]

# Direct one-to-one Snapshot attribute fields (float / int / str / None).
# Date fields and the structural fields (managers count, holdings count,
# composition keys) are handled separately below.
_SCALAR_FIELDS = (
    "scheme_name",
    "sub_category",
    "benchmark",
    "expense_ratio",
    "fund_aum_cr",
    "return_1y",
    "return_3y",
    "return_1y_bm",
    "return_3y_bm",
    "std_dev_1y",
    "sharpe_1y",
    "beta_1y",
    "total_securities",
    "portfolio_pe",
    "modified_duration",
    "drawdown_pct",
    "large_cap_pct",
)


def _approx(expected: Any) -> Any:
    """Wrap floats in `pytest.approx(rel=1e-3)`; pass through anything else."""
    if isinstance(expected, float):
        return pytest.approx(expected, rel=1e-3, abs=1e-6)
    return expected


def _check_date(actual: Any, expected_iso: Any, label: str) -> None:
    """Assert a Snapshot date field matches the ISO string in the golden."""
    if expected_iso is None:
        assert actual is None, f"{label}: expected None, got {actual!r}"
        return
    assert actual is not None, f"{label}: expected {expected_iso}, got None"
    if isinstance(actual, date):
        actual_iso = actual.isoformat()
    else:
        actual_iso = str(actual)
    assert actual_iso == expected_iso, (
        f"{label}: expected {expected_iso}, got {actual_iso}"
    )


@pytest.mark.parametrize(
    "case_id,golden_path,pdf_path",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_parser_matches_golden(case_id: str, golden_path: Path, pdf_path: Path) -> None:
    """Parse `pdf_path` and assert every golden field matches."""
    if not pdf_path.exists():
        pytest.skip(f"Sample PDF not found at {pdf_path}")
    if not golden_path.exists():
        pytest.fail(f"Golden file missing: {golden_path}")

    golden = json.loads(golden_path.read_text())
    snap, _errors = parse_pdf(pdf_path)

    # 1. Scalar fields — direct equality, with approx for floats.
    for field in _SCALAR_FIELDS:
        expected = golden.get(field)
        actual = getattr(snap, field)
        if expected is None:
            assert actual is None, (
                f"[{case_id}] {field}: expected None, got {actual!r}"
            )
            continue
        assert actual == _approx(expected), (
            f"[{case_id}] {field}: expected {expected!r}, got {actual!r}"
        )

    # 2. Date fields — compare as ISO strings.
    _check_date(snap.as_of_date, golden.get("as_of_date"), f"[{case_id}] as_of_date")
    _check_date(
        snap.inception_date, golden.get("inception_date"), f"[{case_id}] inception_date"
    )

    # 3. Fund managers count.
    expected_mgr_count = golden.get("fund_managers_count")
    if expected_mgr_count is not None:
        assert snap.fund_managers_json is not None, (
            f"[{case_id}] fund_managers_json is None but golden expects "
            f"{expected_mgr_count} managers"
        )
        managers = json.loads(snap.fund_managers_json)
        assert len(managers) == expected_mgr_count, (
            f"[{case_id}] fund_managers_count: expected "
            f"{expected_mgr_count}, got {len(managers)}"
        )

    # 4. Full holdings count.
    expected_holdings_count = golden.get("full_holdings_count")
    if expected_holdings_count is not None:
        actual_holdings = snap.full_holdings or []
        assert len(actual_holdings) == expected_holdings_count, (
            f"[{case_id}] full_holdings_count: expected "
            f"{expected_holdings_count}, got {len(actual_holdings)}"
        )

    # 5. Composition JSON keys — order-independent set membership.
    expected_keys = golden.get("composition_json_keys")
    if expected_keys is not None:
        assert snap.composition_json is not None, (
            f"[{case_id}] composition_json is None but golden expects keys "
            f"{expected_keys}"
        )
        actual_keys = sorted(json.loads(snap.composition_json).keys())
        assert actual_keys == sorted(expected_keys), (
            f"[{case_id}] composition keys: expected {sorted(expected_keys)}, "
            f"got {actual_keys}"
        )
