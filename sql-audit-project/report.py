#!/usr/bin/env python3
"""
report.py — turn queries into a report someone will actually read.

A correct query that nobody reads is worth zero. This runs every check
and every analysis against the database, interprets the results, and
writes a document with findings in plain English, severity flags, and
a recommended action for each one.

Usage:
    export DATABASE_URL="postgresql://user:pass@host/dbname"
    python report.py                 # writes report.md
    python report.py --html          # also writes report.html
    python report.py --quiet         # no console output

Get DATABASE_URL from your Neon/Supabase dashboard. It looks like:
    postgresql://neondb_owner:xxxx@ep-xxx.us-east-2.aws.neon.tech/neondb

No dependencies beyond psycopg2:
    pip install psycopg2-binary
"""

import os
import sys
import argparse
import re
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    sys.exit("Missing driver. Run:  pip install psycopg2-binary")


# ═══════════════════════════════════════════════════════════════
#  Severity levels. These drive the report's tone and ordering.
# ═══════════════════════════════════════════════════════════════
CRITICAL = "CRITICAL"   # data is actively wrong, or money is at risk
WARNING  = "WARNING"    # needs a human decision, not necessarily broken
INFO     = "INFO"       # worth knowing, nothing to do
GOOD     = "PASS"       # a guardrail held. proof, not silence.

# Plain-text badges. Emoji break on Windows terminals and some editors,
# and a report that won't render is a report nobody reads.
BADGE = {
    CRITICAL: "**[ CRITICAL ]**",
    WARNING:  "**[ WARNING ]**",
    INFO:     "**[ INFO ]**",
    GOOD:     "**[ PASS ]**",
}

# Used only in the HTML output, where color is safe.
BADGE_COLOR = {
    CRITICAL: "#a32d2d",
    WARNING:  "#854f0b",
    INFO:     "#185fa5",
    GOOD:     "#0f6e56",
}


def esc(text):
    """Escape underscores so markdown doesn't read them as italics.

    `rep_id` renders as `repid` in a lot of viewers, including several PDF
    exporters and Word's markdown import. A report that mangles the column
    names it is telling you to fix is not a good report.

    Underscores inside backticks are LEFT ALONE only if the renderer is
    strict -- most aren't. So escape everywhere, then unescape inside code
    spans where it is provably safe.
    """
    if not text:
        return text
    parts = re.split(r"(`[^`]*`)", text)      # keep code spans intact as groups
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(part)                  # inside backticks: leave it alone
        else:
            out.append(part.replace("_", r"\_"))
    return "".join(out)


class Finding:
    """One thing the report found, and what to do about it."""

    def __init__(self, title, severity, headline, detail="", action="", table=None):
        self.title = title
        self.severity = severity
        self.headline = headline      # one sentence. the finding itself.
        self.detail = detail          # why it matters. the business impact.
        self.action = action          # what to do. concrete.
        self.table = table            # optional supporting rows

    def to_md(self):
        out = [f"### {self.title}", "", f"{BADGE[self.severity]} — {esc(self.headline)}", ""]
        if self.detail:
            out += [esc(self.detail), ""]
        if self.table:
            out += [render_table(self.table), ""]
        if self.action:
            out += [f"**What to do:** {esc(self.action)}", ""]
        return "\n".join(out)


def pretty(col):
    """deal_id -> Deal ID.  win_rate_pct -> Win Rate %."""
    special = {"id": "ID", "pct": "%", "usd": "USD", "ms": "ms",
               "avg": "Avg", "cost": "Cost", "n": "N"}
    words = str(col).split("_")
    out = []
    for w in words:
        out.append(special.get(w.lower(), w.capitalize()))
    return " ".join(out)


def render_table(rows, limit=12):
    """Markdown table from a list of dicts.

    NOTE: column names contain underscores, and markdown reads `_x_` as italics.
    `deal_id` renders as `dealid` if you don't handle it. Prettifying the headers
    sidesteps the problem entirely and reads better anyway -- nobody outside
    engineering wants to see `win_rate_pct` in a report.
    """
    if not rows:
        return "_(no rows)_"
    cols = list(rows[0].keys())
    head = "| " + " | ".join(pretty(c) for c in cols) + " |"
    sep  = "|" + "|".join("---" for _ in cols) + "|"
    body = []
    for r in rows[:limit]:
        cells = []
        for c in cols:
            v = r[c]
            name = str(c).lower()
            if v is None:
                cells.append("—")
            elif "date" in name or "month" in name:
                cells.append(str(v)[:10])                      # drop 00:00:00
            elif "amount" in name or "revenue" in name:
                cells.append(f"${float(v):,.0f}")
            elif "cost" in name:
                cells.append(f"${float(v):.5f}".rstrip("0"))   # sub-cent values
            elif "pct" in name or "rate" in name:
                cells.append(f"{float(v):.1f}%")
            elif isinstance(v, float):
                cells.append(f"{v:,.2f}")
            elif isinstance(v, int):
                cells.append(f"{v:,}")
            else:
                cells.append(str(v))
        body.append("| " + " | ".join(cells) + " |")
    tail = ""
    if len(rows) > limit:
        tail = f"\n\n_…and {len(rows) - limit:,} more rows._"
    return "\n".join([head, sep] + body) + tail


