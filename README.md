# Auditing an AI Feature with SQL

I built an AI tool that scores deal health. Then I built the queries that would tell me whether it was worth keeping.

This repo is the second part.

---

## The problem with shipping AI

An AI feature is easy to ship and hard to trust. It produces confident output. It looks correct. And if it's wrong, **nothing errors** — you just get a plausible number that quietly informs a bad decision.

So I logged every run to Postgres — cost, latency, tokens, schema-validation status — and then wrote the analysis that could prove the whole thing was decoration.

The last query in `analysis.sql` is the one that could get the feature killed. That's the point of writing it.

---

## The data is deliberately broken

I generated the dataset rather than using a clean sample. **Clean data teaches you nothing.** Every query returns the obvious answer, you never see a wrong one, and you learn to trust output instead of checking it.

So this is seeded, on purpose, with the failure modes real CRM data actually has:

| Trap | What a lazy query does |
|---|---|
| 22 deals with **no stage history** | `INNER JOIN` silently drops them. `LEFT JOIN` doesn't. Different answer, no error. |
| 17 deals with **NULL `rep_id`** | "Revenue by rep" quietly excludes $1.5M and won't tie out to the revenue report. |
| 30 **duplicate leads**, differing only by casing or trailing whitespace | `GROUP BY email` finds **zero** duplicates. You'd swear the data was clean. It isn't — you have 469 leads, not 499. |
| 7 companies with a **NULL domain** | Lead-to-account matching fails on a company that **is in the database**. The sneakiest one. |
| 20 companies that are **subsidiaries** | A flat `GROUP BY company` scatters the revenue and nobody ever rolls it up to the parent. |
| 10 audit runs scoring **7+ risk with ZERO red flags** | The model asserted danger and produced no evidence. Invisible unless you compare two columns that should agree. |
| Cost per run **rising over time** | It looks like the model got expensive. It didn't. The inputs got bigger. |

Every single one of those produces a plausible number and no error message.

**That is the entire reason to be able to read SQL in a world where AI can write it.**

---

## The output is a report, not a table

**A correct query that nobody reads is worth zero.**

`report.py` runs every check, interprets the results, and writes a document a human can act on — severity flags, findings in plain English, and a recommended action for each one.

```bash
export DATABASE_URL="postgresql://..."
python report.py --html
```

Sample output is committed: **[`report_sample.md`](report_sample.md)**

It opens like this:

> ## Executive summary
>
> - **3 critical** — data is actively wrong or money is at risk.
> - **6 warnings** — need a human decision.
> - **2 passing** — guardrails held (an empty result is a passing test).
>
> **The three things that need attention, in order:**
>
> 1. **Pipeline freshness** — No audit has run in 14 days. The pipeline may have stopped.
> 2. **Duplicate leads** — The lead count is inflated by 30 duplicate records. The real number is **469**, not 499.
> 3. **Unassigned deals** — 17 deals have no rep assigned, hiding **$1,504,354** from every rep-level report.

Every finding carries the business impact and what to do next — not just the number.

---

## What's here

```
schema.sql          the tables (CRM + the AI pipeline's telemetry)
generate.py         builds the dataset, traps and all. seeded, reproducible.
seed.sql            the generated output — just run it
checks.sql          seven data-quality checks. run these FIRST.
analysis.sql        the real questions. run these SECOND.
report.py           runs everything, writes the report above
report_sample.md    what it produces
WALKTHROUGH.md      every query explained, plus what to say about it
```

### Run it

