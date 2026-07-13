-- ===========================================================
-- checks.sql  —  DATA QUALITY
--
-- Seven checks. Every one of them catches a failure that produces
-- a PLAUSIBLE NUMBER AND NO ERROR MESSAGE.
--
-- That is the whole point. A query that crashes is a good day.
-- A query that returns a confidently wrong number and nobody
-- notices is how a business makes a bad decision.
--
-- Run these BEFORE you trust anything in analysis.sql.
-- ===========================================================


-- ─────────────────────────────────────────────────────────────
-- 1. DUPLICATES
--
-- Business failure: duplicates inflate customer counts, double-count
-- revenue, and send the same person two emails. A repeated file load
-- or a bad merge is all it takes.
-- ─────────────────────────────────────────────────────────────

-- The naive check. Looks clean. IT IS LYING TO YOU.
SELECT email, COUNT(*) AS n
FROM leads
GROUP BY email
HAVING COUNT(*) > 1;
--> returns almost nothing, because 'rachel@northwind.com' and
--  'Rachel@Northwind.com' are NOT an exact match.


-- The check that actually works. LOWER + TRIM inside the grouping.
SELECT LOWER(TRIM(email)) AS normalized_email,
       COUNT(*)           AS n
FROM leads
GROUP BY LOWER(TRIM(email))
HAVING COUNT(*) > 1
ORDER BY n DESC;
--> 30 duplicate humans. Every one of them was being counted twice.


-- Now go fix it: ROW_NUMBER keeps one of each.
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY LOWER(TRIM(email))   -- normalize INSIDE the partition
           ORDER BY created_at               -- keep the earliest
         ) AS rn
  FROM leads
)
SELECT COUNT(*) AS true_lead_count
FROM ranked
WHERE rn = 1;
--> The real number. Compare it to SELECT COUNT(*) FROM leads.


-- ─────────────────────────────────────────────────────────────
-- 2. NULLS IN CRITICAL COLUMNS
--
-- Business failure: a NULL in a join key means that row silently
-- vanishes from every report that joins on it. Not an error. Just gone.
-- ─────────────────────────────────────────────────────────────

SELECT
  COUNT(*) FILTER (WHERE rep_id     IS NULL) AS deals_with_no_rep,
  COUNT(*) FILTER (WHERE company_id IS NULL) AS deals_with_no_company,
  COUNT(*) FILTER (WHERE amount     IS NULL) AS deals_with_no_amount,
  COUNT(*) FILTER (WHERE audited_at IS NULL) AS deals_never_audited
FROM deals;


-- Prove it matters. These two numbers should be the same. They are not.
SELECT
  (SELECT SUM(amount) FROM deals)                          AS true_total,
  (SELECT SUM(d.amount) FROM deals d JOIN reps r ON d.rep_id = r.id) AS total_by_rep;
--> "Revenue by rep" quietly excludes every deal with no rep assigned.
--  Your rep report will not tie out to your revenue report, and
--  nobody will know why.


-- ─────────────────────────────────────────────────────────────
-- 3. ORPHAN ROWS  (referential integrity)
--
-- Business failure: the transaction exists but the context doesn't.
-- The sale happened; the customer record it points at is missing.
-- ─────────────────────────────────────────────────────────────

-- Deals with NO stage history. They exist. They just aren't in the
-- other table.
SELECT COUNT(*) AS deals_with_no_history
FROM deals d
LEFT JOIN deal_stage_history h ON d.id = h.deal_id
WHERE h.deal_id IS NULL;


-- Why you care. Run BOTH. They return different numbers.
SELECT 'INNER JOIN' AS join_type, COUNT(DISTINCT d.id) AS deals_visible
FROM deals d INNER JOIN deal_stage_history h ON d.id = h.deal_id
UNION ALL
SELECT 'LEFT JOIN',  COUNT(DISTINCT d.id)
FROM deals d LEFT  JOIN deal_stage_history h ON d.id = h.deal_id;
--> The INNER JOIN silently drops every deal with no history.
--  NOBODY GETS AN ERROR. The number is just wrong.
--  This is the single most important thing on this page.


-- ─────────────────────────────────────────────────────────────
-- 4. THE COVERAGE GAP
--
-- Business failure: THE QUIET ONE. A tool with a 100% success rate
-- and 12 runs a week is a tool NOBODY ADOPTED.
--
-- No error log will ever tell you this. Only a LEFT JOIN will.
-- ─────────────────────────────────────────────────────────────