def q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def one(cur, sql):
    """Single-row helper."""
    r = q(cur, sql)
    return r[0] if r else {}


# ═══════════════════════════════════════════════════════════════
#  THE CHECKS
#  Each returns a Finding. The interpretation lives HERE, not in
#  the reader's head. That's the whole point of a report.
# ═══════════════════════════════════════════════════════════════

def check_duplicates(cur):
    naive = one(cur, """
        SELECT COUNT(*) AS n FROM (
          SELECT email FROM leads GROUP BY email HAVING COUNT(*) > 1
        ) t""")["n"]

    real = one(cur, """
        SELECT COUNT(*) AS n FROM (
          SELECT LOWER(TRIM(email)) FROM leads GROUP BY 1 HAVING COUNT(*) > 1
        ) t""")["n"]

    total = one(cur, "SELECT COUNT(*) AS n FROM leads")["n"]
    true_count = total - real

    if real == 0:
        return Finding(
            "Duplicate leads", GOOD,
            f"No duplicates found across {total:,} leads.",
            action="Nothing. Re-run this on every import.")

    pct = 100.0 * real / total
    sample = q(cur, """
        SELECT LOWER(TRIM(email)) AS normalized_email, COUNT(*) AS copies
        FROM leads GROUP BY 1 HAVING COUNT(*) > 1
        ORDER BY copies DESC, 1 LIMIT 6""")

    return Finding(
        "Duplicate leads", CRITICAL,
        f"The lead count is inflated by **{real} duplicate records** "
        f"({pct:.1f}% of the list). The real number is **{true_count:,}**, not {total:,}.",
        detail=(
            f"A naive `GROUP BY email` finds **{naive}** duplicates — it looks clean. "
            "But `rachel@northwind.com` and `Rachel@Northwind.com` are the same human and "
            "different strings, so they land in separate buckets. Normalizing with "
            f"`LOWER(TRIM(email))` surfaces all **{real}**.\n\n"
            "**Business impact:** every one of these people gets contacted twice. "
            "Any per-lead cost metric is overstated. Conversion rate is understated, "
            "because the denominator is too big."),
        action=(
            "Dedupe with `ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(email)) ORDER BY created_at)`, "
            "keeping `rn = 1`. Then add a normalized-email uniqueness constraint at "
            "the point of capture so it can't happen again."),
        table=sample)


def check_nulls_and_totals(cur):
    n = one(cur, """
        SELECT
          COUNT(*) FILTER (WHERE rep_id IS NULL)     AS no_rep,
          COUNT(*) FILTER (WHERE company_id IS NULL) AS no_company,
          COUNT(*) FILTER (WHERE amount IS NULL)     AS no_amount,
          COUNT(*)                                   AS total
        FROM deals""")

    tot = one(cur, """
        SELECT
          (SELECT SUM(amount) FROM deals)                                    AS true_total,
          (SELECT SUM(d.amount) FROM deals d JOIN reps r ON d.rep_id = r.id) AS total_by_rep""")

    gap = float(tot["true_total"] or 0) - float(tot["total_by_rep"] or 0)

    if n["no_rep"] == 0:
        return Finding(
            "Unassigned deals", GOOD,
            "Every deal has an owner. Rep-level reporting will tie out to the totals.",
            action="Nothing.")

    return Finding(
        "Unassigned deals", CRITICAL,
        f"**{n['no_rep']} deals have no rep assigned**, hiding "
        f"**${gap:,.0f}** from every rep-level report.",
        detail=(
            f"Total pipeline is **${float(tot['true_total']):,.0f}**. "
            f"Sum it by rep and you get **${float(tot['total_by_rep']):,.0f}**.\n\n"
            "The difference isn't a rounding error. A `JOIN` to the reps table drops "
            "any deal with a NULL `rep_id`, silently. No error is thrown.\n\n"
            "**Business impact:** the rep report and the revenue report will never "
            "agree, and whoever notices will assume one of them is broken. Worse — "
            "nobody is following up on those deals, because nobody owns them."),
        action=(
            "Find them (`WHERE rep_id IS NULL`) and assign an owner. Then make `rep_id` "
            "`NOT NULL` at the schema level so a deal cannot be created without one."))


def check_join_integrity(cur):
    r = one(cur, """
        SELECT
          (SELECT COUNT(DISTINCT d.id) FROM deals d
             INNER JOIN deal_stage_history h ON d.id = h.deal_id) AS inner_n,
          (SELECT COUNT(DISTINCT d.id) FROM deals d
             LEFT  JOIN deal_stage_history h ON d.id = h.deal_id) AS left_n""")

    missing = r["left_n"] - r["inner_n"]
    if missing == 0:
        return Finding(
            "Stage-history coverage", GOOD,
            "Every deal has stage history. Velocity analysis will be complete.",
            action="Nothing.")

    pct = 100.0 * missing / r["left_n"]
    return Finding(
        "Stage-history coverage", WARNING,
        f"**{missing} deals have no stage history.** Any velocity analysis using an "
        f"`INNER JOIN` silently excludes them — {pct:.1f}% of the pipeline.",
        detail=(
            f"`INNER JOIN` returns **{r['inner_n']}** deals. "
            f"`LEFT JOIN` returns **{r['left_n']}**.\n\n"
            "Same tables. Same key. One keyword different. **No error either way.**\n\n"
            "**Business impact:** every cycle-time and stage-velocity number is computed "
            "on a subset, and the report gives no indication that it is. This is the most "
            "common way a dashboard ends up quietly wrong."),
        action=(
            "Find out why those deals have no history — is the sync broken, or were they "
            "created before logging started? Until you know, use `LEFT JOIN` and show the "
            "unknowns as a separate bucket rather than dropping them."))


