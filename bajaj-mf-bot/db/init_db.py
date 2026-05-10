"""Initialize (or rebuild) the bajaj_mf.db SQLite database.

Usage:
    python -m db.init_db           # create-if-missing, idempotent re-run safe
    python -m db.init_db --force   # drop the DB file and recreate from scratch

Behaviour:
    * Reads `schema.sql` from this directory and executes it.
    * Enables foreign keys on the connection (SQLite default is OFF).
    * Seeds the `schemes` table from `config.SCHEMES_CSV` if present;
      prints a clear skip warning and exits 0 if the CSV is absent.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# Allow `python db/init_db.py` (script-style) in addition to `python -m db.init_db`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402

SCHEMA_PATH = _THIS_DIR / "schema.sql"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_schema(conn: sqlite3.Connection, schema_path: Path) -> tuple[int, int]:
    """Execute schema.sql against the open connection.

    Returns (table_count, index_count) read back from sqlite_master after apply.
    """
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql)

    # Seed schema_version=1 idempotently (kept out of schema.sql for portability).
    conn.execute(
        "INSERT INTO schema_version (version) "
        "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE version = 1)"
    )
    conn.commit()

    table_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    index_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    return table_count, index_count


_AMC_STOP_TOKENS = ("Cap", "Fund", "Hybrid", "Arbitrage", "Asset", "Allocation")


def _slugify(name: str) -> str:
    """Deterministic slug: lowercase, non-alphanumeric → '-', collapse, strip."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _infer_amc(scheme_name: str) -> str | None:
    """Best-effort: take everything before the first stop-token. None if unsure."""
    for token in _AMC_STOP_TOKENS:
        m = re.search(rf"\b{re.escape(token)}\b", scheme_name)
        if m:
            head = scheme_name[: m.start()].strip()
            head = re.sub(r"\s+", " ", head)
            return head or None
    return None


def seed_schemes(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Seed the schemes table from a CSV with columns: category, scheme, url.

    Returns count of newly inserted rows (0 if the CSV is missing).
    """
    if not csv_path.exists():
        # Caller (init) prints the spec-required phrasing; stay quiet here so
        # we don't double-log when invoked via the CLI.
        return 0

    import csv

    inserted = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            scheme_name = (row.get("scheme") or "").strip()
            if not scheme_name:
                continue
            category = (row.get("category") or "").strip() or None
            url = (row.get("url") or "").strip() or None
            scheme_uid = _slugify(scheme_name)
            amc = _infer_amc(scheme_name)

            cur = conn.execute(
                "INSERT OR IGNORE INTO schemes "
                "(scheme_uid, scheme_name, amc, category, source_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (scheme_uid, scheme_name, amc, category, url),
            )
            inserted += cur.rowcount or 0
    conn.commit()
    return inserted


def init(db_path: Path, schemes_csv: Path, force: bool) -> None:
    """Full init flow used by both __main__ entry-points."""
    if force and db_path.exists():
        print(f"--force: removing existing DB at {db_path}")
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    creating = not db_path.exists()
    if creating:
        print(f"Creating DB at {db_path}")
    else:
        print(f"Opening existing DB at {db_path}")

    conn = _connect(db_path)
    try:
        table_count, index_count = run_schema(conn, SCHEMA_PATH)
        print(f"Running schema.sql ({table_count} tables, {index_count} indexes)")

        n = seed_schemes(conn, schemes_csv)
        if n > 0:
            print(f"Seeded {n} schemes from CSV")
        elif schemes_csv.exists():
            print("Seeded 0 schemes from CSV (already up to date)")
        else:
            # seed_schemes already printed the skip line; add the spec-required phrasing.
            print(f"Skipping seed: schemes_master.csv not found at {schemes_csv}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize (or rebuild) the Bajaj MF research DB."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop the DB file and recreate from scratch before applying schema.",
    )
    args = parser.parse_args(argv)

    init(Path(config.DB_PATH), Path(config.SCHEMES_CSV), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
