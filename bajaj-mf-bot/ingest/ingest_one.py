"""Ingest a single Finalyca PDF — CLI entrypoint for Phase 1.3.

Usage:
    python -m ingest.ingest_one <pdf_path>            # parse + INSERT
    python -m ingest.ingest_one <pdf_path> --dry-run  # parse + pretty-print, no DB write

The `--dry-run` flag is the eyeball path: it parses the PDF, prints the
non-null fields of the Snapshot to stdout, and exits 0. It NEVER touches
the database, so it works before the schemes table has been seeded.

Without `--dry-run`, the CLI looks up `scheme_id` from the `schemes`
table by `LIKE` match on `scheme_name`. If no match (because
`schemes_master.csv` hasn't been seeded yet — common in early Phase 1),
the CLI prints a clear remediation message and exits 1.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Allow `python -m ingest.ingest_one` AND `python ingest/ingest_one.py`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402
from ingest.db_writer import counts_for_snapshot, insert_snapshot_full  # noqa: E402
from ingest.models import Snapshot  # noqa: E402
from ingest.parse_finalyca import parse_pdf_minimal  # noqa: E402


def _format_dry_run(snap: Snapshot) -> str:
    """Aligned key/value listing of the snapshot's non-null fields."""
    rows = snap.non_null_fields()
    if not rows:
        return "(no fields populated)"
    width = max(len(k) for k in rows)
    lines = ["Parsed snapshot (non-null fields):", "-" * 60]
    for key, value in rows.items():
        lines.append(f"  {key.ljust(width)}  {value}")
    lines.append("-" * 60)
    lines.append(f"  total populated: {len(rows)}")
    return "\n".join(lines)


def _lookup_scheme_id(conn: sqlite3.Connection, scheme_name: str) -> int | None:
    """Return the matching scheme_id or None.

    Tries exact match first; falls back to LIKE %name% then a stripped form
    (the PDF title may include " - Regular (G)" suffix that the master CSV
    omits, or vice-versa).
    """
    row = conn.execute(
        "SELECT scheme_id FROM schemes WHERE scheme_name = ? LIMIT 1",
        (scheme_name,),
    ).fetchone()
    if row:
        return row[0]

    row = conn.execute(
        "SELECT scheme_id FROM schemes WHERE scheme_name LIKE ? LIMIT 1",
        (f"%{scheme_name}%",),
    ).fetchone()
    if row:
        return row[0]

    # Strip plan-variant suffixes for the fuzzy attempt.
    stripped = scheme_name
    for suffix in (" - Regular (G)", " - Regular", " (G)", " - Direct (G)"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
    if stripped != scheme_name:
        row = conn.execute(
            "SELECT scheme_id FROM schemes WHERE scheme_name LIKE ? LIMIT 1",
            (f"%{stripped}%",),
        ).fetchone()
        if row:
            return row[0]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse one Finalyca PDF and ingest into fund_snapshots.",
    )
    parser.add_argument("pdf_path", help="Path to the source PDF.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print the snapshot; do NOT touch the database.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Logging level (default: WARNING; use INFO/DEBUG for parser traces).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    pdf_path = Path(args.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    try:
        snap = parse_pdf_minimal(pdf_path)
    except ValueError as e:
        print(f"Parse failed: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(_format_dry_run(snap))
        return 0

    # Live ingest path — look up scheme_id, then INSERT.
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        scheme_id = _lookup_scheme_id(conn, snap.scheme_name or "")
        if scheme_id is None:
            print(
                f"Cannot insert: scheme '{snap.scheme_name}' not in schemes "
                "table. Run db init with seeded CSV first, OR use --dry-run "
                "to preview the parse.",
                file=sys.stderr,
            )
            return 1
        snapshot_id = insert_snapshot_full(conn, snap, scheme_id)
        conn.commit()
        counts = counts_for_snapshot(snap)
        print(
            f"Inserted snapshot_id={snapshot_id} for scheme_id={scheme_id} "
            f"({snap.scheme_name}) report_month={snap.report_month} "
            f"({counts['sector_weights']} sector_weights, "
            f"{counts['periodic_returns']} periodic_returns, "
            f"{counts['holdings']} holdings)"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