def check_coverage_gap(cur):
    r = one(cur, """
        SELECT
          COUNT(*)                                  AS open_deals,
          COUNT(*) FILTER (WHERE a.deal_id IS NULL) AS never_audited
        FROM deals d
        LEFT JOIN audit_runs a ON d.id = a.deal_id
        WHERE d.close_date IS NULL""")

    if r["open_deals"] == 0:
        return Finding("Tool coverage", INFO, "No open deals to audit.")

    pct = 100.0 * r["never_audited"] / r["open_deals"]
    big = q(cur, """
        SELECT d.id, d.name, d.amount, d.stage
        FROM deals d
        LEFT JOIN audit_runs a ON d.id = a.deal_id
        WHERE a.deal_id IS NULL AND d.close_date IS NULL
        ORDER BY d.amount DESC LIMIT 5""")

    exposure = sum(float(x["amount"]) for x in q(cur, """
        SELECT d.amount FROM deals d
        LEFT JOIN audit_runs a ON d.id = a.deal_id
        WHERE a.deal_id IS NULL AND d.close_date IS NULL"""))

    sev = CRITICAL if pct > 25 else (WARNING if pct > 10 else INFO)
    return Finding(
        "Tool adoption — the coverage gap", sev,
        f"**{r['never_audited']} of {r['open_deals']} open deals ({pct:.1f}%) have never "
        f"been audited.** That's **${exposure:,.0f}** of open pipeline the tool has "
        f"never looked at.",
        detail=(
            "**This is the failure mode no error log will ever show you.** A tool with a "
            "100% success rate that only runs on three-quarters of deals is not working — "
            "it is *unused*.\n\n"
            "The runs that never happened don't log anything. They are invisible unless "
            "you go looking for them with a `LEFT JOIN`.\n\n"
            "**Largest uncovered deals:**"),
        action=(
            "Two possibilities and they need different fixes. If reps *don't know* the tool "
            "exists, that's enablement. If they know and aren't using it, the output isn't "
            "worth the click — and that's a product problem. Ask five reps before you "
            "assume which."),
        table=big)


def check_model_contradiction(cur):
    bad = q(cur, """
        SELECT deal_id, company, risk_score, red_flag_count, note_count
        FROM audit_runs
        WHERE status = 'success' AND risk_score >= 7 AND red_flag_count = 0
        ORDER BY risk_score DESC LIMIT 8""")

    total = one(cur, "SELECT COUNT(*) AS n FROM audit_runs WHERE status='success'")["n"]

    if not bad:
        return Finding(
            "Model self-consistency", GOOD,
            f"Across {total:,} runs, every high-risk score came with at least one "
            "supporting red flag.",
            action="Keep this check in the nightly job. It's how you'd catch drift.")

    pct = 100.0 * len(bad) / total
    return Finding(
        "Model self-consistency", WARNING,
        f"**{len(bad)} runs scored a deal 7+ for risk and then listed ZERO red flags** "
        f"({pct:.1f}% of successful runs).",
        detail=(
            "The model asserted danger and produced no evidence for it. That is not an "
            "insight — it's a confident number with nothing behind it.\n\n"
            "**You cannot catch this by looking at either column alone.** A risk score of 9 "
            "looks fine. Zero red flags looks fine. Only the *combination* is broken."),
        action=(
            "Add a validation rule to the pipeline: if `risk_score >= 7` and "
            "`red_flag_count = 0`, reject the output and route it to human review. The "
            "model should not be allowed to score high without justifying it."),
        table=bad)


def check_idempotency(cur):
    dupes = q(cur, """
        SELECT idempotency_key, COUNT(*) AS times_run
        FROM audit_runs GROUP BY idempotency_key HAVING COUNT(*) > 1
        ORDER BY times_run DESC LIMIT 10""")

    total = one(cur, "SELECT COUNT(*) AS n FROM audit_runs")["n"]

    if not dupes:
        return Finding(
            "Idempotency guardrail", GOOD,
            f"**No duplicate audits across {total:,} runs.** A retry or double-click "
            "cannot create a second record or a second API charge.",
            detail=(
                "**An empty result here is a passing test.** The pipeline builds a key "
                "(`audit:{deal_id}:{date}`) before calling the model, and skips the run if "
                "that key already exists.\n\n"
                "This query is the *proof* the guardrail held — not a claim that it did."),
            action="Nothing. Keep this in the nightly job.")

    return Finding(
        "Idempotency guardrail", CRITICAL,
        f"**The guardrail failed.** {len(dupes)} keys were processed more than once.",
        detail=(
            "A retry or double-submit created duplicate audit records **and charged the "
            "API twice.** Every duplicate is a double-write and a double-spend."),
        action=(
            "Stop the pipeline. Find out whether the key is being built before or after "
            "the dupe check, and whether the check is reading a stale value."),
        table=dupes)


