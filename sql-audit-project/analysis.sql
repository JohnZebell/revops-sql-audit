-- ===========================================================
-- analysis.sql  —  THE QUESTIONS THAT DECIDE THINGS
--
-- Run checks.sql FIRST. Do not analyze data you have not audited.
--
-- The last query on this page is the one that matters. It is the
-- query that could get the whole AI feature killed. That is the point
-- of shipping it.
-- ===========================================================


-- ─────────────────────────────────────────────────────────────
-- PIPELINE VELOCITY  —  where do deals actually stall?
--
-- The CRM shows you today and overwrites yesterday. It cannot tell
-- you that a deal sat in Discovery for 97 days. The history table can.
--
-- LEAD() grabs the timestamp of the NEXT stage change. The gap between
-- them is the dwell time.
-- ─────────────────────────────────────────────────────────────

WITH stage_spans AS (
  SELECT
    deal_id,
    stage,
    changed_at AS entered_at,
    LEAD(changed_at) OVER (PARTITION BY deal_id ORDER BY changed_at) AS exited_at
  FROM deal_stage_history
)
SELECT
  stage,
  COUNT(*)                                                   AS deals_entered,
  ROUND(AVG(exited_at - entered_at), 1)                      AS avg_days_in_stage,
  MAX(exited_at - entered_at)                                AS worst_case_days
FROM stage_spans
WHERE exited_at IS NOT NULL          -- still-open deals have no exit yet
  AND stage NOT IN ('Closed Won', 'Closed Lost')
GROUP BY stage
ORDER BY avg_days_in_stage DESC;
--> The stage at the top of this list is where deals go to die.
--  That is a coaching conversation, not a data point.


-- Which specific open deals are stalled RIGHT NOW?
WITH latest AS (
  SELECT deal_id, stage, changed_at,
         ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY changed_at DESC) AS rn
  FROM deal_stage_history
)
SELECT d.id, d.name, d.amount, l.stage,
       CURRENT_DATE - l.changed_at AS days_stuck,
       d.risk_score
FROM latest l
JOIN deals d ON d.id = l.deal_id
WHERE l.rn = 1
  AND d.close_date IS NULL
  AND CURRENT_DATE - l.changed_at > 45
ORDER BY d.amount DESC
LIMIT 20;
--> Open, expensive, and hasn't moved in 45+ days. Call these today.


-- ─────────────────────────────────────────────────────────────
-- COST DRIFT  —  is the pipeline getting more expensive?
--
-- THE TRAP: cost per run WILL rise over time. It looks like the model
-- got pricier. It didn't.
--
-- note_count is the CONTROL VARIABLE. Look at both columns or you
-- will chase a problem that does not exist.
-- ─────────────────────────────────────────────────────────────

SELECT
  DATE_TRUNC('month', created_at)    AS month,
  COUNT(*)                            AS runs,
  ROUND(AVG(cost_usd)::numeric, 5)    AS avg_cost,
  ROUND(AVG(note_count), 2)           AS avg_notes,      -- ← the control
  ROUND(AVG(cost_usd / note_count)::numeric, 6) AS cost_per_note,  -- ← the honest metric
  ROUND(AVG(latency_ms))              AS avg_latency_ms
FROM audit_runs
WHERE status = 'success'
GROUP BY 1
ORDER BY 1;
--> If avg_cost climbs but cost_per_note is FLAT, nothing is wrong.
--  The inputs just got bigger. Two very different stories, one rising number.


-- ─────────────────────────────────────────────────────────────
-- MODEL DRIFT  —  did the model's judgment change?
--
-- Same trap, different column. If average risk score climbs while
-- note_count also climbs, the DATA changed and the model is fine.
-- If risk climbs and notes stay FLAT, the model moved and nobody
-- touched it. That is the one to worry about.
-- ─────────────────────────────────────────────────────────────

