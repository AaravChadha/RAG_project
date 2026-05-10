"""Bulk-ingest all downloaded PDFs for one report month.

Usage:
    python -m ingest.ingest_month --month 2026-05

For every `data/pdfs/<month>/<scheme_uid>.pdf` file:

  1. Parse the PDF via `parse_pdf` -> (Snapshot, errors).
  2. Resolve `scheme_id` via the scheme_uid encoded in the filename.
  3. Check the existing non-superseded snapshot for that (scheme_id, month):
       * Same `pdf_sha256`  -> skip ("already ingested").
       * Different `pdf_sha256` -> mark prior superseded, bump revision,
         insert the new snapshot.
       * No prior snapshot -> insert with revision=1.

A per-scheme outcome (status, snapshot_id, revision, parse errors,
invariant warnings) is recorded into
`bajaj-mf-bot/data/ingest_report_<month>.json` and a one-line summary
prints at the end.

Missing PDFs are logged but never crash the script — schemes whose
download failed will simply not get a row.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow `python -m ingest.ingest_month` and `python ingest/ingest_month.py`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402
from ingest.db_writer import counts_for_snapshot, insert_snapshot_full  # noqa: E402
from ingest.models import Snapshot  # noqa: E402
from ingest.parse_finalyca import parse_pdf  # noqa: E402

logger = logging.getLogger(__name__)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled (per-connection)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _lookup_scheme(
    conn: sqlite3.Connection, scheme_uid: str,
) -> Optional[tuple[int, str]]:
    """Return (scheme_id, scheme_name) for the given uid, or None."""
    row = conn.execute(
        "SELECT scheme_id, scheme_name FROM schemes WHERE scheme_uid = ?",
        (scheme_uid,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), row[1]


def _existing_snapshot(
    conn: sqlite3.Connection, scheme_id: int, report_month: str,
) -> Optional[dict]:
    """Return {snapshot_id, pdf_sha256, revision} for the current non-superseded
    snapshot of (scheme_id, report_month), or None."""
    row = conn.execute(
        "SELECT snapshot_id, pdf_sha256, revision FROM fund_snapshots "
        "WHERE scheme_id = ? AND report_month = ? AND superseded_at IS NULL",
        (scheme_id, report_month),
    ).fetchone()
    if row is None:
        return None
    return {"snapshot_id": int(row[0]), "pdf_sha256": row[1], "revision": int(row[2])}


def _max_revision(
    conn: sqlite3.Connection, scheme_id: int, report_month: str,
) -> int:
    """Highest revision number for (scheme_id, report_month), 0 if none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM fund_snapshots "
        "WHERE scheme_id = ? AND report_month = ?",
        (scheme_id, report_month),
    ).fetchone()
    return int(row[0])


def _mark_superseded(
    conn: sqlite3.Connection, scheme_id: int, report_month: str,
) -> None:
    """Mark every non-superseded snapshot for (scheme_id, report_month) as
    superseded *now*. Caller commits."""
    conn.execute(
        "UPDATE fund_snapshots SET superseded_at = CURRENT_TIMESTAMP "
        "WHERE scheme_id = ? AND report_month = ? AND superseded_at IS NULL",
        (scheme_id, report_month),
    )


