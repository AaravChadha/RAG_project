"""Tests for `ingest.db_writer.insert_snapshot_full`.

The `seeded_db` fixture (see `conftest.py`) parses the Canara Robeco
sample PDF and writes the snapshot + normalized side-tables. These
tests then query the resulting DB to verify each side-table got
populated and that re-ingest of the same (scheme_id, report_month)
revision is rejected cleanly (UNIQUE constraint) WITHOUT corrupting
the holdings rows that were inserted on the first pass.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Package-root importability — mirrors conftest.py.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from ingest.db_writer import insert_snapshot_full  # noqa: E402
from ingest.parse_finalyca import parse_pdf  # noqa: E402

# Sample PDF — same path as conftest; we re-parse for the re-ingest test.
_CANARA_PDF = Path(
    "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
    "Canara Robeco Multi Cap Fund.pdf"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Run a single-value scalar query and return the integer result."""
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _snapshot_id(conn: sqlite3.Connection) -> int:
    """Return the snapshot_id of the one row in fund_snapshots (the
    `seeded_db` fixture inserts exactly one)."""
    return _scalar(conn, "SELECT snapshot_id FROM fund_snapshots LIMIT 1")


# ---------------------------------------------------------------------------
# 1. sector_weights populated
# ---------------------------------------------------------------------------
def test_sector_weights_populated(seeded_db: Path) -> None:
    """Canara Robeco's PDF has ~19 sectors; we accept >= 10 as a floor."""
    conn = sqlite3.connect(str(seeded_db))
    try:
        snap_id = _snapshot_id(conn)
        count = _scalar(
            conn,
            "SELECT COUNT(*) FROM sector_weights WHERE snapshot_id = ?",
            (snap_id,),
        )
    finally:
        conn.close()
    assert count >= 10, f"expected >= 10 sector_weights rows, got {count}"


# ---------------------------------------------------------------------------
# 2. periodic_returns populated
# ---------------------------------------------------------------------------
def test_periodic_returns_populated(seeded_db: Path) -> None:
    """At least 12 monthly entries (one full year) should land."""
    conn = sqlite3.connect(str(seeded_db))
    try:
        snap_id = _snapshot_id(conn)
        count = _scalar(
            conn,
            "SELECT COUNT(*) FROM periodic_returns WHERE snapshot_id = ?",
            (snap_id,),
        )
    finally:
        conn.close()
    assert count >= 12, f"expected >= 12 periodic_returns rows, got {count}"


# ---------------------------------------------------------------------------
# 3. holdings populated
# ---------------------------------------------------------------------------
def test_holdings_populated(seeded_db: Path) -> None:
    """Canara has ~104 holdings; accept >= 50 as a generous floor."""
    conn = sqlite3.connect(str(seeded_db))
    try:
        # Look up the (scheme_id, report_month) tuple from fund_snapshots
        # so the test stays decoupled from any specific id values.
        row = conn.execute(
            "SELECT scheme_id, report_month FROM fund_snapshots LIMIT 1"
        ).fetchone()
        scheme_id, report_month = int(row[0]), row[1]
        count = _scalar(
            conn,
            "SELECT COUNT(*) FROM holdings "
            "WHERE scheme_id = ? AND report_month = ?",
            (scheme_id, report_month),
        )
    finally:
        conn.close()
    assert count >= 50, f"expected >= 50 holdings rows, got {count}"


# ---------------------------------------------------------------------------
# 4. holdings queryable by security name (the Phase 5 use case)
# ---------------------------------------------------------------------------
def test_holdings_query_by_security(seeded_db: Path) -> None:
    """`which funds hold HDFC Bank?` — Canara does. Must return >= 1 row."""
    conn = sqlite3.connect(str(seeded_db))
    try:
        rows = conn.execute(
            "SELECT scheme_id FROM holdings "
            "WHERE LOWER(security_name) LIKE '%hdfc bank%'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) >= 1, (
        "expected >= 1 holdings row matching 'HDFC Bank', got 0"
    )


# ---------------------------------------------------------------------------
# 5. sector_weights queryable by sector name
# ---------------------------------------------------------------------------
def test_sector_weights_query_by_sector(seeded_db: Path) -> None:
    """Canara's #1 sector is Financial Services."""
    conn = sqlite3.connect(str(seeded_db))
    try:
        snap_id = _snapshot_id(conn)
        row = conn.execute(
            "SELECT sector FROM sector_weights "
            "WHERE snapshot_id = ? "
            "ORDER BY weight_pct DESC LIMIT 1",
            (snap_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no sector_weights rows returned"
    assert row[0] == "Financial Services", (
        f"expected top sector 'Financial Services', got '{row[0]}'"
    )


# ---------------------------------------------------------------------------
# 6. Re-ingest is rejected by UNIQUE constraint and the transaction rolls
#    back — meaning the holdings DELETE that runs inside the same txn must
#    be undone. This is the bug-fix verification: if the holdings DELETE
#    were committed before the fund_snapshots insert, the count would drop
#    to zero after the failed second call.
# ---------------------------------------------------------------------------
def test_idempotent_re_ingest(seeded_db: Path) -> None:
    """A second call to `insert_snapshot_full` with the same (scheme,
    report_month, revision) must:
      (a) raise sqlite3.IntegrityError on the fund_snapshots UNIQUE,
      (b) leave the holdings table unchanged (rollback of the DELETE).
    """
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        row = conn.execute(
            "SELECT scheme_id, report_month FROM fund_snapshots LIMIT 1"
        ).fetchone()
        scheme_id, report_month = int(row[0]), row[1]

        before_count = _scalar(
            conn,
            "SELECT COUNT(*) FROM holdings "
            "WHERE scheme_id = ? AND report_month = ?",
            (scheme_id, report_month),
        )
        assert before_count > 0, "fixture didn't populate holdings"

        # Re-parse the PDF to get a fresh snapshot (same logical row).
        snap2, _errors = parse_pdf(_CANARA_PDF)

        # Expect IntegrityError on UNIQUE (scheme_id, report_month, revision).
        with pytest.raises(sqlite3.IntegrityError):
            insert_snapshot_full(conn, snap2, scheme_id)

        # Critical: holdings rows must still be present — the DELETE
        # inside the failed transaction MUST have rolled back.
        after_count = _scalar(
            conn,
            "SELECT COUNT(*) FROM holdings "
            "WHERE scheme_id = ? AND report_month = ?",
            (scheme_id, report_month),
        )
        assert after_count == before_count, (
            f"holdings rowcount changed after failed re-ingest: "
            f"{before_count} -> {after_count}"
        )
    finally:
        conn.close()