SELECT
  DATE_TRUNC('month', created_at) AS month,
  prompt_version,
  COUNT(*)                        AS runs,
  ROUND(AVG(risk_score), 2)       AS avg_risk,
  ROUND(AVG(note_count), 2)       AS avg_notes,     -- ← control variable again
  ROUND(AVG(red_flag_count), 2)   AS avg_flags,
  COUNT(*) FILTER (WHERE status = 'schema_violation') AS failures
FROM audit_runs
GROUP BY 1, 2
ORDER BY 1;
--> Also: this is how you compare prompt v1 to v2. If you can't answer
--  "what changed when I edited the prompt", you are editing it blind.


-- ─────────────────────────────────────────────────────────────
-- REP PERFORMANCE  —  and why the naive version is unfair
-- ─────────────────────────────────────────────────────────────

-- The naive version. Ranks reps. Looks reasonable. IS UNFAIR.
SELECT r.name,
       COUNT(*) FILTER (WHERE d.stage = 'Closed Won')  AS won,
       COUNT(*) FILTER (WHERE d.close_date IS NOT NULL) AS closed,
       ROUND(100.0 * COUNT(*) FILTER (WHERE d.stage = 'Closed Won')
             / NULLIF(COUNT(*) FILTER (WHERE d.close_date IS NOT NULL), 0), 1) AS win_rate
FROM reps r
JOIN deals d ON d.rep_id = r.id
GROUP BY r.name
ORDER BY win_rate DESC;
--> Two problems. (1) It silently excludes 17 deals with no rep assigned.
--  (2) It punishes NEW reps who haven't had time to close anything.


-- The fair version: control for tenure.
SELECT r.name,
       r.start_date,
       (CURRENT_DATE - r.start_date) / 30           AS months_tenure,
       COUNT(*) FILTER (WHERE d.close_date IS NOT NULL) AS closed,
       ROUND(100.0 * COUNT(*) FILTER (WHERE d.stage = 'Closed Won')
             / NULLIF(COUNT(*) FILTER (WHERE d.close_date IS NOT NULL), 0), 1) AS win_rate
FROM reps r
LEFT JOIN deals d ON d.rep_id = r.id     -- LEFT: show reps with zero deals too
GROUP BY r.name, r.start_date
ORDER BY months_tenure;
--> A rep with 3 months tenure and a low win rate is RAMPING, not failing.
--  Fire them off the first query and you fire the wrong person.


-- ─────────────────────────────────────────────────────────────
-- SUBSIDIARY ROLLUP  —  the revenue you can't see
--
-- "How much do we get from Cascade Health AND all its subsidiaries?"
--
-- A flat GROUP BY company scatters this across multiple rows and
-- nobody ever adds it up. Needs recursion.
-- ─────────────────────────────────────────────────────────────

WITH RECURSIVE hierarchy AS (
  -- ANCHOR: the roots. A root's root is itself.
  SELECT id, parent_id, name, id AS root_id
  FROM companies
  WHERE parent_id IS NULL

  UNION ALL

  -- RECURSIVE: each child INHERITS its parent's root_id.
  -- That inherited root_id is the entire trick.
  SELECT c.id, c.parent_id, c.name, h.root_id
  FROM companies c
  JOIN hierarchy h ON c.parent_id = h.id
)
SELECT
  root.name                                AS parent_company,
  COUNT(DISTINCT h.id)                     AS entities_in_group,
  COUNT(d.id)                              AS total_deals,
  ROUND(SUM(d.amount)::numeric, 2)         AS group_revenue
FROM hierarchy h
JOIN companies root ON root.id = h.root_id
LEFT JOIN deals d   ON d.company_id = h.id AND d.stage = 'Closed Won'
GROUP BY root.name
HAVING COUNT(DISTINCT h.id) > 1            -- only groups with subsidiaries
ORDER BY group_revenue DESC NULLS LAST;
--> This revenue was always there. It was just spread across rows
--  nobody thought to add together.


-- ─────────────────────────────────────────────────────────────
-- LEAD-TO-ACCOUNT MATCHING  —  the orphans
-- ─────────────────────────────────────────────────────────────

