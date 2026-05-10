"""Phase 3.5 regression baseline tests.

For each sample PDF, parse it fresh, project the Snapshot via the same
helper used by `scripts/snapshot_parsed_outputs.py`, and `deepdiff` the
result against the committed baseline in `tests/snapshots/`. Any
unexpected delta fails the test.

If a parser change is intentional, re-run `python
scripts/snapshot_parsed_outputs.py` and commit the regenerated baseline
JSONs alongside the parser change. The test will then pass against the
new baseline.

Fields that are path- or run-time dependent (`pdf_sha256`,
`source_pdf_path`, `ingested_at`) are stripped on both sides via the
shared `IGNORE_FIELDS` constant in the snapshot script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from deepdiff import DeepDiff

# Make the bajaj-mf-bot package root importable regardless of where pytest
# is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.parse_finalyca import parse_pdf  # noqa: E402
from scripts.snapshot_parsed_outputs import (  # noqa: E402
    SAMPLE_PDFS,
    SNAPSHOTS_DIR,
    snapshot_to_dict,
)


@pytest.mark.parametrize(
    "slug,pdf_path",
    SAMPLE_PDFS,
    ids=[s for s, _ in SAMPLE_PDFS],
)
def test_parsed_snapshot_matches_baseline(slug: str, pdf_path: Path) -> None:
    """Parse `pdf_path` fresh, diff against the committed baseline."""
    if not pdf_path.exists():
        pytest.skip(f"Sample PDF not found at {pdf_path}")

    baseline_path = SNAPSHOTS_DIR / f"{slug}.json"
    if not baseline_path.exists():
        pytest.fail(
            f"Baseline snapshot missing: {baseline_path}. "
            f"Run `python scripts/snapshot_parsed_outputs.py` to generate it."
        )

    baseline = json.loads(baseline_path.read_text())
    snap, _errors = parse_pdf(pdf_path)
    current = snapshot_to_dict(snap)

    diff = DeepDiff(baseline, current, ignore_order=True, significant_digits=6)
    assert not diff, (
        f"[{slug}] parser output diverges from baseline at {baseline_path}.\n"
        f"Diff: {diff.to_json(indent=2)}\n"
        f"If this change is intentional, re-run "
        f"`python scripts/snapshot_parsed_outputs.py` to refresh the baseline."
    )