def _ingest_one(
    conn: sqlite3.Connection,
    pdf_path: Path,
    scheme_uid: str,
    report_month: str,
) -> dict:
    """Parse + insert one PDF. Returns an outcome record for the JSON report.

    Outcome statuses:
      - inserted             (new snapshot, revision=1)
      - superseded_inserted  (prior snapshot marked superseded, new revision=N+1)
      - skipped_no_change    (pdf_sha256 matches existing)
      - failed               (parse failed or scheme not in DB)
    """
    outcome: dict = {
        "scheme_uid": scheme_uid,
        "scheme_name": None,
        "pdf_path": str(pdf_path),
        "status": None,
        "snapshot_id": None,
        "revision": None,
        "errors": [],
        "invariant_warnings": [],
        "parse_errors_json": None,
    }

    scheme = _lookup_scheme(conn, scheme_uid)
    if scheme is None:
        outcome["status"] = "failed"
        outcome["errors"].append({
            "section": "lookup",
            "error": f"scheme_uid '{scheme_uid}' not in schemes table",
        })
        logger.warning("no scheme match for uid=%s — skipping", scheme_uid)
        return outcome
    scheme_id, scheme_name = scheme
    outcome["scheme_name"] = scheme_name

    try:
        snap, errors = parse_pdf(pdf_path)
    except Exception as e:  # noqa: BLE001 — surface every failure into the report
        outcome["status"] = "failed"
        outcome["errors"].append({
            "section": "parse_pdf",
            "error": f"{type(e).__name__}: {e}",
        })
        logger.warning("parse failed for %s: %s", pdf_path.name, e)
        return outcome

    # Split per-section errors from invariant warnings — parse_errors_json
    # bundles both into the snapshot row, but the report shows them separately.
    outcome["parse_errors_json"] = snap.parse_errors_json
    if snap.parse_errors_json:
        try:
            payload = json.loads(snap.parse_errors_json)
        except (TypeError, ValueError):
            payload = []
        for record in payload:
            section = record.get("section", "")
            if section.startswith("invariant:"):
                outcome["invariant_warnings"].append(record)
            else:
                outcome["errors"].append(record)
    # Top-level (ParseError) entries returned from parse_pdf — already in the
    # bundle above, but include the section names again for quick scanning.
    for e in errors:
        if not any(
            r.get("section") == e.section and r.get("error") == e.error
            for r in outcome["errors"]
        ):
            outcome["errors"].append({"section": e.section, "error": e.error})

    if not snap.scheme_name or not snap.as_of_date or not snap.pdf_sha256:
        outcome["status"] = "failed"
        outcome["errors"].append({
            "section": "snapshot",
            "error": "missing scheme_name / as_of_date / pdf_sha256 post-parse",
        })
        return outcome

    # The parser hardcodes report_month=2026-05 today, but we override here
    # in case a future month run reuses this script before that constant is
    # parameterised.
    snap.report_month = report_month

    existing = _existing_snapshot(conn, scheme_id, report_month)
    if existing is not None and existing["pdf_sha256"] == snap.pdf_sha256:
        outcome["status"] = "skipped_no_change"
        outcome["snapshot_id"] = existing["snapshot_id"]
        outcome["revision"] = existing["revision"]
        logger.info(
            "skip (sha unchanged): scheme_uid=%s snapshot_id=%d rev=%d",
            scheme_uid, existing["snapshot_id"], existing["revision"],
        )
        return outcome

    if existing is not None:
        # Different content — supersede the prior and bump revision.
        _mark_superseded(conn, scheme_id, report_month)
        snap.revision = _max_revision(conn, scheme_id, report_month) + 1
        conn.commit()
        status_label = "superseded_inserted"
    else:
        snap.revision = 1
        status_label = "inserted"

    try:
        snapshot_id = insert_snapshot_full(conn, snap, scheme_id)
        conn.commit()
    except sqlite3.Error as e:
        outcome["status"] = "failed"
        outcome["errors"].append({
            "section": "insert_snapshot_full",
            "error": f"{type(e).__name__}: {e}",
        })
        logger.warning("DB insert failed for %s: %s", scheme_uid, e)
        return outcome

    counts = counts_for_snapshot(snap)
    outcome["status"] = status_label
    outcome["snapshot_id"] = snapshot_id
    outcome["revision"] = snap.revision
    outcome["normalized_counts"] = counts
    logger.info(
        "%s snapshot_id=%d scheme_uid=%s rev=%d (sectors=%d, returns=%d, holdings=%d)",
        status_label, snapshot_id, scheme_uid, snap.revision,
        counts["sector_weights"], counts["periodic_returns"], counts["holdings"],
    )
    return outcome