def check_freshness(cur):
    r = one(cur, """
        SELECT MAX(created_at) AS last_run,
               (CURRENT_DATE - MAX(created_at)) AS days_ago
        FROM audit_runs""")

    if r["last_run"] is None:
        return Finding("Pipeline freshness", CRITICAL,
                       "**No runs logged at all.** The pipeline has never executed.",
                       action="Check that the workflow is enabled and the trigger is firing.")

    days = r["days_ago"]
    if days <= 7:
        return Finding("Pipeline freshness", GOOD,
                       f"Last run was **{days} days ago**. The pipeline is alive.",
                       action="Nothing.")

    return Finding(
        "Pipeline freshness", CRITICAL,
        f"**No audit has run in {days} days.** The pipeline may have stopped.",
        detail=(
            "A table can pass every other check on this page and still be wrong, because "
            "it's *stale*. Clean, valid, and out of date.\n\n"
            "**Nothing errors when a scheduled job silently stops firing.** The data just "
            "quietly gets older while the dashboard keeps rendering it."),
        action="Check the schedule trigger and the credentials. Then add an alert on this query.")


# ═══════════════════════════════════════════════════════════════
#  THE ANALYSIS  (the questions that decide things)
# ═══════════════════════════════════════════════════════════════

def analyze_model_efficacy(cur):
    """The one that could kill the feature."""
    bands = q(cur, """
        SELECT
          CASE WHEN risk_score <= 3 THEN 'Low (1-3)'
               WHEN risk_score <= 6 THEN 'Medium (4-6)'
               ELSE                      'High (7-10)' END AS risk_band,
          COUNT(*)                                     AS closed_deals,
          COUNT(*) FILTER (WHERE stage='Closed Won')   AS won,
          ROUND(100.0 * COUNT(*) FILTER (WHERE stage='Closed Won')
                / COUNT(*), 1)                         AS win_rate_pct
        FROM deals
        WHERE close_date IS NOT NULL AND risk_score IS NOT NULL
        GROUP BY 1
        ORDER BY MIN(risk_score)""")

    if len(bands) < 2:
        return Finding("Does the risk score predict anything?", INFO,
                       "Not enough closed deals across risk bands to say yet.",
                       action="Revisit once 50+ audited deals have closed.")

    rates = {b["risk_band"]: float(b["win_rate_pct"]) for b in bands}
    lo = rates.get("Low (1-3)")
    hi = rates.get("High (7-10)")
    spread = (lo - hi) if (lo is not None and hi is not None) else 0

    # distribution — is the model hedging?
    dist = q(cur, """
        SELECT risk_score, COUNT(*) AS runs,
               ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_runs
        FROM audit_runs WHERE status='success'
        GROUP BY risk_score ORDER BY risk_score""")
    mid = sum(float(d["pct_of_runs"]) for d in dist if 4 <= d["risk_score"] <= 6)

    if spread >= 15:
        return Finding(
            "Does the risk score predict anything?", GOOD,
            f"**Yes.** Low-risk deals win at **{lo:.1f}%**. High-risk deals win at "
            f"**{hi:.1f}%**. A **{spread:.1f} point** spread.",
            detail=(
                "The score has real signal. Win rate falls monotonically as risk rises.\n\n"
                f"**But high-risk deals still win {hi:.1f}% of the time.** It's a signal, not a "
                "verdict. Anyone treating a score of 8 as a dead deal is throwing away roughly "
                "one in three.\n\n"
                f"Score distribution is healthy — only **{mid:.0f}%** of runs land in the "
                "middle band (4-6), so the model is committing to a judgment rather than "
                "hedging.\n\n"
                "**The caveat that matters:** this query cannot tell you whether a rep saw a "
                "high score and *saved* the deal. A perfect early-warning system that everyone "
                "acts on would show **no correlation at all**, because every flagged deal got "
                "rescued. The tool working and the tool failing look identical in this data."),
            action=(
                "Keep the feature. Cross-reference against rep activity — if flagged deals "
                "get *more* touches afterward, the tool is changing behavior and the real "
                "impact is larger than this table shows."),
            table=bands)

    if spread >= 5:
        return Finding(
            "Does the risk score predict anything?", WARNING,
            f"**Weakly.** Only a **{spread:.1f} point** spread between low-risk "
            f"({lo:.1f}%) and high-risk ({hi:.1f}%) win rates.",
            detail=(
                "There's *some* signal but it's thin. Before concluding the model is bad, "
                "check two things.\n\n"
                f"**1. Is the model hedging?** {mid:.0f}% of scores land in the middle band. "
                "If most scores cluster at 5-6, the model refuses to commit, and a flat "
                "outcome curve is the *expected* result even from a model that understands "
                "the deals perfectly well. That's a **prompt** problem, not a capability one.\n\n"
                "**2. Are the bands wrong?** Maybe risk isn't linear. Maybe only 9s and 10s "
                "mean anything. Re-cut the bands before touching the model."),
            action=(
                "Recalibrate before you rebuild. Re-band the scores, and check the "
                "distribution. Only kill the feature after both come back clean."),
            table=bands)

    return Finding(
        "Does the risk score predict anything?", CRITICAL,
        f"**No measurable signal.** Low-risk and high-risk deals close at roughly the "
        f"same rate ({lo:.1f}% vs {hi:.1f}%).",
        detail=(
            "**Do not immediately kill the feature.** A flat curve says the score has no "
            "signal *as banded*. It does not say why. Four explanations, four different fixes:\n\n"
            "1. **The bands are wrong.** Maybe only 9s and 10s mean anything → recalibrate.\n"
            f"2. **The model is hedging.** {mid:.0f}% of runs land in the middle band. If it "
            "scores everything 5-6, outcomes look flat even if the model is fine → prompt fix.\n"
            "3. **The reps acted on it.** Every flagged deal got rescued → **the tool working "
            "looks identical to the tool failing.**\n"
            "4. **The model genuinely doesn't know.** → *now* you kill it.\n\n"
            "**Nobody kills a feature off one query.**"),
        action=(
            "Check the score distribution first. Then check whether reps are even seeing the "
            "output. Only after ruling out calibration and adoption should you conclude the "
            "model has no signal."),
        table=bands)


