-- ===========================================================================
--  00_init_schemas.sql
--  Medallion layering: bronze (raw) -> silver (cleaned) -> gold (business).
--  Idempotent: safe to run on every `make bronze`.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

COMMENT ON SCHEMA bronze IS
  'Bronze layer: raw CSV as loaded, append-only, every column TEXT.';
COMMENT ON SCHEMA silver IS
  'Silver layer: cleaned, typed, deduplicated, conformed column names.';
COMMENT ON SCHEMA gold IS
  'Gold layer: Kimball star schema (dim_*/fact_*), data marts (mart_*) and materialized views (mv_*).';

-- ---------------------------------------------------------------------------
-- Data-quality results
--
-- Lives in `gold` (not `bronze`) because DQ outcomes are a business-facing
-- product of the pipeline, and the acceptance criteria reference
-- gold.dq_results. One row per rule per run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.dq_results (
    rule_id      TEXT        NOT NULL,
    layer        TEXT        NOT NULL,
    table_name   TEXT        NOT NULL,
    column_name  TEXT,
    failed_rows  BIGINT      NOT NULL,
    status       TEXT        NOT NULL CHECK (status IN ('PASS', 'FAIL', 'WARN')),
    run_ts       TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dq_results_run_ts ON gold.dq_results (run_ts DESC);
CREATE INDEX IF NOT EXISTS ix_dq_results_status ON gold.dq_results (status);
