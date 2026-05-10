"""Shared pytest fixtures for the bajaj-mf-bot test suite.

The flagship fixture is `seeded_db`: it forces a fresh DB, inserts one
known scheme, parses the Canara Robeco sample PDF to produce a real
Snapshot, attaches the scheme_id, and writes the snapshot row. This
gives every test a known-good single-fund dataset so `ask()` can hit
real data.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Make the bajaj-mf-bot package root importable regardless of where pytest
# is invoked from (e.g. `pytest tests/` vs `pytest bajaj-mf-bot/tests/`).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from db import init_db  # noqa: E402
from ingest.models import FUND_SNAPSHOTS_COLUMNS  # noqa: E402
from ingest.parse_finalyca import parse_pdf_minimal  # noqa: E402

# Sample PDF path is hardcoded for the spine; Phase 4 will load a manifest.
_CANARA_PDF = Path(
    "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/"
    "Canara Robeco Multi Cap Fund.pdf"
)

_SEED_SCHEME = {
    "scheme_name": "Canara Robeco Multi Cap Fund - Regular (G)",
    "amc": "Canara Robeco",
    "category": "Multi Cap",
    "scheme_uid": "canara-robeco-multi-cap-fund-regular-g",
    "source_url": (
        "https://research-host.example/Recommended/"
        "Canara%20Robeco%20Multi%20Cap%20Fund.pdf"
    ),
}


def _insert_snapshot(conn: sqlite3.Connection, snap) -> int:
    """Insert a parsed Snapshot using the canonical column-order tuple."""
    cols = FUND_SNAPSHOTS_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    sql = (
        f"INSERT INTO fund_snapshots ({', '.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    cur = conn.execute(sql, snap.to_db_tuple())
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def seeded_db() -> Iterator[Path]:
    """Force-rebuild the DB, insert one scheme + one parsed snapshot.

    Yields the `config.DB_PATH` so tests have a handle to the file if they
    need to open their own connection (e.g. for the `query_log` checks).
    """
    if not _CANARA_PDF.exists():
        pytest.skip(f"Sample PDF not found at {_CANARA_PDF}")

    # 1. Wipe & recreate the DB. We bypass any seeded CSV by pointing at a
    #    path that doesn't exist — the test owns the entire dataset.
    init_db.init(
        db_path=Path(config.DB_PATH),
        schemes_csv=Path("/nonexistent-schemes-csv-for-tests.csv"),
        force=True,
    )

    # 2. Insert the known scheme.
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            """
            INSERT INTO schemes (scheme_name, amc, category, scheme_uid, source_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _SEED_SCHEME["scheme_name"],
                _SEED_SCHEME["amc"],
                _SEED_SCHEME["category"],
                _SEED_SCHEME["scheme_uid"],
                _SEED_SCHEME["source_url"],
            ),
        )
        conn.commit()

        # 3. Parse the sample PDF and stitch the snapshot's scheme_id from
        #    the row we just inserted.
        snap = parse_pdf_minimal(_CANARA_PDF)
        row = conn.execute(
            "SELECT scheme_id FROM schemes WHERE scheme_uid = ?",
            (_SEED_SCHEME["scheme_uid"],),
        ).fetchone()
        snap.scheme_id = int(row[0])

        # 4. Insert the snapshot.
        _insert_snapshot(conn, snap)
    finally:
        conn.close()

    yield Path(config.DB_PATH)
    # Teardown is optional — the next test that needs a clean slate will
    # call this fixture again, which force-recreates the DB.