SELECT
  COUNT(*)                                                       AS unmatched_leads,
  COUNT(*) FILTER (WHERE SPLIT_PART(email,'@',2)
                   IN (SELECT domain FROM companies WHERE domain IS NOT NULL))
                                                                 AS matchable_now,
  COUNT(*) FILTER (WHERE SPLIT_PART(email,'@',2)
                   IN ('gmail.com','yahoo.com','outlook.com'))   AS personal_email
FROM leads
WHERE company_id IS NULL;
--> `matchable_now` are leads sales could claim TODAY, for free,
--  just by matching the email domain. That is money on the floor.


-- Actually do the match
SELECT l.id, l.email, SPLIT_PART(l.email,'@',2) AS domain, c.name AS matched_company
FROM leads l
JOIN companies c ON SPLIT_PART(l.email,'@',2) = c.domain
WHERE l.company_id IS NULL
ORDER BY c.name
LIMIT 20;


-- THE ONES THAT WILL NEVER MATCH, and why. This is the honest part.
SELECT
  CASE
    WHEN SPLIT_PART(email,'@',2) IN ('gmail.com','yahoo.com','outlook.com')
      THEN 'personal email — correctly unmatchable'
    WHEN SPLIT_PART(email,'@',2) IN (SELECT domain FROM companies WHERE domain IS NOT NULL)
      THEN 'matchable'
    ELSE 'typo, or the company has a NULL domain'   -- ← the sneaky bucket
  END AS reason,
  COUNT(*) AS leads
FROM leads
WHERE company_id IS NULL
GROUP BY 1
ORDER BY leads DESC;
--> That last bucket contains companies that ARE in the database.
--  The join fails anyway, because their `domain` field is NULL.
--  You would swear the company is missing. It isn't.


-- ─────────────────────────────────────────────────────────────
-- FIRST-TOUCH / LAST-TOUCH ATTRIBUTION
--
-- ROW_NUMBER() twice — once ASC, once DESC. Rank 1 ascending is the
-- first touch. Rank 1 descending is the last. That's the whole trick.
-- ─────────────────────────────────────────────────────────────

WITH ranked AS (
  SELECT
    deal_id, campaign, channel, touched_at,
    ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY touched_at ASC)  AS first_rank,
    ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY touched_at DESC) AS last_rank
  FROM marketing_touches
  WHERE deal_id IS NOT NULL
)
SELECT
  MAX(CASE WHEN first_rank = 1 THEN channel END) AS first_touch_channel,
  COUNT(DISTINCT deal_id)                        AS deals,
  ROUND(SUM(d.amount)::numeric, 0)               AS pipeline_value
FROM ranked r
JOIN deals d ON d.id = r.deal_id
GROUP BY r.deal_id, d.amount
LIMIT 0;   -- (structure shown; the aggregate version is below)


-- Credit by first-touch channel
WITH ranked AS (
  SELECT deal_id, channel,
         ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY touched_at ASC) AS rn
  FROM marketing_touches
  WHERE deal_id IS NOT NULL
)
SELECT r.channel                        AS first_touch_channel,
       COUNT(*)                         AS deals_sourced,
       COUNT(*) FILTER (WHERE d.stage = 'Closed Won') AS won,
       ROUND(SUM(d.amount) FILTER (WHERE d.stage = 'Closed Won')::numeric, 0) AS won_revenue
FROM ranked r
JOIN deals d ON d.id = r.deal_id
WHERE r.rn = 1
GROUP BY r.channel
ORDER BY won_revenue DESC NULLS LAST;
--> Change ASC to DESC above and this becomes last-touch attribution.
--  The two will disagree. That disagreement is the whole argument
--  marketing and sales have every quarter.


-- ═════════════════════════════════════════════════════════════
--
--  THE QUERY THAT COULD KILL THE FEATURE
--
--  Did a high risk score actually predict a loss?
--
--  If deals scored 8-10 close at the same rate as deals scored 1-3,
--  the audit may be decoration. But read the four explanations below
--  before you conclude that -- a flat result has more than one cause.
--
--  Every AI feature I build ships with a query that could prove it
--  useless. If you can't write this one, you don't have a feature.
--  You have a demo.
--
-- ═════════════════════════════════════════════════════════════

