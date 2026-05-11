"""Read-only SQLite query layer + audit-log writer.

Two surfaces:

* `query_db(sql, params)` — the *only* path the chatbot uses to read data.
  Opens the SQLite DB in `mode=ro` (the kernel rejects writes even if the
  SQL slipped past our regex) and additionally refuses any query that
  contains a DDL/DML token. Belt-and-braces: we never want a future tool
  description bug to let the model write to the DB.

* `log_query(...)` — the chatbot's only writable path. Opens a *separate*
  writable connection just for the `query_log` insert, so the read path
  cannot be repurposed for writes. Returns the new `query_id` for
  downstream UI affordances (thumbs-up/down linking).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow `python -m retrieval.db_query` and direct imports.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402

logger = logging.getLogger(__name__)


# Tokens that flag a non-read-only statement. Matched as whole words via
# `\b` so column names like `created_at` don't trip the `CREATE` guard.
_FORBIDDEN_TOKENS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "REPLACE", "TRUNCATE", "ATTACH", "DETACH", "PRAGMA",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_TOKENS) + r")\b",
    re.IGNORECASE,
)


def _assert_read_only(sql: str) -> None:
    """Raise ValueError if `sql` contains any DDL/DML keyword."""
    m = _FORBIDDEN_RE.search(sql)
    if m:
        token = m.group(1).upper()
        raise ValueError(f"Refusing non-read-only SQL: {token}")


def query_db(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Execute a read-only SQL query and return rows as a list of dicts.

    Args:
        sql: SELECT statement. Must not contain any DDL/DML keyword.
        params: Parameter tuple for `?` placeholders.

    Returns:
        Each row is a dict keyed by column name.

    Raises:
        ValueError: If `sql` contains a forbidden token (case-insensitive,
            whole-word match) — e.g. `INSERT`, `DROP`, `PRAGMA`.
    """
    _assert_read_only(sql)

    db_uri = f"file:{config.DB_PATH}?mode=ro"
    logger.debug("query_db: %s | params=%s", sql, params)
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_query_feedback(
    query_id: int,
    feedback: str,
    comment: Optional[str] = None,
) -> None:
    """Update ``user_feedback`` and ``feedback_comment`` for a query_log row.

    Used by the Streamlit UI (Phase 6) when an RM clicks thumbs-up /
    thumbs-down under an assistant message. Writes are upserts on the
    existing row keyed by ``query_id`` — the row is guaranteed to exist
    because ``log_query`` ran synchronously before the UI could render
    the buttons.

    Args:
        query_id: Primary key from ``log_query()`` return value.
        feedback: ``"thumbs_up"`` or ``"thumbs_down"``. Any string is
            accepted at the SQL layer; the UI is responsible for the
            two-value convention.
        comment: Optional free-text comment, typically only attached to
            thumbs-down. ``None`` leaves the column NULL.

    Raises:
        ValueError: If ``query_id`` does not match any row in query_log.
    """
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        cur = conn.execute(
            """
            UPDATE query_log
               SET user_feedback = ?,
                   feedback_comment = ?
             WHERE query_id = ?
            """,
            (feedback, comment, query_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"No query_log row found for query_id={query_id}")
    finally:
        conn.close()


def log_query(
    question: str,
    sql: Optional[str],
    answer: str,
    model_name: str,
    tool_calls_json: Optional[str] = None,
    latency_ms: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    refusal_reason: Optional[str] = None,
    user_id: Optional[str] = None,
) -> int:
    """Insert one row into `query_log`. Returns the new `query_id`.

    Uses a separate writable connection (NOT the read-only one used by
    `query_db`) so the read path cannot accidentally pick up write
    permissions.
    """
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        cur = conn.execute(
            """
            INSERT INTO query_log (
                user_id, question, sql_executed, final_answer,
                tool_calls_json, model_name, latency_ms,
                tokens_in, tokens_out, refusal_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, question, sql, answer,
                tool_calls_json, model_name, latency_ms,
                tokens_in, tokens_out, refusal_reason,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()
