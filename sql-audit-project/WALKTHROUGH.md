# Walkthrough

Read this while you run the queries. It tells you what each one does, what you should see, and what to say if someone asks about it.

Run them **one at a time**, in order. Don't paste the whole file. The point is to watch each result land.

---

## Setup (fifteen minutes)

1. **neon.tech** → sign up free → create a project. (Supabase works identically.)
2. Open the **SQL Editor** in their web interface.
3. Paste all of `schema.sql` → Run. Creates seven empty tables.
4. Paste all of `seed.sql` → Run. It's big; that's fine. Fills them.
5. Sanity check:

```sql
SELECT COUNT(*) FROM deals;   -- 400
SELECT COUNT(*) FROM leads;   -- 499
```

If those numbers come back, you're loaded. Now go query by query.

---

# checks.sql

## 1. The duplicate check

**Run the naive version first:**

```sql
SELECT email, COUNT(*) AS n
FROM leads
GROUP BY email
HAVING COUNT(*) > 1;
```

**What you'll see:** nothing. Zero rows.

**Sit with that for a second.** You just checked for duplicates and the data came back clean. If you stopped here, you'd move on and report 499 leads.

**Now run the real one:**

```sql
SELECT LOWER(TRIM(email)) AS normalized_email, COUNT(*) AS n
FROM leads
GROUP BY LOWER(TRIM(email))
HAVING COUNT(*) > 1;
```

**What you'll see:** 30 rows. Thirty humans in your database twice.

**What happened:** `rachel@northwind.com` and `Rachel@Northwind.com` are the same person and *different strings*. `GROUP BY email` compares them character by character, sees a difference, and puts them in separate buckets. `LOWER(TRIM(...))` normalizes them first, so they land together.

**The fix — keep one of each:**

```sql
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY LOWER(TRIM(email))
           ORDER BY created_at
         ) AS rn
  FROM leads
)
SELECT COUNT(*) AS true_lead_count FROM ranked WHERE rn = 1;
```

**499 → 469.**

**How `ROW_NUMBER()` works, plainly:** it numbers rows *within a group*. `PARTITION BY` says what the group is. `ORDER BY` says who gets number 1. So the earliest copy of each email gets `rn = 1`, the second copy gets `rn = 2`. Then `WHERE rn = 1` keeps one of each and `WHERE rn > 1` shows you the dupes.

**Same expression, two uses.** That's the whole pattern.

**If asked:** *"The naive check said the data was clean. It wasn't. Same person, different casing, and a `GROUP BY` on the raw string treats them as two people. You have to normalize inside the grouping or you'll report a headcount that's 6% too high."*

---

## 2. The NULL check

```sql
SELECT
  COUNT(*) FILTER (WHERE rep_id IS NULL)     AS deals_with_no_rep,
  COUNT(*) FILTER (WHERE audited_at IS NULL) AS deals_never_audited
FROM deals;
```

**17 deals have no rep. 80 were never audited.**

`COUNT(*) FILTER (WHERE ...)` is just "count the rows matching this condition." Postgres syntax. Handy.

**Now the part that matters — prove it costs you:**

```sql
SELECT
  (SELECT SUM(amount) FROM deals) AS true_total,
  (SELECT SUM(d.amount) FROM deals d JOIN reps r ON d.rep_id = r.id) AS total_by_rep;
```

**$32,376,074 vs $30,871,720.**

**$1.5 million disappeared.** Because those 17 deals have a NULL `rep_id`, they match no rep, and a `JOIN` drops them. Your "revenue by rep" report will never tie out to your revenue report, and everyone will assume one of them is broken.

**If asked:** *"A NULL in a join key doesn't error. The row just silently doesn't appear. If you sum revenue two different ways and get two different numbers, this is usually why."*

---

## 3. INNER vs LEFT — the most important query on the page