Any Postgres will do — a free [Neon](https://neon.tech) or [Supabase](https://supabase.com) instance takes five minutes.

```bash
psql $DATABASE_URL -f schema.sql
psql $DATABASE_URL -f seed.sql

pip install psycopg2-binary
export DATABASE_URL="postgresql://..."
python report.py --html          # → report.md + report.html
```

Or run the queries by hand, one at a time, with `WALKTHROUGH.md` open next to you. That's the way to actually learn them.

---

## checks.sql — the seven data quality checks

Run these **before** you analyze anything. Do not trust data you have not audited.

1. **Duplicates** — and why `GROUP BY email` misses all 30 of them
2. **NULLs in critical columns** — and proof that the totals don't tie out
3. **Orphan rows** — `INNER JOIN` vs `LEFT JOIN` returning different numbers, silently
4. **The coverage gap** — which deals the AI tool *never looked at*
5. **Internal contradiction** — is the model saying "high risk" and then failing to say why?
6. **Outliers** — SQL finds the candidates, business context decides
7. **Idempotency** — did the guardrail actually hold, or did a retry double-charge us?

**Check 4 is the quiet one.** A tool with a 100% success rate and twelve runs a week is a tool **nobody adopted**. No error log will ever tell you that. Only a `LEFT JOIN` will.

---

## analysis.sql — the questions that decide things

**Pipeline velocity — and the query that lies.** The obvious way to compute time-in-stage only counts deals that *entered a stage and left it*. Deals still sitting there get dropped — and those are exactly the slow ones.

The naive query says Negotiation takes **23 days**. Counting the **132 deals still stuck there**, the real number is **173 days**.

**The fast deals closed and got counted. The slow ones are still open and got ignored.** Averaging only the survivors makes your worst stage look like your best one. `analysis.sql` has both versions side by side, so you can run them and watch the number move 150 days.

**Cost drift.** Average cost per run rose **40%** over the year. Looks alarming. But cost *per note* actually **fell 49%** — the model didn't get expensive, the inputs got bigger. Reps logged more calls, so each audit had more to read. **Same rising number, two completely different stories, and they need opposite responses.** The control variable is the whole point.

**Model drift.** Same trap, different column. If average risk climbs while note count also climbs, the data changed and the model is fine. If risk climbs and notes stay flat, **the model's judgment moved and nobody touched it.**

**Rep performance.** The naive version excludes 17 unassigned deals and punishes new reps for being new. The fair version controls for tenure. Fire someone off the first query and you fire the wrong person.

**Subsidiary rollup.** A recursive CTE that walks the parent-child tree so a $659k customer group stops looking like three unrelated $200k accounts.

**Attribution.** First-touch vs last-touch, `ROW_NUMBER()` ascending vs descending. They disagree. That disagreement is the argument sales and marketing have every quarter.

### And then the last one

```sql
-- Did a high risk score actually predict a loss?
```

| Risk band | Closed | Won | Win rate |
|---|---|---|---|
| Low (1–3) | 47 | 34 | **72.3%** |
| Medium (4–6) | 65 | 32 | **49.2%** |
| High (7–10) | 78 | 26 | **33.3%** |

The score predicts. But **high-risk deals still win a third of the time** — it's a signal, not a verdict. That's an honest result, and it's the one I'd have to defend in a room.

**But a flat result would not have meant "kill it."** That's the part people get wrong.

A flat curve tells you the score has no signal *as banded*. It does **not** tell you why. There are four explanations and they call for four completely different actions:

1. **The bands are wrong.** Maybe only 9s and 10s mean anything and 1–8 is noise. → *Re-cut the bands.* Calibration fix.
2. **The model is hedging.** If it scores everything 5–6, outcomes look flat even if it understands the deals fine. Check the *distribution* before the outcomes. → *Prompt fix.*
3. **The reps acted on it.** A perfect early-warning system that everyone uses shows **no correlation at all** — because every flagged deal got *rescued*. **The tool working looks identical to the tool failing.**
4. **The model genuinely doesn't know.** → *Now* you kill it.

**Nobody kills a feature off one query.** This one opens the investigation. It doesn't close it.

And #3 is the one that should bother you: the SQL cannot distinguish "useless model" from "model so good that everyone acted on it." Only knowing whether reps actually use the tool can.

**The SQL is the easy half.**

---

## Why I built it

Because I don't think "we shipped an AI feature" is an accomplishment. Anyone can wire an LLM to a CRM.

The work is knowing whether it's lying, whether anyone's using it, whether it's drifting, and whether it should exist at all — and being able to answer all four with a query instead of a vibe.

**Every AI feature I build ships with a query that could prove it useless.**

---

*Learning project. The data is generated, the pipeline it models is real — the deal-audit tool that writes to `audit_runs` is a working n8n + Airtable + Postgres build. Full case study at [johnzebellportfolio.vercel.app](https://johnzebellport
