-- ===========================================================
-- schema.sql
--
-- A CRM-shaped database, plus the telemetry table an AI pipeline
-- writes to.
--
-- Run this first, then seed.sql.
-- Works on Postgres (Neon, Supabase, local) and DuckDB.
-- ===========================================================

DROP TABLE IF EXISTS marketing_touches CASCADE;
DROP TABLE IF EXISTS leads CASCADE;
DROP TABLE IF EXISTS audit_runs CASCADE;
DROP TABLE IF EXISTS deal_stage_history CASCADE;
DROP TABLE IF EXISTS deals CASCADE;
DROP TABLE IF EXISTS reps CASCADE;
DROP TABLE IF EXISTS companies CASCADE;


-- Companies. Self-referencing: a company can have a parent.
-- Note there is NO NOT NULL on `domain`. That is deliberate --
-- real CRMs are full of records missing the field you need.
CREATE TABLE companies (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(120),
    domain      VARCHAR(120),
    tier        VARCHAR(30),
    parent_id   INTEGER          -- NULL = this company is a root
);


CREATE TABLE reps (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(80),
    region      VARCHAR(40),
    start_date  DATE             -- for ramp analysis
);


CREATE TABLE deals (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER,
    rep_id      INTEGER,         -- nullable ON PURPOSE
    name        VARCHAR(120),
    amount      NUMERIC(12,2),
    stage       VARCHAR(40),
    deal_type   VARCHAR(30),
    created_at  DATE,
    close_date  DATE,            -- NULL = still open
    risk_score  INTEGER,         -- NULL = never audited
    audited_at  DATE,            -- NULL = never audited  <- the coverage gap
    note_count  INTEGER
);


-- The "movie" table. A CRM shows you today and overwrites yesterday.
-- This is how you reconstruct what actually happened.
CREATE TABLE deal_stage_history (
    id          INTEGER PRIMARY KEY,
    deal_id     INTEGER,
    stage       VARCHAR(40),
    changed_at  DATE
);


-- The AI pipeline's own telemetry.
-- EVERY run is logged: successes, schema violations, cost, latency.
-- A system you cannot audit is a system you are trusting on faith.
CREATE TABLE audit_runs (
    id                INTEGER PRIMARY KEY,
    deal_id           INTEGER,
    idempotency_key   VARCHAR(120),   -- a retry cannot create a second row
    company           VARCHAR(120),
    status            VARCHAR(40),    -- 'success' | 'schema_violation'
    validation_error  VARCHAR(200),
    risk_score        INTEGER,        -- NULL when the run failed validation
    red_flag_count    INTEGER,
    note_count        INTEGER,        -- the CONTROL VARIABLE for drift analysis
    model             VARCHAR(60),
    prompt_version    VARCHAR(20),
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cost_usd          NUMERIC(12,6),
    latency_ms        INTEGER,
    created_at        DATE
);


CREATE TABLE leads (
    id          INTEGER PRIMARY KEY,
    email       VARCHAR(140),
    full_name   VARCHAR(80),
    company_id  INTEGER,         -- NULL for orphans (most of them)
    utm_source  VARCHAR(50),
    created_at  DATE
);


CREATE TABLE marketing_touches (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER,
    deal_id     INTEGER,         -- NULL until the lead converts
    campaign    VARCHAR(80),
    channel     VARCHAR(40),
    touched_at  DATE
);