```sql
SELECT 'INNER JOIN' AS join_type, COUNT(DISTINCT d.id) AS deals_visible
FROM deals d INNER JOIN deal_stage_history h ON d.id = h.deal_id
UNION ALL
SELECT 'LEFT JOIN', COUNT(DISTINCT d.id)
FROM deals d LEFT JOIN deal_stage_history h ON d.id = h.deal_id;
```

**INNER: 378. LEFT: 400.**

**Twenty-two deals vanished.** Same table, same join key, one keyword different.

**Why:** `INNER JOIN` only keeps rows that have a match on *both* sides. Twenty-two deals have no rows in the stage-history table, so they have no match, so `INNER JOIN` throws them away. `LEFT JOIN` keeps everything from the left table and fills the missing side with NULLs.

**No error. No warning. Just a number that's 5.5% too low.**

**This is the single reason to be able to read SQL when AI can write it.** An AI-generated query with the wrong join returns a plausible number, and you'd never know.

**If asked:** *"An INNER JOIN silently drops rows with no match. If a query returns a number that feels low and you can't explain why, check the join type first."*

---

## 4. The coverage gap

```sql
SELECT
  COUNT(*)                                   AS total_open_deals,
  COUNT(*) FILTER (WHERE a.deal_id IS NULL)  AS never_audited,
  ROUND(100.0 * COUNT(*) FILTER (WHERE a.deal_id IS NULL) / COUNT(*), 1) AS pct_uncovered
FROM deals d
LEFT JOIN audit_runs a ON d.id = a.deal_id
WHERE d.close_date IS NULL;
```

**158 open deals. 34 never audited. 21.5% uncovered.**

**Read the shape of this query.** `LEFT JOIN` to the audit table, then `WHERE a.deal_id IS NULL` — meaning *keep only the rows that found no match.* That's the standard "find the things that aren't there" pattern.

**Why it matters more than any error log:** if the AI tool has a 100% success rate but only ever ran on 78% of deals, it isn't working — it's *unused*. Nothing in the logs will tell you that, because the runs that never happened don't log anything. Only this query finds them.

**Then find the expensive ones:**

```sql
SELECT d.id, d.name, d.amount, d.stage
FROM deals d
LEFT JOIN audit_runs a ON d.id = a.deal_id
WHERE a.deal_id IS NULL AND d.close_date IS NULL
ORDER BY d.amount DESC LIMIT 15;
```

If a six-figure open deal is on this list, **that's the finding.** That's the sentence you say in the meeting.

**If asked:** *"A tool with a perfect success rate and low usage is a tool nobody adopted. The failures you can see aren't the problem. The runs that never happened are."*

---

## 5. Is the model bullshitting?

```sql
SELECT deal_id, company, risk_score, red_flag_count
FROM audit_runs
WHERE status = 'success' AND risk_score >= 7 AND red_flag_count = 0;
```

**10 rows.**

Every one is a deal the model scored **7 or higher out of 10** for risk — and then listed **zero reasons**.

It asserted danger and produced no evidence. That's a hallucination with a number attached.

**You cannot catch this by looking at either column alone.** A risk score of 9 looks fine. Zero red flags looks fine. It's only the *combination* that's broken.

**Confirm they normally agree:**

```sql
SELECT risk_score, COUNT(*) AS runs, ROUND(AVG(red_flag_count), 2) AS avg_flags
FROM audit_runs WHERE status = 'success'
GROUP BY risk_score ORDER BY risk_score;
```

`avg_flags` climbs steadily with `risk_score` — 0.8 at risk 1, up to ~5 at risk 10. **That's the expected relationship.** The 10 rows above violate it.

**If asked:** *"Two outputs that should agree, and the interesting cases are where they don't. If the model says high risk and can't name a single reason, that's not an insight, that's the model making something up. You only see it by comparing the columns."*

---

## 7. Did the guardrail hold?

```sql
SELECT idempotency_key, COUNT(*) AS times_run
FROM audit_runs GROUP BY idempotency_key HAVING COUNT(*) > 1;
```