SELECT
  COUNT(*)                                        AS total_open_deals,
  COUNT(*) FILTER (WHERE a.deal_id IS NULL)       AS never_audited,
  ROUND(100.0 * COUNT(*) FILTER (WHERE a.deal_id IS NULL) / COUNT(*), 1)
                                                  AS pct_uncovered
FROM deals d
LEFT JOIN audit_runs a ON d.id = a.deal_id
WHERE d.close_date IS NULL;      -- open deals only: these are the ones that
                                 -- could still be SAVED by an audit


-- Which open deals are flying blind, biggest first?
SELECT d.id, d.name, d.amount, d.stage, d.created_at
FROM deals d
LEFT JOIN audit_runs a ON d.id = a.deal_id
WHERE a.deal_id IS NULL
  AND d.close_date IS NULL
ORDER BY d.amount DESC
LIMIT 15;
--> Every row here is an open deal the tool never looked at.
--  If a six-figure deal is on this list, that is the finding.


-- ─────────────────────────────────────────────────────────────
-- 5. INTERNAL CONTRADICTION  (is the model bullshitting?)
--
-- Business failure: the model said "high risk" and then could not
-- name a single reason. That is not an insight. That is a hallucination
-- with a number attached.
--
-- You will NEVER catch this by looking at either column alone.
-- ─────────────────────────────────────────────────────────────

SELECT id, deal_id, company, risk_score, red_flag_count, note_count
FROM audit_runs
WHERE status = 'success'
  AND risk_score >= 7
  AND red_flag_count = 0
ORDER BY risk_score DESC;
--> Scored 7-10 out of 10. Zero red flags. The model asserted danger
--  and produced no evidence for it.


-- The inverse is also a bug: lots of flags, called it healthy.
SELECT id, deal_id, company, risk_score, red_flag_count
FROM audit_runs
WHERE status = 'success'
  AND risk_score <= 3
  AND red_flag_count >= 5;


-- Do the two columns agree AT ALL? They should move together.
SELECT risk_score,
       COUNT(*)                    AS runs,
       ROUND(AVG(red_flag_count),2) AS avg_flags
FROM audit_runs
WHERE status = 'success'
GROUP BY risk_score
ORDER BY risk_score;
--> avg_flags should climb with risk_score. Where it doesn't, look closer.


-- ─────────────────────────────────────────────────────────────
-- 6. OUTLIERS
--
-- Outliers are NOT always errors. A $250k deal might be real.
-- But a decimal-point mistake looks exactly the same.
--
-- SQL finds the candidates. BUSINESS CONTEXT decides.
-- ─────────────────────────────────────────────────────────────

-- Business-rule version (start here — it's the honest one)
SELECT id, name, amount, stage
FROM deals
WHERE amount <= 0 OR amount > 300000;


-- Statistical version: more than 3 standard deviations out
SELECT id, name, amount
FROM deals
WHERE amount > (SELECT AVG(amount) + 3 * STDDEV(amount) FROM deals)
ORDER BY amount DESC;


-- Latency outliers: is the pipeline degrading?
SELECT id, deal_id, latency_ms, note_count, cost_usd
FROM audit_runs
WHERE latency_ms > (SELECT AVG(latency_ms) + 2 * STDDEV(latency_ms) FROM audit_runs)
ORDER BY latency_ms DESC;


-- ─────────────────────────────────────────────────────────────
-- 7. DID THE IDEMPOTENCY GUARDRAIL ACTUALLY HOLD?
--
-- Business failure: if this returns ANY rows, a retry or a double-click
-- created a second audit and charged the API twice.
--
-- Should return NOTHING. If it doesn't, I need to know TODAY, not in
-- six weeks when someone questions the invoice.
-- ─────────────────────────────────────────────────────────────

SELECT idempotency_key, COUNT(*) AS times_run
FROM audit_runs
GROUP BY idempotency_key
HAVING COUNT(*) > 1;
--> Empty result = the guardrail held. This is the query that proves it.


-- ─────────────────────────────────────────────────────────────
-- BONUS: FRESHNESS
--
-- A table can pass all seven checks above and STILL be wrong,
-- because it's stale. Clean, valid, and out of date.
-- ─────────────────────────────────────────────────────────────

SELECT
  MAX(created_at) AS last_audit,
  CURRENT_DATE - MAX(created_at) AS days_since_last_run,
  CASE
    WHEN CURRENT_DATE - MAX(created_at) <= 7 THEN 'FRESH'
    ELSE 'STALE — has the pipeline stopped?'
  END AS status
FROM audit_runs;