def ingest_month(
    month: str, pdf_dir: Path, db_path: Path, report_path: Path,
) -> dict:
    """Iterate every <scheme_uid>.pdf in `pdf_dir` and ingest it.

    The schemes table is also scanned so we can report "missing PDF" for
    schemes whose download failed — those don't show up otherwise.
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()

    pdf_files: list[Path] = []
    if pdf_dir.exists():
        pdf_files = sorted(pdf_dir.glob("*.pdf"))

    parsed = inserted = skipped = superseded = failed = 0
    entries: list[dict] = []

    conn = _connect(db_path)
    try:
        # Build the universe: every scheme in the DB plus every PDF on disk.
        # If a scheme has no PDF we record a "missing_pdf" entry; if a PDF has
        # no matching scheme (debt CSV that hasn't been seeded etc) we still
        # try to ingest and let the lookup fail.
        all_uids_in_db = {
            row[0] for row in conn.execute("SELECT scheme_uid FROM schemes")
        }
        uids_on_disk = {p.stem for p in pdf_files}

        for pdf_path in pdf_files:
            scheme_uid = pdf_path.stem
            parsed += 1
            outcome = _ingest_one(conn, pdf_path, scheme_uid, month)
            status = outcome.get("status")
            if status == "inserted":
                inserted += 1
            elif status == "superseded_inserted":
                superseded += 1
                inserted += 1  # also counts towards "rows now live"
            elif status == "skipped_no_change":
                skipped += 1
            else:
                failed += 1
            entries.append(outcome)

        # Schemes in DB with no PDF on disk — log but never crash.
        missing_uids = sorted(all_uids_in_db - uids_on_disk)
        for uid in missing_uids:
            row = conn.execute(
                "SELECT scheme_name FROM schemes WHERE scheme_uid = ?",
                (uid,),
            ).fetchone()
            scheme_name = row[0] if row else None
            logger.warning("missing PDF for scheme_uid=%s (%s)", uid, scheme_name)
            entries.append({
                "scheme_uid": uid,
                "scheme_name": scheme_name,
                "pdf_path": None,
                "status": "missing_pdf",
                "snapshot_id": None,
                "revision": None,
                "errors": [{"section": "download", "error": "PDF not on disk"}],
                "invariant_warnings": [],
                "parse_errors_json": None,
            })
    finally:
        conn.close()

    elapsed_s = round(time.perf_counter() - start, 1)
    report = {
        "month": month,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdf_dir": str(pdf_dir),
        "db_path": str(db_path),
        "totals": {
            "parsed": parsed,
            "inserted": inserted,
            "skipped_no_change": skipped,
            "superseded": superseded,
            "failed": failed,
            "elapsed_seconds": elapsed_s,
        },
        "entries": entries,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-ingest Finalyca PDFs into fund_snapshots and side-tables.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Report month in YYYY-MM form, e.g. 2026-05.",
    )
    parser.add_argument(
        "--pdf-dir",
        default=None,
        help="Override the source directory (default: config.PDF_ROOT/<month>).",
    )
    parser.add_argument(
        "--db",
        default=str(config.DB_PATH),
        help="Path to the SQLite DB (default: config.DB_PATH).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO; use WARNING to quiet per-fund traces).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else Path(config.PDF_ROOT) / args.month
    db_path = Path(args.db)
    report_path = (
        Path(config.PDF_ROOT).parent / f"ingest_report_{args.month}.json"
    )

    report = ingest_month(args.month, pdf_dir, db_path, report_path)
    t = report["totals"]
    print(
        f"Parsed {t['parsed']}, Inserted {t['inserted']}, Skipped (no change) "
        f"{t['skipped_no_change']}, Superseded {t['superseded']}, Failed "
        f"{t['failed']}. Errors saved to {report_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