**Empty. That's the correct answer.**

The pipeline builds a key like `audit:{deal_id}:{date}` before it calls the model. If that key already exists, the run is skipped. So a double-click, a webhook retry, or an impatient user can't produce two audit records or two API charges.

**This query is the proof it worked.** An empty result is a passing test.

**If asked:** *"I don't just claim it's idempotent, I have the query that would show me if it wasn't. If this ever returns a row, I need to know today, not in six weeks when someone questions the invoice."*

---

# analysis.sql

## Pipeline velocity

```sql
WITH stage_spans AS (
  SELECT deal_id, stage, changed_at AS entered_at,
         LEAD(changed_at) OVER (PARTITION BY deal_id ORDER BY changed_at) AS exited_at
  FROM deal_stage_history
)
SELECT stage, COUNT(*) AS deals_entered,
       ROUND(AVG(exited_at - entered_at), 1) AS avg_days_in_stage,
       MAX(exited_at - entered_at) AS worst_case_days
FROM stage_spans
WHERE exited_at IS NOT NULL AND stage NOT IN ('Closed Won','Closed Lost')
GROUP BY stage ORDER BY avg_days_in_stage DESC;
```

**Prospecting ~31 days. Discovery ~28. Demo ~27. Negotiation ~23.**

**What `LEAD()` does:** it reaches *forward* one row and grabs a value. Here, it grabs the timestamp of the *next* stage change for that deal. Subtract, and you have how long the deal sat in the current stage.

**`PARTITION BY deal_id` is the critical part.** Without it, `LEAD` would grab the next row in the *whole table* — which is a different deal entirely — and every number would be garbage that looks like data. The partition makes it reset at each deal boundary.

**Why the CRM can't do this:** the CRM shows you the deal's stage *today*. It overwrote yesterday. The history table is the only place the movie exists.

**If asked:** *"CRM reporting shows you a photo. Stage history lets you reconstruct the movie. `LEAD` over a partition is how you measure the gaps."*

---

## Cost drift — and the trap

```sql
SELECT DATE_TRUNC('month', created_at) AS month,
       COUNT(*) AS runs,
       ROUND(AVG(cost_usd)::numeric, 5) AS avg_cost,
       ROUND(AVG(note_count), 2) AS avg_notes,
       ROUND(AVG(cost_usd / note_count)::numeric, 6) AS cost_per_note
FROM audit_runs WHERE status = 'success'
GROUP BY 1 ORDER BY 1;
```

**Watch `avg_cost` climb** across the year. Looks like a problem. Someone's going to ask why the AI is getting more expensive.

**Now look at `cost_per_note`.** It's flat, or falling.

**The model didn't get expensive. The inputs got bigger.** Reps logged more calls as they adopted the tool, so each audit had more text to read.

**Same rising number, two completely different stories.** One is "our vendor raised prices" and one is "the tool got more popular." If you only look at `avg_cost`, you go chase a problem that doesn't exist.

**`avg_notes` is the control variable.** It's in the query for exactly this reason.

**If asked:** *"A number going up isn't a finding. You need the control variable next to it. Cost rose because the input grew, not because the model changed, and those need completely different responses."*

---

## Rep performance — and why the obvious query is unfair

**The naive one:**

```sql
SELECT r.name,
       COUNT(*) FILTER (WHERE d.stage = 'Closed Won') AS won,
       ROUND(100.0 * COUNT(*) FILTER (WHERE d.stage='Closed Won')
             / NULLIF(COUNT(*) FILTER (WHERE d.close_date IS NOT NULL),0), 1) AS win_rate
FROM reps r JOIN deals d ON d.rep_id = r.id
GROUP BY r.name ORDER BY win_rate DESC;
```

Looks fine. Ranks the reps. **Two things wrong with it.**

