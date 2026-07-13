# Revenue Operations Health Report
*Generated July 13, 2026 · deal-audit pipeline + CRM data*

---

## Executive summary

- **3 critical** — data is actively wrong or money is at risk.
- **4 warnings** — need a human decision.
- **2 passing** — guardrails held (an empty result is a passing test).

**The three things that need attention, in order:**

1. **Pipeline freshness** — **No audit has run in 14 days.** The pipeline may have stopped.
2. **Duplicate leads** — The lead count is inflated by **30 duplicate records** (6.0% of the list). The real number is **469**, not 499.
3. **Unassigned deals** — **17 deals have no rep assigned**, hiding **$1,504,354** from every rep-level report.

---

## Data quality

_These run before any analysis. Do not trust numbers from data you have not audited._

### Pipeline freshness

🔴 **CRITICAL** — **No audit has run in 14 days.** The pipeline may have stopped.

A table can pass every other check on this page and still be wrong, because it's *stale*. Clean, valid, and out of date.

**Nothing errors when a scheduled job silently stops firing.** The data just quietly gets older while the dashboard keeps rendering it.

**What to do:** Check the schedule trigger and the credentials. Then add an alert on this query.

### Idempotency guardrail

🟢 PASS — **No duplicate audits across 304 runs.** A retry or double-click cannot create a second record or a second API charge.

**An empty result here is a passing test.** The pipeline builds a key (`audit:{deal_id}:{date}`) before calling the model, and skips the run if that key already exists.

This query is the *proof* the guardrail held — not a claim that it did.

**What to do:** Nothing. Keep this in the nightly job.

### Duplicate leads

🔴 **CRITICAL** — The lead count is inflated by **30 duplicate records** (6.0% of the list). The real number is **469**, not 499.

A naive `GROUP BY email` finds **0** duplicates — it looks clean. But `rachel@northwind.com` and `Rachel@Northwind.com` are the same human and different strings, so they land in separate buckets. Normalizing with `LOWER(TRIM(email))` surfaces all **30**.

**Business impact:** every one of these people gets contacted twice. Any per-lead cost metric is overstated. Conversion rate is understated, because the denominator is too big.

| normalized_email | copies |
|---|---|
| bree.blake@outlook.com | 2 |
| bree.reeves@outlook.com | 2 |
| cal.obi@outlook.com | 2 |
| dana.adeyemi@gmail.com | 2 |
| dana@keystone.ai | 2 |
| dana@kingsley.io | 2 |

**What to do:** Dedupe with `ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(email)) ORDER BY created_at)`, keeping `rn = 1`. Then add a normalized-email uniqueness constraint at the point of capture so it can't happen again.

### Unassigned deals

🔴 **CRITICAL** — **17 deals have no rep assigned**, hiding **$1,504,354** from every rep-level report.

Total pipeline is **$32,376,074**. Sum it by rep and you get **$30,871,720**.

The difference isn't a rounding error. A `JOIN` to the reps table drops any deal with a NULL `rep_id`, silently. No error is thrown.

**Business impact:** the rep report and the revenue report will never agree, and whoever notices will assume one of them is broken. Worse — nobody is following up on those deals, because nobody owns them.

**What to do:** Find them (`WHERE rep_id IS NULL`) and assign an owner. Then make `rep_id` `NOT NULL` at the schema level so a deal cannot be created without one.

### Stage-history coverage

🟡 **WARNING** — **22 deals have no stage history.** Any velocity analysis using an `INNER JOIN` silently excludes them — 5.5% of the pipeline.

`INNER JOIN` returns **378** deals. `LEFT JOIN` returns **400**.

Same tables. Same key. One keyword different. **No error either way.**

**Business impact:** every cycle-time and stage-velocity number is computed on a subset, and the report gives no indication that it is. This is the most common way a dashboard ends up quietly wrong.

**What to do:** Find out why those deals have no history — is the sync broken, or were they created before logging started? Until you know, use `LEFT JOIN` and show the unknowns as a separate bucket rather than dropping them.

