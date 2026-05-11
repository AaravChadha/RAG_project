"""Shared pytest fixtures for the bajaj-mf-bot test suite.

The flagship fixture is `seeded_db`: it forces a fresh DB, inserts one
known scheme, parses the Canara Robeco sample PDF to produce a real
Snapshot, attaches the scheme_id, and writes the snapshot row PLUS the
normalized side-tables (`sector_weights`, `periodic_returns`,
`holdings`). This gives every test a known-good single-fund dataset so
`ask()` can hit real data for both the snapshot-level questions and
the Phase 5 holdings/sector questions.
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
from ingest.db_writer import insert_snapshot_full  # noqa: E402
from ingest.parse_finalyca import parse_pdf  # noqa: E402

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


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Force-rebuild a TEMP DB, insert one scheme + one parsed snapshot.

    **Test isolation note**: this fixture creates the DB inside `tmp_path`
    (pytest's per-test temp directory) and monkeypatches `config.DB_PATH`
    so every other module that reads it at call time (db_query.query_db,
    db_query.log_query, chatbot.ask, etc.) sees the temp file. The
    production DB at `bajaj-mf-bot/db/bajaj_mf.db` is never touched by
    the test suite — you can run `pytest` between bulk ingests without
    wiping your 90-scheme dataset.

    Uses the full `parse_pdf` (not `parse_pdf_minimal`) so the scratch
    attributes (`sector_weights`, `periodic_returns`, `full_holdings`)
    are populated — `insert_snapshot_full` consumes those and writes the
    normalized side-tables. After this fixture runs the test DB contains
    rows in `schemes`, `fund_snapshots`, `sector_weights`,
    `periodic_returns`, and `holdings` for Canara Robeco.

    Yields the test DB path so tests can open their own connection if
    they need to (e.g. for `query_log` checks).
    """
    if not _CANARA_PDF.exists():
        pytest.skip(f"Sample PDF not found at {_CANARA_PDF}")

    test_db_path = tmp_path / "test_bajaj_mf.db"
    monkeypatch.setattr(config, "DB_PATH", test_db_path)

    # 1. Create the temp DB from scratch. Point at a nonexistent CSV so
    #    `init` skips its seed step — the test owns the entire dataset.
    init_db.init(
        db_path=test_db_path,
        schemes_csv=Path("/nonexistent-schemes-csv-for-tests.csv"),
        force=True,
    )

    # 2. Insert the known scheme.
    conn = sqlite3.connect(str(test_db_path))
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

        # 3. Parse the sample PDF (full parse — populates scratch attrs
        #    for sector_weights / periodic_returns / full_holdings).
        snap, _errors = parse_pdf(_CANARA_PDF)
        row = conn.execute(
            "SELECT scheme_id FROM schemes WHERE scheme_uid = ?",
            (_SEED_SCHEME["scheme_uid"],),
        ).fetchone()
        scheme_id = int(row[0])

        # 4. Insert the snapshot + normalized side-tables in one shot.
        insert_snapshot_full(conn, snap, scheme_id)
        conn.commit()
    finally:
        conn.close()

    yield test_db_path
    # Teardown: pytest cleans up `tmp_path` automatically. monkeypatch
    # restores `config.DB_PATH` so the next test (or post-test process)
    # sees the real production path again.