1. **`JOIN` excludes the 17 unassigned deals.** The totals won't match your revenue report.
2. **It punishes new reps for being new.** A rep who started three months ago hasn't had time to close a long-cycle deal. They'll be bottom of this list and it means nothing.

**The fair version adds tenure:**

```sql
SELECT r.name, r.start_date,
       (CURRENT_DATE - r.start_date) / 30 AS months_tenure,
       COUNT(*) FILTER (WHERE d.close_date IS NOT NULL) AS closed,
       ROUND(100.0 * COUNT(*) FILTER (WHERE d.stage='Closed Won')
             / NULLIF(COUNT(*) FILTER (WHERE d.close_date IS NOT NULL),0), 1) AS win_rate
FROM reps r
LEFT JOIN deals d ON d.rep_id = r.id
GROUP BY r.name, r.start_date
ORDER BY months_tenure;
```

Note the `LEFT JOIN` — so a rep with zero deals still appears instead of vanishing.

**If asked:** *"The obvious query would have you fire your newest rep. Ramp is real. If you don't control for tenure you're measuring how long someone's been there, not how good they are."*

---

## Subsidiary rollup (the recursive CTE)

```sql
WITH RECURSIVE hierarchy AS (
  SELECT id, parent_id, name, id AS root_id
  FROM companies WHERE parent_id IS NULL          -- ANCHOR: the roots

  UNION ALL

  SELECT c.id, c.parent_id, c.name, h.root_id     -- ← inherit the parent's root
  FROM companies c
  JOIN hierarchy h ON c.parent_id = h.id          -- RECURSIVE: reference itself
)
SELECT root.name AS parent_company,
       COUNT(DISTINCT h.id) AS entities_in_group,
       ROUND(SUM(d.amount)::numeric, 2) AS group_revenue
FROM hierarchy h
JOIN companies root ON root.id = h.root_id
LEFT JOIN deals d ON d.company_id = h.id AND d.stage = 'Closed Won'
GROUP BY root.name
HAVING COUNT(DISTINCT h.id) > 1
ORDER BY group_revenue DESC NULLS LAST;
```

**Silverpine Analytics: 3 entities, $659,125.**

Without this, that's three separate rows in your customer list and nobody realizes it's one relationship worth two-thirds of a million dollars.

**How recursion works, plainly:**

- **Anchor member** (before `UNION ALL`): the starting point. Companies with `parent_id IS NULL` — the top-level parents. Runs **once**.
- **Recursive member** (after `UNION ALL`): joins the table back to *the CTE itself*. Each pass finds the children of whatever the last pass found.
- It **stops when a pass returns zero rows** — i.e. when there are no more children.

**The trick is `root_id`.** The anchor sets `root_id = its own id`. Then every child *inherits* `h.root_id` from its parent instead of computing a new one. So a grandchild four levels down still carries the ID of the ultimate parent. Then a plain `GROUP BY root_id` rolls up the whole tree.

**If asked:** *"Marketing targets the subsidiary, sales sells to the division, finance bills the parent. The revenue is scattered across five records and no flat GROUP BY will pull it together. You need to walk the tree."*

---

## Orphan leads

```sql
SELECT
  CASE
    WHEN SPLIT_PART(email,'@',2) IN ('gmail.com','yahoo.com','outlook.com')
      THEN 'personal email — correctly unmatchable'
    WHEN SPLIT_PART(email,'@',2) IN (SELECT domain FROM companies WHERE domain IS NOT NULL)
      THEN 'MATCHABLE NOW'
    ELSE 'typo, or the company has a NULL domain'
  END AS reason,
  COUNT(*) AS leads
FROM leads WHERE company_id IS NULL
GROUP BY 1 ORDER BY leads DESC;
```

**217 matchable. 123 personal email. 66 unmatchable.**

**`SPLIT_PART(email, '@', 2)`** cuts the string at the `@` and takes the second piece — the domain.

**The 217 are money on the floor.** Those leads could be attached to a company account *today*, for free, just by matching the email domain. Nobody's done it.

