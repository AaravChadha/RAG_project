"""Dataclasses for the ingest pipeline.

`Snapshot` mirrors the columns of the `fund_snapshots` table (see
`db/schema.sql`). All fields default to `None` so each section parser can
populate the fields it knows about and leave the rest untouched — the
partial-snapshot pattern. `to_db_tuple()` produces values in INSERT-column
order; `non_null_fields()` is a debug helper for `--dry-run` printing.

`ParseError` records per-section failures without aborting the parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Canonical column order for fund_snapshots INSERT statements.
#
# Excludes auto-managed columns: snapshot_id (PK), ingested_at (DEFAULT).
# Order MUST match `to_db_tuple()` below and the INSERT column list used by
# `ingest_one.py` / `ingest_month.py`.
# ---------------------------------------------------------------------------
FUND_SNAPSHOTS_COLUMNS: tuple[str, ...] = (
    "scheme_id",
    "as_of_date",
    "report_month",
    "revision",
    "superseded_at",
    "parser_version",
    "parse_errors_json",
    "pdf_sha256",
    # Header block
    "benchmark",
    "inception_date",
    "min_investment",
    "expense_ratio",
    "exit_load",
    "fund_aum_cr",
    "fund_age",
    "overview",
    # Trailing returns — fund
    "return_1m", "return_3m", "return_6m",
    "return_1y", "return_2y", "return_3y",
    "return_5y", "return_10y", "return_since_inception",
    # Trailing returns — benchmark
    "return_1m_bm", "return_3m_bm", "return_6m_bm",
    "return_1y_bm", "return_2y_bm", "return_3y_bm",
    "return_5y_bm", "return_10y_bm", "return_since_inception_bm",
    # Risk metrics 1Y
    "std_dev_1y", "sharpe_1y", "beta_1y", "r_square_1y",
    "treynor_1y", "info_ratio_1y", "up_capture_1y",
    "down_capture_1y", "tracking_error_1y", "sortino_1y",
    # Risk metrics 3Y
    "std_dev_3y", "sharpe_3y", "beta_3y", "r_square_3y",
    "treynor_3y", "info_ratio_3y", "up_capture_3y",
    "down_capture_3y", "tracking_error_3y", "sortino_3y",
    # Portfolio characteristics
    "total_securities",
    "avg_mkt_cap_cr",
    "median_mkt_cap_cr",
    "portfolio_pe",
    "portfolio_pb",
    "portfolio_div_yield",
    "modified_duration",
    # Drawdown
    "drawdown_pct",
    "drawdown_duration_days",
    "drawdown_peak_date",
    "drawdown_valley_date",
    "drawdown_recovery_date",
    # Market cap composition
    "large_cap_pct",
    "mid_cap_pct",
    "small_cap_pct",
    # JSON blobs (stored as TEXT)
    "composition_json",
    "risk_rating_json",
    "investment_style_json",
    "fund_managers_json",
    # Provenance
    "source_pdf_path",
)


@dataclass
class Snapshot:
    """In-memory mirror of one `fund_snapshots` row.

    Every field defaults to `None`. Parsers mutate the fields they extract;
    the ingest layer then writes the populated dataclass to the DB.
    """

    # Identity / provenance
    scheme_id: Optional[int] = None
    as_of_date: Optional[date] = None
    report_month: Optional[str] = None
    revision: Optional[int] = None
    superseded_at: Optional[str] = None
    parser_version: Optional[str] = None
    parse_errors_json: Optional[str] = None
    pdf_sha256: Optional[str] = None

    # Header block
    benchmark: Optional[str] = None
    inception_date: Optional[date] = None
    min_investment: Optional[str] = None
    expense_ratio: Optional[float] = None
    exit_load: Optional[str] = None
    fund_aum_cr: Optional[float] = None
    fund_age: Optional[str] = None
    overview: Optional[str] = None

    # Trailing returns — fund
    return_1m: Optional[float] = None
    return_3m: Optional[float] = None
    return_6m: Optional[float] = None
    return_1y: Optional[float] = None
    return_2y: Optional[float] = None
    return_3y: Optional[float] = None
    return_5y: Optional[float] = None
    return_10y: Optional[float] = None
    return_since_inception: Optional[float] = None

    # Trailing returns — benchmark
    return_1m_bm: Optional[float] = None
    return_3m_bm: Optional[float] = None
    return_6m_bm: Optional[float] = None
    return_1y_bm: Optional[float] = None
    return_2y_bm: Optional[float] = None
    return_3y_bm: Optional[float] = None
    return_5y_bm: Optional[float] = None
    return_10y_bm: Optional[float] = None
    return_since_inception_bm: Optional[float] = None

    # Risk metrics 1Y
    std_dev_1y: Optional[float] = None
    sharpe_1y: Optional[float] = None
    beta_1y: Optional[float] = None
    r_square_1y: Optional[float] = None
    treynor_1y: Optional[float] = None
    info_ratio_1y: Optional[float] = None
    up_capture_1y: Optional[float] = None
    down_capture_1y: Optional[float] = None
    tracking_error_1y: Optional[float] = None
    sortino_1y: Optional[float] = None

    # Risk metrics 3Y
    std_dev_3y: Optional[float] = None
    sharpe_3y: Optional[float] = None
    beta_3y: Optional[float] = None
    r_square_3y: Optional[float] = None
    treynor_3y: Optional[float] = None
    info_ratio_3y: Optional[float] = None
    up_capture_3y: Optional[float] = None
    down_capture_3y: Optional[float] = None
    tracking_error_3y: Optional[float] = None
    sortino_3y: Optional[float] = None

    # Portfolio characteristics
    total_securities: Optional[int] = None
    avg_mkt_cap_cr: Optional[float] = None
    median_mkt_cap_cr: Optional[float] = None
    portfolio_pe: Optional[float] = None
    portfolio_pb: Optional[float] = None
    portfolio_div_yield: Optional[float] = None
    modified_duration: Optional[float] = None

    # Drawdown
    drawdown_pct: Optional[float] = None
    drawdown_duration_days: Optional[int] = None
    drawdown_peak_date: Optional[date] = None
    drawdown_valley_date: Optional[date] = None
    drawdown_recovery_date: Optional[date] = None

    # Market cap composition
    large_cap_pct: Optional[float] = None
    mid_cap_pct: Optional[float] = None
    small_cap_pct: Optional[float] = None

    # JSON blobs (stored as TEXT in SQLite)
    composition_json: Optional[str] = None
    risk_rating_json: Optional[str] = None
    investment_style_json: Optional[str] = None
    fund_managers_json: Optional[str] = None

    # Provenance
    source_pdf_path: Optional[str] = None

    # Convenience: non-DB scratch space for parser-name tracking, not persisted
    # to fund_snapshots. `scheme_name` is used by ingest_one for schemes-table
    # lookup; `sub_category` is parsed from the PDF header and is owned by the
    # `schemes` table (the ingest layer can write it back there if desired).
    scheme_name: Optional[str] = field(default=None, metadata={"persist": False})
    sub_category: Optional[str] = field(default=None, metadata={"persist": False})

    # Normalized sector exposures parsed from the "Sector Wts(%)" block. Lives
    # in its own table (`sector_weights`) so the persist=False marker keeps it
    # out of the fund_snapshots tuple. The ingest layer writes these rows after
    # the snapshot insert returns a snapshot_id. Format: list[dict] with keys
    # "sector" (str) and "weight_pct" (float). None ⇒ section absent (debt
    # funds typically have no equity-style sector breakdown).
    sector_weights: Optional[list] = field(default=None, metadata={"persist": False})

    # Top 10 holdings parsed from the page-2 "Top Holdings" block. Stored as a
    # scratch attribute here; the per-row insert into `holdings` (or a future
    # `top_holdings` view) happens in the ingest layer. Format: list[dict]
    # with keys "security_name" (str), "weight_pct" (float), "as_of_date"
    # (ISO date string — may differ from the snapshot's overall as_of_date).
    top_holdings: Optional[list] = field(default=None, metadata={"persist": False})

    # Normalized periodic returns (monthly + FY + CY) parsed from pages 6-7.
    # Lives in its own `periodic_returns` table — the ingest layer writes
    # these rows after the snapshot insert returns a snapshot_id. Format:
    # list[dict] with keys "period_type" ("monthly"|"fy"|"cy"), "period_label"
    # (str like "2025-05", "FY 24", "CY 23"), "return_pct" (Optional[float]).
    # None ⇒ section absent entirely.
    periodic_returns: Optional[list] = field(default=None, metadata={"persist": False})

    # Full per-security holdings (~50-200 rows) parsed from the multi-page
    # "Detailed Portfolio" block. Lives in its own `holdings` table. Format:
    # list[dict] with keys "security_name", "weight_pct", "sector",
    # "market_cap", "instrument_type", "risk_rating", "investment_style",
    # "held_since" (ISO date str or None). Same security can appear twice
    # with different instrument_type — duplicates are intentional, NOT deduped.
    full_holdings: Optional[list] = field(default=None, metadata={"persist": False})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def to_db_tuple(self) -> tuple[Any, ...]:
        """Return values in the exact order of `FUND_SNAPSHOTS_COLUMNS`.

        Dates are converted to ISO strings (SQLite stores DATE as TEXT) so
        the same tuple works against either SQLite or Postgres.
        """
        out: list[Any] = []
        for col in FUND_SNAPSHOTS_COLUMNS:
            val = getattr(self, col)
            if isinstance(val, date):
                val = val.isoformat()
            out.append(val)
        return tuple(out)

    def non_null_fields(self) -> dict[str, Any]:
        """Return {field_name: value} for every field that is not None.

        Used by `--dry-run` to print just the parsed data without a wall
        of `None` entries.
        """
        result: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is None:
                continue
            result[f.name] = val
        return result


@dataclass
class ParseError:
    """One failure record per section that raised during parsing.

    `section` is the parser function name (e.g. `parse_header`); `error` is
    the exception's `str(e)`; `traceback` is the full formatted traceback
    when callers choose to capture it (optional to keep error rows small).
    """

    section: str
    error: str
    traceback: Optional[str] = None