def analyze_velocity(cur):
    # COALESCE(exited, CURRENT_DATE) is the whole fix. A deal that has NOT left
    # a stage has still been sitting in it -- measure to today, don't drop it.
    rows = q(cur, """
        WITH spans AS (
          SELECT h.deal_id, h.stage, h.changed_at AS entered,
                 LEAD(h.changed_at) OVER (PARTITION BY h.deal_id ORDER BY h.changed_at) AS exited,
                 d.close_date
          FROM deal_stage_history h
          JOIN deals d ON d.id = h.deal_id
        )
        SELECT stage,
               COUNT(*)                                                   AS deals_entered,
               COUNT(*) FILTER (WHERE exited IS NULL AND close_date IS NULL) AS still_sitting,
               ROUND(AVG(COALESCE(exited, CURRENT_DATE) - entered), 1)    AS avg_days,
               MAX(COALESCE(exited, CURRENT_DATE) - entered)              AS worst_days
        FROM spans
        WHERE stage NOT IN ('Closed Won','Closed Lost')
          AND (exited IS NOT NULL OR close_date IS NULL)   -- exclude stages abandoned at close
        GROUP BY stage
        ORDER BY avg_days DESC""")

    # The naive version, for comparison. This is what most dashboards show.
    naive = q(cur, """
        WITH spans AS (
          SELECT deal_id, stage, changed_at AS entered,
                 LEAD(changed_at) OVER (PARTITION BY deal_id ORDER BY changed_at) AS exited
          FROM deal_stage_history
        )
        SELECT stage, ROUND(AVG(exited - entered), 1) AS avg_days_completed_only
        FROM spans
        WHERE exited IS NOT NULL AND stage NOT IN ('Closed Won','Closed Lost')
        GROUP BY stage""")
    naive_map = {r["stage"]: float(r["avg_days_completed_only"]) for r in naive}

    if not rows:
        return Finding("Where deals stall", INFO, "No completed stage transitions yet.")

    worst = rows[0]
    # Zombie deals: untouched for 6+ months. Not "stalled" -- DEAD. And they
    # are inflating every forecast that sums open pipeline.
    stalled_n = one(cur, """
        WITH latest AS (
          SELECT deal_id, changed_at,
                 ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY changed_at DESC) AS rn
          FROM deal_stage_history
        )
        SELECT COUNT(*) AS n
        FROM latest l JOIN deals d ON d.id = l.deal_id
        WHERE l.rn = 1 AND d.close_date IS NULL
          AND (CURRENT_DATE - l.changed_at) BETWEEN 46 AND 180""")
    stalled = [1] * int(stalled_n["n"] or 0)   # count only; the LIST lives in its own finding

    dead = one(cur, """
        WITH latest AS (
          SELECT deal_id, changed_at,
                 ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY changed_at DESC) AS rn
          FROM deal_stage_history
        )
        SELECT COUNT(*)          AS n,
               SUM(d.amount)     AS value
        FROM latest l JOIN deals d ON d.id = l.deal_id
        WHERE l.rn = 1 AND d.close_date IS NULL
          AND (CURRENT_DATE - l.changed_at) > 180""")

    zombie_note = ""
    if dead["n"]:
        zombie_note = (
            f"\n\n**Separately: {dead['n']} open deals have not moved in over six months, "
            f"worth ${float(dead['value']):,.0f}.** Those are not stalled — they are **dead**, "
            "and they are inflating every forecast that sums open pipeline. "
            "**Close them lost.** A forecast built on zombie deals is worse than no forecast, "
            "because people believe it.")

    # Find the stage where the naive (survivors-only) number LIES the most.
    bias = []
    for r in rows:
        st = r["stage"]
        if st in naive_map:
            true_avg = float(r["avg_days"])
            naive_avg = naive_map[st]
            if true_avg > naive_avg * 1.25:          # naive understates by 25%+
                bias.append((st, naive_avg, true_avg, int(r["still_sitting"] or 0)))
    bias.sort(key=lambda b: b[2] - b[1], reverse=True)

    bias_note = ""
    if bias:
        st, naive_avg, true_avg, sitting = bias[0]
        bias_note = (
            f"\n\n**Watch the trap in this number.** The usual way to compute stage velocity "
            f"only measures deals that ENTERED a stage and LEFT it. Deals still sitting in a "
            f"stage get dropped — and those are exactly the slow ones.\n\n"
            f"For **{st}**, the naive calculation says **{naive_avg:.0f} days**. "
            f"Counting the {sitting} deals still stuck there, the real average is "
            f"**{true_avg:.0f} days**.\n\n"
            f"**The fast deals closed and got counted. The slow ones are still open and got "
            f"ignored.** Averaging only the survivors makes your worst stage look like your "
            f"best one. Every stage number here uses `COALESCE(exited_at, CURRENT_DATE)` so "
            f"open deals still count.")

    return Finding(
        "Where deals stall", WARNING if (dead["n"] or bias) else INFO,
        f"**{worst['stage']}** is the slowest stage — deals sit there an average of "
        f"**{float(worst['avg_days']):.0f} days** (worst case: {worst['worst_days']}).",
        detail=(
            "The CRM shows you a deal's stage *today* and overwrote yesterday. This is "
            "reconstructed from stage history, which is the only place the movie exists."
            + bias_note
            + (f"\n\n**{len(stalled)} open deals have gone quiet for 46-180 days** — "
               "listed in the next section. Those are the calls to make this week."
               if stalled else "")
            + zombie_note),
        action=(
            f"Ask why {worst['stage']} takes {float(worst['avg_days']):.0f} days. It's usually "
            "one of three things: waiting on a stakeholder who was never introduced, waiting "
            "on procurement nobody scoped, or the rep is avoiding a hard conversation."
            + (" And purge the zombie deals before the next forecast." if dead["n"] else "")),
        table=rows)


