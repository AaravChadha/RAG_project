-- ============================================================================
-- Bajaj Capital MF research bot — database schema (v1)
-- ----------------------------------------------------------------------------
-- ANSI-portable SQL. Idempotent: every CREATE uses IF NOT EXISTS.
-- No SQLite-isms (no WITHOUT ROWID, no JSON1 functions).
-- Designed to migrate cleanly to Postgres via pgloader.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- schemes — master list of recommended schemes (populated from CSV)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schemes (
    scheme_id        INTEGER PRIMARY KEY,
    scheme_uid       TEXT NOT NULL UNIQUE,    -- stable surrogate slug; never changes
    scheme_name      TEXT NOT NULL UNIQUE,
    amc              TEXT,
    category         TEXT,
    sub_category     TEXT,                    -- e.g. "Equity: Multi Cap" from PDF header
    amfi_code        TEXT,                    -- enrich later from amfiindia.com
    isin_regular     TEXT,
    isin_direct      TEXT,
    source_url       TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- ----------------------------------------------------------------------------
-- scheme_aliases — historical names / renames / AMC mergers
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheme_aliases (
    alias_id    INTEGER PRIMARY KEY,
    scheme_id   INTEGER NOT NULL REFERENCES schemes(scheme_id),
    alias       TEXT NOT NULL,
    valid_from  DATE,
    valid_to    DATE
);


-- ----------------------------------------------------------------------------
-- fund_snapshots — one row per scheme per report_month per revision
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fund_snapshots (
    snapshot_id      INTEGER PRIMARY KEY,
    scheme_id        INTEGER NOT NULL REFERENCES schemes(scheme_id),
    as_of_date       DATE NOT NULL,           -- "As On" date from PDF header
    report_month     TEXT NOT NULL,           -- "2026-05" for filtering

    -- Revisioning (handles mid-month republished PDFs)
    revision         INTEGER NOT NULL DEFAULT 1,
    superseded_at    TIMESTAMP NULL,

    -- Parser provenance
    parser_version    TEXT,
    parse_errors_json TEXT,
    pdf_sha256        TEXT,

    -- Header block
    benchmark            TEXT,
    inception_date       DATE,
    min_investment       TEXT,
    expense_ratio        REAL,
    exit_load            TEXT,
    fund_aum_cr          REAL,
    fund_age             TEXT,
    overview             TEXT,

    -- Trailing returns (% — 1M/3M/6M absolute, 1Y+ are CAGR)
    return_1m REAL, return_3m REAL, return_6m REAL,
    return_1y REAL, return_2y REAL, return_3y REAL,
    return_5y REAL, return_10y REAL, return_since_inception REAL,

    -- Benchmark returns (parallel set with _bm suffix)
    return_1m_bm REAL, return_3m_bm REAL, return_6m_bm REAL,
    return_1y_bm REAL, return_2y_bm REAL, return_3y_bm REAL,
    return_5y_bm REAL, return_10y_bm REAL, return_since_inception_bm REAL,

    -- Risk metrics 1Y
    std_dev_1y REAL, sharpe_1y REAL, beta_1y REAL, r_square_1y REAL,
    treynor_1y REAL, info_ratio_1y REAL, up_capture_1y REAL,
    down_capture_1y REAL, tracking_error_1y REAL, sortino_1y REAL,

    -- Risk metrics 3Y (same set, _3y suffix)
    std_dev_3y REAL, sharpe_3y REAL, beta_3y REAL, r_square_3y REAL,
    treynor_3y REAL, info_ratio_3y REAL, up_capture_3y REAL,
    down_capture_3y REAL, tracking_error_3y REAL, sortino_3y REAL,

    -- Portfolio characteristics
    total_securities     INTEGER,
    avg_mkt_cap_cr       REAL,
    median_mkt_cap_cr    REAL,
    portfolio_pe         REAL,
    portfolio_pb         REAL,
    portfolio_div_yield  REAL,
    modified_duration    REAL,
    -- Debt-specific (NULL for pure equity funds)
    avg_maturity_years   REAL,
    yield_to_maturity    REAL,

    -- Drawdown
    drawdown_pct           REAL,
    drawdown_duration_days INTEGER,
    drawdown_peak_date     DATE,
    drawdown_valley_date   DATE,
    drawdown_recovery_date DATE,

    -- Mkt cap composition
    large_cap_pct REAL,
    mid_cap_pct   REAL,
    small_cap_pct REAL,

    -- Flexible JSON blobs for fund-type-specific data that doesn't normalize cleanly
    composition_json      TEXT,   -- {"Equity":95.93,"Cash":4.07,...}
    risk_rating_json      TEXT,   -- {"Equity":95.91,"A1+":17.29,...}
    investment_style_json TEXT,   -- {"Large Cap_Blend":4.52,...}
    fund_managers_json    TEXT,   -- [{"name":...,"role":...,"qual":...,"exp":...}]

    source_pdf_path TEXT,
    ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(scheme_id, report_month, revision)
);
CREATE INDEX IF NOT EXISTS idx_snap_month  ON fund_snapshots(report_month);
CREATE INDEX IF NOT EXISTS idx_snap_scheme ON fund_snapshots(scheme_id);


-- ----------------------------------------------------------------------------
-- sector_weights — normalized sector exposure (replaces sector_weights_json)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sector_weights (
    snapshot_id   INTEGER NOT NULL REFERENCES fund_snapshots(snapshot_id),
    sector        TEXT NOT NULL,
    weight_pct    REAL,
    PRIMARY KEY (snapshot_id, sector)
);


-- ----------------------------------------------------------------------------
-- periodic_returns — normalized monthly / FY / CY returns
--   period_type ∈ ('monthly','fy','cy')
--   period_label e.g. '2025-05', 'FY 24', 'CY 23'
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS periodic_returns (
    snapshot_id   INTEGER NOT NULL REFERENCES fund_snapshots(snapshot_id),
    period_type   TEXT NOT NULL,
    period_label  TEXT NOT NULL,
    return_pct    REAL,
    PRIMARY KEY (snapshot_id, period_type, period_label)
);


-- ----------------------------------------------------------------------------
-- holdings — full per-security holdings (high cardinality)
--   ~50–200 holdings × 90 schemes × 12 months ≈ 100k+ rows/year
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holdings (
    holding_id       INTEGER PRIMARY KEY,
    scheme_id        INTEGER NOT NULL REFERENCES schemes(scheme_id),
    report_month     TEXT NOT NULL,
    security_name    TEXT NOT NULL,
    weight_pct       REAL,
    sector           TEXT,
    market_cap       TEXT,        -- "Large Cap"/"Mid Cap"/"Small Cap"/NULL
    instrument_type  TEXT,        -- "Equity"/"Debt"/"Mutual Fund"/"Commodity"/"Derivatives"/"Invit/Reit"
    risk_rating      TEXT,        -- "EQUITY"/"A1+"/"AAA"/"Sovereign"/NULL
    investment_style TEXT,        -- "Growth"/"Value"/"Blend"/NULL
    held_since       DATE
);
CREATE INDEX IF NOT EXISTS idx_hold_scheme_month   ON holdings(scheme_id, report_month);
CREATE INDEX IF NOT EXISTS idx_hold_security       ON holdings(security_name);
CREATE INDEX IF NOT EXISTS idx_hold_security_month ON holdings(security_name, report_month);


-- ----------------------------------------------------------------------------
-- query_log — audit trail for every RM query (compliance non-negotiable)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_log (
    query_id         INTEGER PRIMARY KEY,
    user_id          TEXT,
    timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    question         TEXT NOT NULL,
    sql_executed     TEXT,
    sources_cited    TEXT,        -- JSON array of (scheme, month, page)
    final_answer     TEXT,
    user_feedback    TEXT,        -- "thumbs_up"/"thumbs_down"/NULL
    feedback_comment TEXT,

    -- Compliance / observability fields
    tool_calls_json  TEXT,
    model_name       TEXT,
    model_version    TEXT,
    latency_ms       INTEGER,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    refusal_reason   TEXT NULL
);


-- ----------------------------------------------------------------------------
-- schema_version — migration baseline marker
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Seed row inserted by init_db.py (using INSERT ... WHERE NOT EXISTS to stay
-- portable; SQLite-specific INSERT OR IGNORE is avoided in schema.sql so the
-- file remains a clean migration baseline for pgloader → Postgres).