### Tool adoption — the coverage gap

🟡 **WARNING** — **34 of 158 open deals (21.5%) have never been audited.** That's **$3,181,542** of open pipeline the tool has never looked at.

**This is the failure mode no error log will ever show you.** A tool with a 100% success rate that only runs on three-quarters of deals is not working — it is *unused*.

The runs that never happened don't log anything. They are invisible unless you go looking for them with a `LEFT JOIN`.

**Largest uncovered deals:**

| id | name | amount | stage |
|---|---|---|---|
| 334 | Cascade - Platform | 224523.14 | Negotiation |
| 243 | Cobalt - Expansion | 213224.30 | Prospecting |
| 124 | Havenport - Pilot | 198366.25 | Negotiation |
| 135 | Lakeshore - Platform | 188244.06 | Negotiation |
| 48 | Vantage - Expansion | 181861.34 | Negotiation |

**What to do:** Two possibilities and they need different fixes. If reps *don't know* the tool exists, that's enablement. If they know and aren't using it, the output isn't worth the click — and that's a product problem. Ask five reps before you assume which.

### Model self-consistency

🟡 **WARNING** — **8 runs scored a deal 7+ for risk and then listed ZERO red flags** (2.7% of successful runs).

The model asserted danger and produced no evidence for it. That is not an insight — it's a confident number with nothing behind it.

**You cannot catch this by looking at either column alone.** A risk score of 9 looks fine. Zero red flags looks fine. Only the *combination* is broken.

| deal_id | company | risk_score | red_flag_count | note_count |
|---|---|---|---|---|
| 227 | Northwind Freight | 10 | 0 | 7 |
| 44 | Kingsley Industries | 9 | 0 | 3 |
| 52 | Winslow Solutions | 9 | 0 | 3 |
| 331 | Northwind Systems | 9 | 0 | 6 |
| 220 | Ravenswood Labs | 8 | 0 | 2 |
| 272 | Sterling Industries | 8 | 0 | 4 |
| 170 | Orbit Systems | 7 | 0 | 6 |
| 270 | Ashgrove Media | 7 | 0 | 2 |

**What to do:** Add a validation rule to the pipeline: if `risk_score >= 7` and `red_flag_count = 0`, reject the output and route it to human review. The model should not be allowed to score high without justifying it.

---

## Analysis

_The questions that decide things._

### Does the risk score predict anything?

🟢 PASS — **Yes.** Low-risk deals win at **72.3%**. High-risk deals win at **33.3%**. A **39.0 point** spread.

The score has real signal. Win rate falls monotonically as risk rises.

**But high-risk deals still win 33.3% of the time.** It's a signal, not a verdict. Anyone treating a score of 8 as a dead deal is throwing away roughly one in three.

Score distribution is healthy — only **31%** of runs land in the middle band (4-6), so the model is committing to a judgment rather than hedging.

**The caveat that matters:** this query cannot tell you whether a rep saw a high score and *saved* the deal. A perfect early-warning system that everyone acts on would show **no correlation at all**, because every flagged deal got rescued. The tool working and the tool failing look identical in this data.

| risk_band | closed_deals | won | win_rate_pct |
|---|---|---|---|
| Low (1-3) | 47 | 34 | 72.30 |
| Medium (4-6) | 65 | 32 | 49.20 |
| High (7-10) | 78 | 26 | 33.30 |

**What to do:** Keep the feature. Cross-reference against rep activity — if flagged deals get *more* touches afterward, the tool is changing behavior and the real impact is larger than this table shows.

### Where deals stall

🔵 INFO — **Prospecting** is the slowest stage — deals sit there an average of **31 days** (worst case: 70).

The CRM shows you a deal's stage *today* and overwrote yesterday. This is reconstructed from stage history, which is the only place the movie exists.

**6 open deals have not moved in 45+ days.** Those are the calls to make this week.