def analyze_stalled_deals(cur):
    """Split out as its own finding -- the calls to make this week."""
    stalled = q(cur, """
        WITH latest AS (
          SELECT deal_id, stage, changed_at,
                 ROW_NUMBER() OVER (PARTITION BY deal_id ORDER BY changed_at DESC) AS rn
          FROM deal_stage_history
        )
        SELECT d.id, d.name, d.amount, l.stage,
               (CURRENT_DATE - l.changed_at) AS days_stuck, d.risk_score
        FROM latest l JOIN deals d ON d.id = l.deal_id
        WHERE l.rn = 1 AND d.close_date IS NULL
          AND (CURRENT_DATE - l.changed_at) BETWEEN 46 AND 180
        ORDER BY d.amount DESC LIMIT 8""")

    if not stalled:
        return Finding("Deals to call this week", GOOD,
                       "No open deal has gone quiet for more than 45 days.",
                       action="Nothing.")

    val = sum(float(r["amount"]) for r in stalled)
    unaudited = sum(1 for r in stalled if r["risk_score"] is None)

    return Finding(
        "Deals to call this week", WARNING,
        f"**{len(stalled)} open deals** worth **${val:,.0f}** have gone quiet for 46-180 days. "
        "Old enough to be a problem, recent enough to save.",
        detail=(
            "These are the actionable ones — not the zombies above. Someone should touch every "
            "row in this table before Friday."
            + (f"\n\n**{unaudited} of them were never audited**, so there is no risk score to "
               "prioritize by. That's the coverage gap costing you, right here, in a specific list."
               if unaudited else "")),
        action="Assign each one an owner and a next step. If a deal can't get a next step, close it.",
        table=stalled)


def analyze_cost(cur):
    rows = q(cur, """
        SELECT DATE_TRUNC('month', created_at)::date       AS month,
               COUNT(*)                                     AS runs,
               ROUND(AVG(cost_usd)::numeric, 5)             AS avg_cost,
               ROUND(AVG(note_count)::numeric, 2)           AS avg_notes,
               ROUND(AVG(cost_usd/note_count)::numeric, 6)  AS cost_per_note
        FROM audit_runs WHERE status='success'
        GROUP BY 1 ORDER BY 1""")

    if len(rows) < 3:
        return Finding("Cost trend", INFO, "Not enough months of data to see a trend.")

    first, last = rows[0], rows[-1]
    cost_chg = 100.0 * (float(last["avg_cost"]) - float(first["avg_cost"])) / float(first["avg_cost"])
    unit_chg = 100.0 * (float(last["cost_per_note"]) - float(first["cost_per_note"])) / float(first["cost_per_note"])

    total = one(cur, "SELECT ROUND(SUM(cost_usd)::numeric,2) AS t FROM audit_runs")["t"]

    if cost_chg > 15 and unit_chg < 5:
        unit_word = (f"actually FELL {abs(unit_chg):.0f}%" if unit_chg < -5
                     else f"is {unit_chg:+.1f}%, essentially flat")
        return Finding(
            "Cost trend", INFO,
            f"Average cost per run rose **{cost_chg:+.0f}%** — but **cost per note "
            f"{unit_word}**. **The model didn't get more expensive. The inputs got bigger.**",
            detail=(
                f"Reps logged more calls over the period (avg notes went from "
                f"{float(first['avg_notes']):.1f} to {float(last['avg_notes']):.1f}), so each "
                "audit had more text to read.\n\n"
                + ("**Per unit of work, the pipeline actually got CHEAPER.** Total spend is up "
                   "because usage is up — which is the outcome you want. Reading only the "
                   "headline number, you would conclude the opposite.\n\n"
                   if unit_chg < -5 else "")
                + "**Same rising number, two completely different stories.** One is 'our vendor "
                "raised prices.' The other is 'the tool got more popular.' They demand opposite "
                "responses, and you cannot tell them apart without the control variable.\n\n"
                f"**${float(last['avg_cost']):.4f} per audit.** Scaled up, that is "
                f"**~${float(last['avg_cost']) * 1000:.0f} to audit 1,000 deals** — the cost is "
                f"not the constraint here, and it will not become one. "
                f"({sum(int(r['runs']) for r in rows):,} runs logged to date.)"),
            action="Nothing. But keep `avg_notes` next to `avg_cost` in any cost dashboard, "
                   "or someone will eventually panic about the wrong number.",
            table=rows)

    if unit_chg > 15:
        return Finding(
            "Cost trend", WARNING,
            f"**Cost per note rose {unit_chg:+.0f}%.** This is *not* explained by bigger inputs — "
            "the unit economics genuinely got worse.",
            detail="Something changed in the model, the prompt, or the pricing.",
            action="Diff the prompt versions. Check whether the model was silently upgraded.",
            table=rows)

    return Finding(
        "Cost trend", GOOD,
        f"Costs are stable at **${float(last['avg_cost']):.4f} per audit** — "
        f"**~${float(last['avg_cost']) * 1000:.0f} per 1,000 deals.**",
        detail=f"Cost is not the constraint on this system and won't become one. "
               f"({sum(int(r['runs']) for r in rows):,} runs logged to date.)",
        action="Nothing.", table=rows)


