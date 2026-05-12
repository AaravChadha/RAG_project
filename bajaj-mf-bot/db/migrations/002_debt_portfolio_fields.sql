-- Migration 002: debt-side Portfolio Characteristics fields
--
-- The Finalyca template's Portfolio Characteristics block carries different
-- labels for debt funds vs equity funds. Equity shows P/E, P/B, Dividend
-- Yield, Avg/Median Mkt Cap. Debt shows Average Maturity Years, Macaulay
-- Duration Years (unit varies — skipped from schema), Modified Duration
-- (already captured), and Yield To Maturity. This migration adds the two
-- debt-only fields so debt funds can populate them while equity funds
-- leave them NULL.

ALTER TABLE fund_snapshots ADD COLUMN avg_maturity_years REAL;
ALTER TABLE fund_snapshots ADD COLUMN yield_to_maturity  REAL;

INSERT INTO schema_version (version)
SELECT 2 WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE version = 2);