SELECT
  CASE
    WHEN risk_score <= 3 THEN '1. Low risk (1-3)'
    WHEN risk_score <= 6 THEN '2. Medium risk (4-6)'
    ELSE                      '3. High risk (7-10)'
  END                                                          AS risk_band,
  COUNT(*)                                                     AS closed_deals,
  COUNT(*) FILTER (WHERE stage = 'Closed Won')                 AS won,
  COUNT(*) FILTER (WHERE stage = 'Closed Lost')                AS lost,
  ROUND(100.0 * COUNT(*) FILTER (WHERE stage = 'Closed Won')
        / COUNT(*), 1)                                         AS win_rate_pct,
  ROUND(AVG(amount)::numeric, 0)                               AS avg_deal_size
FROM deals
WHERE close_date IS NOT NULL      -- only deals with a KNOWN outcome
  AND risk_score IS NOT NULL      -- only deals the tool actually looked at
GROUP BY risk_band
ORDER BY risk_band;

--> READ THIS CAREFULLY.
--
--  If win_rate_pct falls as risk_band rises: the model has signal.
--
--  If it's FLAT across all three bands, DO NOT immediately kill the
--  feature. A flat result tells you the score has no signal AS BANDED.
--  It does not tell you WHY. There are four different explanations and
--  they call for four completely different actions:
--
--   1. THE BANDS ARE WRONG.
--      Maybe risk isn't linear. Maybe only 9s and 10s mean anything and
--      1-8 is noise. Re-cut the bands before you touch anything else.
--      -> This is a CALIBRATION fix.
--
--   2. THE MODEL IS HEDGING.
--      If it scores almost everything 5-6, the outcomes will look flat
--      even if the model understands the deals fine. It just refuses to
--      commit. Check the DISTRIBUTION (query below) before the outcomes.
--      -> This is a PROMPT fix.
--
--   3. THE REPS ACTED ON IT.
--      A perfect early-warning system that everyone uses would show NO
--      correlation at all -- because every flagged deal got RESCUED.
--      The tool working looks identical to the tool failing.
--      -> This is a SUCCESS, and you need adoption data to see it.
--
--   4. THE MODEL GENUINELY DOESN'T KNOW.
--      -> NOW you kill it.
--
--  Nobody kills a feature off one query. This query opens the
--  investigation; it does not close it.
--
--  The SQL is the easy half.


-- ─────────────────────────────────────────────────────────────
-- BEFORE you conclude anything from the query above:
-- LOOK AT THE DISTRIBUTION.
--
-- If every score is squeezed into 5-6, the model is HEDGING, not wrong.
-- A model that refuses to commit will produce a flat outcome curve even
-- when it understands the deals perfectly well.
--
-- Calibration problem, not a capability problem. Completely different fix.
-- ─────────────────────────────────────────────────────────────

SELECT risk_score,
       COUNT(*)                                              AS runs,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)    AS pct_of_all_runs
FROM audit_runs
WHERE status = 'success'
GROUP BY risk_score
ORDER BY risk_score;
--> A healthy model SPREADS across the range.
--  A hedging model piles up in the middle and tells you nothing.


-- The same question, held to a higher standard: does it beat deal size?
-- Maybe the score is just a proxy for "big deals are harder."
SELECT
  CASE WHEN risk_score <= 3 THEN 'Low' WHEN risk_score <= 6 THEN 'Med' ELSE 'High' END AS risk,
  CASE WHEN amount < 40000 THEN 'small' WHEN amount < 100000 THEN 'mid' ELSE 'large' END AS size,
  COUNT(*) AS deals,
  ROUND(100.0 * COUNT(*) FILTER (WHERE stage='Closed Won') / COUNT(*), 1) AS win_rate
FROM deals
WHERE close_date IS NOT NULL AND risk_score IS NOT NULL
GROUP BY 1, 2
ORDER BY 2, 1;
--> If risk still predicts within each size band, the signal is real.
--  If it disappears once you control for deal size, the model learned
--  "big deals are risky" and dressed it up as insight.