**The 66 are the honest part.** Some are typos. But some are leads at companies that **are in your database** — the join fails anyway, because that company's `domain` field is NULL.

**You'd swear the company was missing. It isn't. The field is.**

**If asked:** *"Roughly 40% of unmatched leads could be auto-assigned by domain right now. And the ones that can't split into 'personal email, which is correct' and 'our own data is incomplete,' which is a fixable problem, not a lost lead."*

---

# The last query. The one that matters.

```sql
SELECT
  CASE
    WHEN risk_score <= 3 THEN '1. Low risk (1-3)'
    WHEN risk_score <= 6 THEN '2. Medium risk (4-6)'
    ELSE                      '3. High risk (7-10)'
  END AS risk_band,
  COUNT(*) AS closed_deals,
  COUNT(*) FILTER (WHERE stage = 'Closed Won') AS won,
  ROUND(100.0 * COUNT(*) FILTER (WHERE stage='Closed Won') / COUNT(*), 1) AS win_rate_pct
FROM deals
WHERE close_date IS NOT NULL AND risk_score IS NOT NULL
GROUP BY risk_band ORDER BY risk_band;
```

| Risk band | Closed | Won | Win rate |
|---|---|---|---|
| Low (1–3) | 47 | 34 | **72.3%** |
| Medium (4–6) | 65 | 32 | **49.2%** |
| High (7–10) | 78 | 26 | **33.3%** |

**The score predicts.** Win rate falls as risk rises, cleanly, across 190 closed deals.

**But high-risk deals still win a third of the time.** It's a signal, not a verdict. Anyone who treats a score of 8 as a dead deal is throwing away one in three.

### But what if it HAD come back flat?

This is the part most people get wrong, and getting it right is the whole interview.

**A flat result does not mean "kill the feature."** It means the score has no signal *as banded*. It does not tell you **why**. Four explanations, four completely different actions:

**1. The bands are wrong.** Maybe risk isn't linear. Maybe only 9s and 10s mean anything and 1–8 is noise. Re-cut the bands and look again.
→ **Calibration fix. Don't touch the model.**

**2. The model is hedging.** If it scores almost everything 5–6, the outcome curve will look flat even if the model understands the deals perfectly well. It just refuses to commit.
→ **Prompt fix.** And you'd catch it by checking the *distribution* first:

```sql
SELECT risk_score, COUNT(*) AS runs,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM audit_runs WHERE status = 'success'
GROUP BY risk_score ORDER BY risk_score;
```

A healthy model **spreads** across the range. A hedging model piles up in the middle and tells you nothing. **Look at this before you look at outcomes.**

**3. The reps acted on it.** The tool flags a deal, the rep sees it, the rep *saves the deal*. It closes won. Do that consistently and the correlation vanishes — **because the tool worked.**

**The tool working looks identical to the tool failing.** The SQL cannot tell them apart. Only adoption data can.

**4. The model genuinely doesn't know.** → **Now** you kill it.

### The sentence to say

*"A flat result opens an investigation, it doesn't close one. Before I'd recommend killing it I'd check the score distribution — if everything's clustered at 5-6, that's a hedging model and a prompt problem, not a useless one. And a tool that everyone acts on would show no correlation anyway, because every flagged deal got rescued. Those look identical in the data."*

**That's the whole point. The SQL is the easy half.**

---

## The four sentences that carry the whole project

If you remember nothing else:

1. **"The `INNER JOIN` returned 378 and the `LEFT JOIN` returned 400. Nobody got an error."**

2. **"A tool with a 100% success rate and low adoption is a tool nobody uses. No error log will tell you that — only a `LEFT JOIN` will."**

3. **"Cost went up, but cost-per-note was flat. The model didn't get expensive, the inputs got bigger. Same number, two completely different stories."**

4. **"Every AI feature I build ships with a query that could prove it useless."**