| id | name | amount | stage | days_stuck | risk_score |
|---|---|---|---|---|---|
| 138 | Northwind - Renewal | 249071.69 | Negotiation | 429 | 2 |
| 15 | Pinehurst - Suite | 244640.03 | Negotiation | 108 | 7 |
| 330 | Ridgeway - Suite | 240235.05 | Negotiation | 409 | 7 |
| 224 | Halcyon - Expansion | 224635.67 | Negotiation | 80 | 8 |
| 334 | Cascade - Platform | 224523.14 | Negotiation | 63 | — |
| 156 | Brightline - Expansion | 222112.84 | Negotiation | 238 | 4 |

**What to do:** Ask why Prospecting takes 31 days. It's usually one of three things: waiting on a stakeholder who was never introduced, waiting on procurement nobody scoped, or the rep is avoiding a hard conversation.

### Cost trend

🔵 INFO — Average cost per run rose **+40%** — but **cost per note is -49.1%**, essentially flat. **The model didn't get more expensive. The inputs got bigger.**

Reps logged more calls over the period (avg notes went from 3.3 to 8.3), so each audit had more text to read.

**Same rising number, two completely different stories.** One is 'our vendor raised prices.' The other is 'the tool got more popular.' They demand opposite responses, and you cannot tell them apart without the control variable.

Total spend to date: **$2.20**.

| month | runs | avg_cost | avg_notes | cost_per_note |
|---|---|---|---|---|
| 2025-01-01 00:00:00 | 3 | 0.01 | 3.33 | 0.00 |
| 2025-02-01 00:00:00 | 14 | 0.01 | 3.07 | 0.00 |
| 2025-03-01 00:00:00 | 13 | 0.01 | 2.85 | 0.00 |
| 2025-04-01 00:00:00 | 13 | 0.01 | 3.23 | 0.00 |
| 2025-05-01 00:00:00 | 18 | 0.01 | 4.06 | 0.00 |
| 2025-06-01 00:00:00 | 17 | 0.01 | 4.53 | 0.00 |
| 2025-07-01 00:00:00 | 27 | 0.01 | 4.07 | 0.00 |
| 2025-08-01 00:00:00 | 19 | 0.01 | 4.68 | 0.00 |
| 2025-09-01 00:00:00 | 11 | 0.01 | 5.36 | 0.00 |
| 2025-10-01 00:00:00 | 24 | 0.01 | 5.71 | 0.00 |
| 2025-11-01 00:00:00 | 14 | 0.01 | 6.07 | 0.00 |
| 2025-12-01 00:00:00 | 17 | 0.01 | 6.18 | 0.00 |

_…and 6 more rows._

**What to do:** Nothing. But keep `avg_notes` next to `avg_cost` in any cost dashboard, or someone will eventually panic about the wrong number.

### Unmatched leads

🟡 **WARNING** — **217 unassigned leads could be matched to an existing company account today**, for free, just by matching the email domain.

Nobody has done it. These are people from companies already in the CRM, sitting unclaimed.

The other buckets are the honest part: personal-email leads are *correctly* unmatchable. But the last bucket includes leads at companies that **are in the database** — the join fails anyway, because that company's `domain` field is empty. **You'd swear the company was missing. It isn't. The field is.**

| category | leads |
|---|---|
| MATCHABLE NOW — money on the floor | 217 |
| Personal email (correctly unmatchable) | 123 |
| Typo, or our company record has a NULL domain | 66 |

**What to do:** Run the domain match and assign them. Then backfill the missing `domain` values on the company records — that's a one-time cleanup that permanently improves match rate.

---

## Method

Every finding above is a query, and every query is in `checks.sql` or `analysis.sql`.

The interpretation is the point. A table of numbers is raw material — this report
says what the numbers *mean* and what to do about them. A correct query nobody reads
is worth zero.

**Every AI feature I build ships with a query that could prove it useless.**
The efficacy check above is that query.
