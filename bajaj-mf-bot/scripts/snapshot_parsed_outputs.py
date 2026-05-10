"""Phase 3.5 — snapshot parsed Snapshot outputs to disk as JSON baselines.

Runs `parse_pdf` against each of the 3 sample PDFs and dumps the
populated Snapshot (`non_null_fields()` plus the scratch list/JSON
attributes) to `tests/snapshots/<scheme_slug>.json`. These files are
the regression baseline for `tests/test_parser_regression.py`.

The dump is JSON-serializable: dates are ISO-stringified, JSON columns
(`composition_json` etc.) are re-parsed back into dicts/lists so a
trivial whitespace change in `json.dumps` doesn't produce false diffs.

Usage (from `bajaj-mf-bot/`):
    python scripts/snapshot_parsed_outputs.py

Re-run after intentional parser changes to refresh the baseline.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Make the bajaj-mf-bot package root importable when invoked as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.parse_finalyca import parse_pdf  # noqa: E402

# Sample PDFs to baseline — kept in sync with `tests/test_parser_golden.py`.
SAMPLE_PDFS: list[tuple[str, Path]] = [
    (
        "canara_robeco_multi_cap",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "Canara Robeco Multi Cap Fund.pdf"
        ),
    ),
    (
        "absl_arbitrage",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "2026-05 mutual funds/Aditya Birla SL Arbitrage Fund.pdf"
        ),
    ),
    (
        "dsp_multi_asset",
        Path(
            "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
            "2026-05 mutual funds/DSP Multi Asset Allocation Fund.pdf"
        ),
    ),
]

SNAPSHOTS_DIR = ROOT / "tests" / "snapshots"

# Fields that vary by file location / run timing and must be stripped from
# both the baseline AND the comparison value. Mirrored in the regression
# test's `IGNORE_FIELDS` constant.
IGNORE_FIELDS: frozenset[str] = frozenset({
    "pdf_sha256",
    "source_pdf_path",
    "ingested_at",
})

# JSON-encoded TEXT columns we expand back into dicts/lists for diff
# stability — small whitespace shifts in `json.dumps` shouldn't trip the
# regression check.
JSON_TEXT_FIELDS: tuple[str, ...] = (
    "composition_json",
    "risk_rating_json",
    "investment_style_json",
    "fund_managers_json",
    "parse_errors_json",
)


def _jsonable(value: Any) -> Any:
    """Convert a Snapshot field value into something `json.dumps` can handle."""
    if isinstance(value, date):
        return value.isoformat()
    return value


def snapshot_to_dict(snap_obj: Any) -> dict[str, Any]:
    """Project a parsed Snapshot to a JSON-serializable dict."""
    out: dict[str, Any] = {}
    fields_present = snap_obj.non_null_fields()
    for name, val in fields_present.items():
        if name in IGNORE_FIELDS:
            continue
        if name in JSON_TEXT_FIELDS and isinstance(val, str):
            # Re-parse so diff is structural rather than whitespace-sensitive.
            try:
                out[name] = json.loads(val)
            except (TypeError, ValueError):
                out[name] = val
            continue
        out[name] = _jsonable(val)
    return out


def _slugify(name: str) -> str:
    """Tolerant fallback slugifier (not used by current call sites but kept for re-use)."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "scheme"


def main() -> int:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for slug, pdf_path in SAMPLE_PDFS:
        if not pdf_path.exists():
            print(f"SKIP {slug}: PDF not found at {pdf_path}", file=sys.stderr)
            continue
        snap, errors = parse_pdf(pdf_path)
        payload = snapshot_to_dict(snap)
        out_path = SNAPSHOTS_DIR / f"{slug}.json"
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        print(
            f"OK   {slug}: {len(payload)} non-null fields, "
            f"{len(errors)} section errors -> {out_path.relative_to(ROOT)}"
        )
        written += 1
    print(f"\nWrote {written} snapshot baseline(s) to {SNAPSHOTS_DIR.relative_to(ROOT)}")
    return 0 if written == len(SAMPLE_PDFS) else 1


if __name__ == "__main__":
    sys.exit(main())
