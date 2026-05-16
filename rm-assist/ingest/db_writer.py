"""DB writer helpers for the ingest layer.

The top-level entry point `insert_snapshot_full` writes a fully-parsed
`Snapshot` to the database. It owns four tables:

  1. `fund_snapshots`  — one row per (scheme, report_month, revision)
  2. `sector_weights`  — normalized sector exposures for that snapshot
  3. `periodic_returns`— normalized monthly / FY / CY returns
  4. `holdings`        — full per-security holdings keyed by (scheme, month)

All four writes happen inside a single `BEGIN ... COMMIT/ROLLBACK`
transaction so that a failure on any step (e.g. UNIQUE constraint on
`fund_snapshots`) rolls back the holdings DELETE — meaning a failed
re-ingest leaves the previous month's data untouched.

The caller is responsible for the final `conn.commit()`; this function
finishes the inner transaction but does not close or reset the
connection's outer state.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ingest.models import FUND_SNAPSHOTS_COLUMNS, Snapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL templates — kept ANSI-portable (no SQLite-isms) so a future pgloader
# migration to Postgres keeps working.
# ---------------------------------------------------------------------------
_SECTOR_WEIGHTS_INSERT = (
    "INSERT INTO sector_weights (snapshot_id, sector, weight_pct) "
    "VALUES (?, ?, ?)"
)

_PERIODIC_RETURNS_INSERT = (
    "INSERT INTO periodic_returns "
    "(snapshot_id, period_type, period_label, return_pct) "
    "VALUES (?, ?, ?, ?)"
)

_HOLDINGS_DELETE = (
    "DELETE FROM holdings WHERE scheme_id = ? AND report_month = ?"
)

_HOLDINGS_INSERT = (
    "INSERT INTO holdings "
    "(scheme_id, report_month, security_name, weight_pct, sector, market_cap, "
    " instrument_type, risk_rating, investment_style, held_since) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def insert_snapshot_full(
    conn: sqlite3.Connection, snap: Snapshot, scheme_id: int,
) -> int:
    """Insert a fund_snapshots row plus all normalized rows.

    Writes (in order) to `fund_snapshots`, `sector_weights`,
    `periodic_returns`, and `holdings`. Returns the new `snapshot_id`.

    All inserts run in a single transaction. On any failure the entire
    transaction rolls back — important because the holdings table is
    first DELETE-d for the (scheme_id, report_month) pair, and we don't
    want to lose those rows if a subsequent INSERT fails.

    Caller is responsible for the final `conn.commit()` outside this
    function if they want the writes durable beyond the open connection.
    (Inside this function we commit so that `lastrowid` semantics are
    stable for callers that close the connection straight away.)

    Args:
        conn: open sqlite3 connection (PRAGMA foreign_keys = ON expected).
        snap: parsed Snapshot. Scratch attrs `sector_weights`,
            `periodic_returns`, `full_holdings` are consumed if non-empty.
        scheme_id: scheme_id from the `schemes` table; written onto the
            snapshot before insert.

    Returns:
        The new snapshot_id (int).

    Raises:
        sqlite3.IntegrityError: typically the UNIQUE (scheme_id,
            report_month, revision) constraint on `fund_snapshots`.
        Any other sqlite3.Error from a downstream write — transaction
            rolls back before the exception propagates.
    """
    snap.scheme_id = scheme_id

    # `with conn:` opens an implicit transaction that auto-commits on
    # clean exit and rolls back on any uncaught exception. This is the
    # mechanism that protects the holdings DELETE: if any later insert
    # raises, the DELETE is undone.
    with conn:
        cur = conn.cursor()

        # --- 1. fund_snapshots ------------------------------------------------
        cols = FUND_SNAPSHOTS_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        sql = (
            f"INSERT INTO fund_snapshots ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        cur.execute(sql, snap.to_db_tuple())
        snapshot_id = int(cur.lastrowid)
        logger.info(
            "fund_snapshots: inserted snapshot_id=%d (scheme_id=%d, "
            "report_month=%s)",
            snapshot_id, scheme_id, snap.report_month,
        )

        # --- 2. sector_weights ------------------------------------------------
        sector_rows = snap.sector_weights or []
        if sector_rows:
            payload = [
                (snapshot_id, r["sector"], r.get("weight_pct"))
                for r in sector_rows
            ]
            cur.executemany(_SECTOR_WEIGHTS_INSERT, payload)
            logger.info("sector_weights: inserted %d rows", len(payload))
        else:
            logger.info("sector_weights: no rows to insert (scratch empty)")

        # --- 3. periodic_returns ----------------------------------------------
        pr_rows = snap.periodic_returns or []
        if pr_rows:
            payload = [
                (
                    snapshot_id,
                    r["period_type"],
                    r["period_label"],
                    r.get("return_pct"),
                )
                for r in pr_rows
            ]
            cur.executemany(_PERIODIC_RETURNS_INSERT, payload)
            logger.info("periodic_returns: inserted %d rows", len(payload))
        else:
            logger.info("periodic_returns: no rows to insert (scratch empty)")

        # --- 4. holdings ------------------------------------------------------
        # Delete-then-insert keyed on (scheme_id, report_month). Wrapped
        # in the same transaction so any failure rolls back the DELETE.
        h_rows = snap.full_holdings or []
        if h_rows:
            cur.execute(_HOLDINGS_DELETE, (scheme_id, snap.report_month))
            deleted = cur.rowcount
            payload = [
                (
                    scheme_id,
                    snap.report_month,
                    r["security_name"],
                    r.get("weight_pct"),
                    r.get("sector"),
                    r.get("market_cap"),
                    r.get("instrument_type"),
                    r.get("risk_rating"),
                    r.get("investment_style"),
                    r.get("held_since"),
                )
                for r in h_rows
            ]
            cur.executemany(_HOLDINGS_INSERT, payload)
            logger.info(
                "holdings: replaced %d existing row(s) with %d new row(s) "
                "for (scheme_id=%d, report_month=%s)",
                deleted, len(payload), scheme_id, snap.report_month,
            )
        else:
            logger.info("holdings: no rows to insert (scratch empty)")

    return snapshot_id


def counts_for_snapshot(snap: Snapshot) -> dict[str, int]:
    """Convenience: return scratch-table sizes for a parsed Snapshot.

    Useful for the ingest CLI's success-message print so we can report
    how many normalized rows were written without re-querying the DB.
    """
    return {
        "sector_weights": len(snap.sector_weights or []),
        "periodic_returns": len(snap.periodic_returns or []),
        "holdings": len(snap.full_holdings or []),
    }


__all__: list[Any] = ["insert_snapshot_full", "counts_for_snapshot"]