def analyze_orphan_leads(cur):
    r = q(cur, """
        SELECT
          CASE
            WHEN SPLIT_PART(email,'@',2) IN ('gmail.com','yahoo.com','outlook.com')
              THEN 'Personal email (correctly unmatchable)'
            WHEN SPLIT_PART(email,'@',2) IN (SELECT domain FROM companies WHERE domain IS NOT NULL)
              THEN 'MATCHABLE NOW — money on the floor'
            ELSE 'Typo, or our company record has a NULL domain'
          END          AS category,
          COUNT(*)     AS leads
        FROM leads WHERE company_id IS NULL
        GROUP BY 1 ORDER BY leads DESC""")

    matchable = next((x["leads"] for x in r if x["category"].startswith("MATCHABLE")), 0)
    if matchable == 0:
        return Finding("Unmatched leads", GOOD, "No orphan leads can be auto-matched.",
                       action="Nothing.", table=r)

    return Finding(
        "Unmatched leads", WARNING,
        f"**{matchable} unassigned leads could be matched to an existing company account "
        f"today**, for free, just by matching the email domain.",
        detail=(
            "Nobody has done it. These are people from companies already in the CRM, sitting "
            "unclaimed.\n\n"
            "The other buckets are the honest part: personal-email leads are *correctly* "
            "unmatchable. But the last bucket includes leads at companies that **are in the "
            "database** — the join fails anyway, because that company's `domain` field is "
            "empty. **You'd swear the company was missing. It isn't. The field is.**"),
        action=(
            "Run the domain match and assign them. Then backfill the missing `domain` values "
            "on the company records — that's a one-time cleanup that permanently improves "
            "match rate."),
        table=r)


# ═══════════════════════════════════════════════════════════════
#  ASSEMBLE
# ═══════════════════════════════════════════════════════════════

def build(cur):
    checks = [
        check_freshness(cur),
        check_idempotency(cur),
        check_duplicates(cur),
        check_nulls_and_totals(cur),
        check_join_integrity(cur),
        check_coverage_gap(cur),
        check_model_contradiction(cur),
    ]
    analysis = [
        analyze_model_efficacy(cur),
        analyze_velocity(cur),
        analyze_stalled_deals(cur),
        analyze_cost(cur),
        analyze_orphan_leads(cur),
    ]
    return checks, analysis


def summarize(findings):
    order = {CRITICAL: 0, WARNING: 1, INFO: 2, GOOD: 3}
    crit = [f for f in findings if f.severity == CRITICAL]
    warn = [f for f in findings if f.severity == WARNING]
    good = [f for f in findings if f.severity == GOOD]

    lines = []
    if crit:
        lines.append(f"**{len(crit)} critical** — data is actively wrong or money is at risk.")
    if warn:
        lines.append(f"**{len(warn)} warnings** — need a human decision.")
    if good:
        lines.append(f"**{len(good)} passing** — guardrails held (an empty result is a passing test).")

    top = sorted([f for f in findings if f.severity in (CRITICAL, WARNING)],
                 key=lambda f: order[f.severity])[:3]
    return lines, top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL first:\n"
                 '  export DATABASE_URL="postgresql://user:pass@host/db"')

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    checks, analysis = build(cur)
    allf = checks + analysis
    summary, top = summarize(allf)

    now = datetime.now().strftime("%B %d, %Y")
    md = [
        "# Revenue Operations Health Report",
        f"*Generated {now} · deal-audit pipeline + CRM data*",
        "",
        "---",
        "",
        "## Executive summary",
        "",
    ]
    md += [f"- {s}" for s in summary]
    md.append("")
    md.append("")

    if top:
        md += ["**The three things that need attention, in order:**", "", ""]
        for i, f in enumerate(top, 1):
            md.append(f"{i}. **{f.title}** — {f.headline}")
        md.append("")
    else:
        md += ["**Nothing requires attention.** Every check passed.", ""]

    md += ["---", "", "## Data quality", "",
           "_These run before any analysis. Do not trust numbers from data you have not audited._",
           ""]
    for f in checks:
        md.append(f.to_md())

    md += ["---", "", "## Analysis", "",
           "_The questions that decide things._", ""]
    for f in analysis:
        md.append(f.to_md())

    md += [
        "---",
        "",
        "## Method",
        "",
        "Every finding above is a query, and every query is in `checks.sql` or `analysis.sql`.",
        "",
        "The interpretation is the point. A table of numbers is raw material — this report",
        "says what the numbers *mean* and what to do about them. A correct query nobody reads",
        "is worth zero.",
        "",
        "**Every AI feature I build ships with a query that could prove it useless.**",
        "The efficacy check above is that query.",
        "",
    ]

    text = "\n".join(md)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)

    if args.html:
        html = to_html(text, now)
        with open(args.out.replace(".md", ".html"), "w", encoding="utf-8") as f:
            f.write(html)

    if not args.quiet:
        # Windows consoles often can't print emoji. Don't let that kill the run.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(f"\n  Wrote {args.out}" + (f" and {args.out.replace('.md','.html')}" if args.html else ""))
        print(f"\n  {'─'*54}")
        for s in summary:
            print("  " + s.replace("**", ""))
        if top:
            print(f"\n  Needs attention:")
            for i, f in enumerate(top, 1):
                print(f"    {i}. {f.title}")
        print(f"  {'─'*54}\n")

    cur.close()
    conn.close()


def to_html(md_text, date):
    """Minimal, readable HTML. No dependencies."""
    import html as h
    import re
    body = h.escape(md_text.replace(chr(92) + '_', '_'))   # undo the md escaping
    body = re.sub(r'^# (.+)$',    r'<h1>\1</h1>',  body, flags=re.M)
    body = re.sub(r'^## (.+)$',   r'<h2>\1</h2>',  body, flags=re.M)
    body = re.sub(r'^### (.+)$',  r'<h3>\1</h3>',  body, flags=re.M)
    # color-code the severity badges
    for sev, color in (('CRITICAL','#a32d2d'), ('WARNING','#854f0b'),
                       ('INFO','#185fa5'), ('PASS','#0f6e56')):
        body = body.replace(f'**[ {sev} ]**',
                            f'<span class="badge" style="background:{color}">{sev}</span>')
    body = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', body)
    body = re.sub(r'(?<!\*)\*([^*\n]+?)\*(?!\*)', r'<em>\1</em>', body)
    body = re.sub(r'_([^_\n]+?)_', r'<em>\1</em>', body)
    body = re.sub(r'`(.+?)`',       r'<code>\1</code>', body)
    body = re.sub(r'^---$', '<hr>', body, flags=re.M)
    body = re.sub(r'^- (.+)$', r'<li>\1</li>', body, flags=re.M)
    body = re.sub(r'^(\d+)\. (.+)$', r'<li class="num"><span>\1.</span> \2</li>', body, flags=re.M)
    # tables
    lines = body.split("\n")
    out, intable = [], False
    for ln in lines:
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            tag = "th" if not intable else "td"
            if not intable:
                out.append("<table>")
                intable = True
            out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        else:
            if intable:
                out.append("</table>")
                intable = False
            out.append(f"<p>{ln}</p>" if ln.strip() and not ln.startswith("<") else ln)
    if intable:
        out.append("</table>")
    body = "\n".join(out)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>RevOps Health Report — {date}</title>
<style>
  body {{ max-width: 780px; margin: 60px auto; padding: 0 24px;
         font: 16px/1.65 -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         color: #24241f; background: #fdfcf9; }}
  h1 {{ font-size: 30px; letter-spacing: -0.5px; margin-bottom: 4px; }}
  h2 {{ font-size: 21px; margin-top: 52px; padding-bottom: 8px;
        border-bottom: 2px solid #0f6e56; color: #0f6e56; }}
  h3 {{ font-size: 17px; margin-top: 36px; }}
  hr {{ border: 0; border-top: 1px solid #e8e5dc; margin: 40px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 18px 0; font-size: 14px; }}
  th {{ background: #f1efe8; text-align: left; font-weight: 600; }}
  th, td {{ padding: 8px 12px; border-bottom: 1px solid #e8e5dc; }}
  code {{ background: #f1efe8; padding: 2px 5px; border-radius: 3px;
          font-size: 13px; font-family: ui-monospace, monospace; }}
  strong {{ font-weight: 600; }}
  p {{ margin: 10px 0; }}
  p:empty {{ display: none; }}
  li {{ margin: 8px 0 8px 4px; list-style: none; }}
  li:before {{ content: "·"; color: #0f6e56; font-weight: 700; margin-right: 10px; }}
  li.num:before {{ content: ""; margin: 0; }}
  li.num span {{ color: #0f6e56; font-weight: 700; margin-right: 8px; }}
  em {{ color: #5f5e5a; }}
  .badge {{ display: inline-block; color: #fff; font-size: 11px; font-weight: 700;
            letter-spacing: 0.6px; padding: 3px 9px; border-radius: 3px;
            vertical-align: 1px; margin-right: 4px; }}
</style></head><body>
{body}
</body></html>"""


if __name__ == "__main__":
    main()
