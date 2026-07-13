#!/usr/bin/env python3
"""
Generate the sandbox dataset.

The point of this script is NOT to produce clean data.

Clean data teaches you nothing. Every query returns the obvious answer, you
never see a wrong one, and you learn to trust output instead of checking it.

So this generates data that will LIE to a lazy query. It seeds, on purpose,
the failure modes real CRM data actually has:

  - deals with no stage history        -> INNER JOIN silently drops them
  - NULL rep_id on some deals          -> "deals per rep" quietly excludes them
  - NULL company domain                -> lead-to-account match fails on a company
                                          that IS in the table
  - duplicate contacts, differing only
    by casing or trailing whitespace   -> COUNT(DISTINCT email) overcounts
  - a company whose parent is set       -> flat GROUP BY misses subsidiary revenue
  - risk scores that mostly predict
    outcomes, but not always            -> so "did the score work" has an HONEST answer
  - cost per run creeping up over time  -> but only because note_count grew, not
                                          because the model changed. Two different
                                          stories, one rising number.

Every one of those produces a plausible number and no error message.
That is the entire reason to be able to read SQL.

Usage:
    python generate.py            -> writes seed.sql (run it against any Postgres)
    python generate.py --stats    -> also print what got seeded

No dependencies. Standard library only. Seeded RNG, so it is reproducible.
"""

import random
import argparse
from datetime import date, datetime, timedelta

SEED = 42
random.seed(SEED)

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
N_COMPANIES = 120
N_REPS = 8
N_DEALS = 400
N_LEADS = 500
START = date(2025, 1, 6)
END = date(2026, 6, 29)

STAGES = ["Prospecting", "Discovery", "Demo", "Negotiation"]
TERMINAL = ["Closed Won", "Closed Lost"]

# ─────────────────────────────────────────────────────────────
# Name pools (no external deps)
# ─────────────────────────────────────────────────────────────
CO_A = ["Northwind", "Cascade", "Fielder", "Brightline", "Meridian", "Halcyon",
        "Orbit", "Vantage", "Keystone", "Alder", "Ridgeway", "Silverpine",
        "Copperfield", "Ironwood", "Blackrock", "Fairmont", "Lakeshore",
        "Redstone", "Cobalt", "Thornbury", "Winslow", "Marlowe", "Ashgrove",
        "Pinehurst", "Havenport", "Sterling", "Kingsley", "Ravenswood"]
CO_B = ["Logistics", "Health", "Manufacturing", "Systems", "Freight", "Retail",
        "Software", "Legal", "Property", "Energy", "Media", "Analytics",
        "Partners", "Group", "Industries", "Labs", "Holdings", "Solutions"]
TLD = ["com", "io", "co", "net", "ai"]

FIRST = ["Rachel", "Dmitri", "Sam", "Nora", "Jonah", "Ivy", "Wes", "Paul", "Lena",
         "Cal", "Bree", "Marcus", "Priya", "Tom", "Elise", "Dana", "Hugo", "Mira",
         "Owen", "Tess", "Rafi", "Junie", "Ken", "Sable", "Yuri", "Nell"]
LAST = ["Voss", "Kalb", "Ortega", "Blake", "Reeves", "Chen", "Truong", "Ibarra",
        "Fox", "Deering", "Nakamura", "Obi", "Raman", "Castellanos", "Hartmann",
        "Whitfield", "Larkin", "Adeyemi", "Petrov", "Ndiaye", "Kowalski"]

CAMPAIGNS = [
    ("Q1 Search - Ops", "paid_search"),
    ("Ops Newsletter", "email"),
    ("Product Webinar", "webinar"),
    ("Industry Whitepaper", "content"),
    ("LinkedIn Retarget", "paid_social"),
    ("Benchmark Report", "content"),
    ("Partner Referral", "referral"),
    ("Organic Blog", "organic"),
    ("Conference Booth", "event"),
]

TIERS = ["SMB", "Mid-Market", "Enterprise"]
REGIONS = ["West", "East", "Central"]


def esc(s):
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def rand_date(a=START, b=END):
    return a + timedelta(days=random.randint(0, (b - a).days))


# ─────────────────────────────────────────────────────────────
# COMPANIES
# ─────────────────────────────────────────────────────────────
companies = []
used = set()
i = 0
while len(companies) < N_COMPANIES:
    name = f"{random.choice(CO_A)} {random.choice(CO_B)}"
    if name in used:
        continue
    used.add(name)
    i += 1
    slug = name.split()[0].lower()
    domain = f"{slug}.{random.choice(TLD)}"

    # TRAP: ~6% of companies have a NULL domain.
    # The company EXISTS. The lead-to-account join will still fail.
    # This is the sneaky one — you'd swear the company is missing.
    if random.random() < 0.06:
        domain = None

    companies.append({
        "id": i,
        "name": name,
        "domain": domain,
        "tier": random.choices(TIERS, weights=[0.45, 0.40, 0.15])[0],
        "parent_id": None,
    })

# TRAP: subsidiaries. A flat GROUP BY company will scatter this revenue across
# multiple rows and nobody will ever roll it up to the parent.
# Needs a recursive CTE.
parents = random.sample([c["id"] for c in companies[:40]], 6)
subs = random.sample([c["id"] for c in companies[60:]], 14)
for k, s in enumerate(subs):
    companies[s - 1]["parent_id"] = parents[k % len(parents)]
    # some subsidiaries share the parent's domain — another matching headache
    if random.random() < 0.5:
        companies[s - 1]["domain"] = companies[parents[k % len(parents)] - 1]["domain"]

# ─────────────────────────────────────────────────────────────
# REPS
# ─────────────────────────────────────────────────────────────
reps = []
for r in range(1, N_REPS + 1):
    reps.append({
        "id": r,
        "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
        "region": random.choice(REGIONS),
        # some reps are brand new -> they should look worse. Ramp is a real effect
        # and if you don't control for it you'll fire a rep for being new.
        "start_date": rand_date(date(2023, 1, 1), date(2026, 3, 1)),
    })

# ─────────────────────────────────────────────────────────────
# DEALS + STAGE HISTORY + AUDIT RUNS
# ─────────────────────────────────────────────────────────────
deals = []
history = []
audits = []
hid = 0
aid = 0

for d in range(1, N_DEALS + 1):
    co = random.choice(companies)
    created = rand_date(START, END - timedelta(days=20))

    # TRAP: ~5% of deals have NO rep assigned.
    # "Revenue per rep" will quietly exclude these and the total won't tie out.
    rep_id = None if random.random() < 0.05 else random.choice(reps)["id"]

    amount = round(random.choice([
        random.uniform(8000, 30000),      # SMB
        random.uniform(30000, 90000),     # mid
        random.uniform(90000, 250000),    # enterprise
    ]), 2)

    # ── Is it closed?
    closed = random.random() < 0.62
    won = None
    close_date = None

    # ── Deal health drives BOTH the risk score and the real outcome,
    #    but with noise. So the score is a decent predictor, not a perfect one.
    #    That's what makes "did the score work?" an honest question instead of
    #    a victory lap.
    health = random.random()   # 0 = doomed, 1 = healthy

    # note_count grows over the year (reps logged more calls as adoption improved).
    # This is the CONTROL VARIABLE. Cost per run will rise, and it will look like
    # the model got expensive. It didn't. The INPUT got bigger.
    months_in = (created - START).days / 30.0
    note_count = max(1, int(random.gauss(3 + months_in * 0.35, 1.4)))
    note_count = min(note_count, 12)

    # risk score: inversely tracks health, with real noise
    base_risk = (1 - health) * 9 + 1
    risk = int(max(1, min(10, round(random.gauss(base_risk, 1.6)))))

    # red flags should CORRELATE with risk. When they don't, that's a bug
    # in the model — it said "at risk" and couldn't say why.
    flags = int(max(0, min(8, round(risk * 0.6 + random.gauss(0, 1.1)))))

    # TRAP: 3% of the time force a contradiction — high risk, zero flags.
    # The model asserted danger and produced no evidence. You will only ever
    # catch this by comparing two columns that should agree.
    if random.random() < 0.03:
        risk = random.randint(7, 10)
        flags = 0

    # ── audited?
    # TRAP: ~22% of deals were NEVER audited. This is the coverage gap.
    # A tool with a 100% success rate and low adoption is a tool nobody uses.
    # NO ERROR LOG WILL EVER TELL YOU THIS. Only a LEFT JOIN will.
    audited = random.random() > 0.22
    audited_at = None
    if audited:
        audited_at = created + timedelta(days=random.randint(3, 45))
        if audited_at > END:
            audited_at = END

    if closed:
        cycle = int(random.gauss(75 - health * 25, 28))
        cycle = max(14, min(cycle, 260))
        close_date = created + timedelta(days=cycle)
        if close_date > END:
            close_date = END
        # healthier deals win more often — but not always. Noise is the point.
        won = random.random() < (0.15 + health * 0.65)
        stage = "Closed Won" if won else "Closed Lost"
    else:
        stage = random.choice(STAGES)

    deals.append({
        "id": d, "company_id": co["id"], "rep_id": rep_id,
        "name": f"{co['name'].split()[0]} - {random.choice(['Platform','Core','Renewal','Expansion','Pilot','Suite'])}",
        "amount": amount, "stage": stage,
        "deal_type": random.choices(["New Business", "Expansion", "Renewal"],
                                    weights=[0.6, 0.25, 0.15])[0],
        "created_at": created, "close_date": close_date,
        "risk_score": risk if audited else None,
        "audited_at": audited_at,
        "note_count": note_count,
    })

    # ── STAGE HISTORY ────────────────────────────────────────
    # TRAP: ~8% of deals get NO history rows at all.
    # The deal exists in `deals`. It does not exist in `deal_stage_history`.
    # INNER JOIN drops it. LEFT JOIN keeps it. Different answer, no error.
    if random.random() < 0.08:
        continue

    t = created
    path = STAGES[:]
    # TRAP: sometimes a deal SKIPS a stage. Real pipelines are not tidy.
    if random.random() < 0.18:
        path.remove(random.choice(["Discovery", "Demo"]))

    for st in path:
        hid += 1
        history.append({"id": hid, "deal_id": d, "stage": st, "changed_at": t})
        # unhealthy deals STALL. That stall is the thing velocity analysis finds.
        dwell = int(random.gauss(18 + (1 - health) * 30, 12))
        t = t + timedelta(days=max(3, dwell))
        if t > END:
            break

    if closed and close_date:
        hid += 1
        history.append({"id": hid, "deal_id": d, "stage": stage,
                        "changed_at": close_date})

    # ── AUDIT RUNS (the AI pipeline's own telemetry) ─────────
    if audited:
        aid += 1
        # cost scales with input size. This is the whole point of the drift query.
        in_tok = 380 + note_count * 165 + random.randint(-40, 60)
        out_tok = random.randint(150, 280)
        cost = round(in_tok * 0.000003 + out_tok * 0.000015, 6)
        latency = random.randint(9000, 24000)

        # ~4% of runs fail schema validation. They are LOGGED, not written.
        # A bad output is a failure to fix, not something to paper over.
        ok = random.random() > 0.04
        audits.append({
            "id": aid, "deal_id": d,
            "idem_key": f"audit:{d}:{audited_at.isoformat()}",
            "company": co["name"],
            "status": "success" if ok else "schema_violation",
            "validation_error": None if ok else random.choice([
                "risk_score out of range (got 14, expected 1-10)",
                "momentum not in enum (got 'sideways')",
                "red_flags was string, expected array",
            ]),
            "risk_score": risk if ok else None,
            "red_flag_count": flags if ok else None,
            "note_count": note_count,
            "model": "claude-sonnet-4",
            "prompt_version": "v1" if audited_at < date(2025, 9, 1) else "v2",
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cost_usd": cost, "latency_ms": latency,
            "created_at": audited_at,
        })

# ─────────────────────────────────────────────────────────────
# LEADS  (the orphan / dedupe playground)
# ─────────────────────────────────────────────────────────────
leads = []
lid = 0
for _ in range(N_LEADS):
    lid += 1
    co = random.choice(companies)
    f, l = random.choice(FIRST), random.choice(LAST)

    if co["domain"] and random.random() < 0.72:
        email = f"{f.lower()}@{co['domain']}"
    else:
        # personal domains: correctly unmatchable. Not a bug.
        email = f"{f.lower()}.{l.lower()}@{random.choice(['gmail.com','yahoo.com','outlook.com'])}"

    # TRAP: ~12% of leads are typo'd domains — off by one character.
    # Looks like a real corporate email. Will never match.
    if co["domain"] and random.random() < 0.12:
        bad = co["domain"].replace("o", "", 1) if "o" in co["domain"] else co["domain"][:-1]
        email = f"{f.lower()}@{bad}"

    # only ~18% have already been matched to a company by sales
    company_id = co["id"] if random.random() < 0.18 else None

    leads.append({
        "id": lid, "email": email, "full_name": f"{f} {l}",
        "company_id": company_id,
        "utm_source": random.choice(["google", "linkedin", "webinar", "referral",
                                     "organic", "conference"]),
        "created_at": rand_date(),
    })

# First: strip accidental exact-duplicate emails produced by random name collisions.
# We want the ONLY duplicates in this dataset to be the case/whitespace ones below,
# so the lesson is clean: a naive GROUP BY email finds NOTHING, and you'd swear
# the data was fine.
seen_email = set()
_clean = []
for ld in leads:
    if ld["email"] in seen_email:
        continue
    seen_email.add(ld["email"])
    _clean.append(ld)
leads = _clean
# reindex so ids stay contiguous
for n, ld in enumerate(leads, start=1):
    ld["id"] = n
lid = len(leads)

# TRAP: DUPLICATES. Same human, different casing / trailing whitespace.
# ROW_NUMBER() alone will NOT catch these, because they are not an exact match.
# You need LOWER(TRIM(email)) inside the PARTITION BY.
# COUNT(DISTINCT email) will happily tell you that you have more leads than you do.
dupes = random.sample(leads, 30)
for src in dupes:
    lid += 1
    e = src["email"]
    style = random.random()
    if style < 0.4:
        e = e.capitalize()                 # Rachel@northwind.com
    elif style < 0.7:
        e = e + " "                        # trailing whitespace
    else:
        e = e.upper()                      # RACHEL@NORTHWIND.COM
    leads.append({
        "id": lid, "email": e, "full_name": src["full_name"],
        "company_id": src["company_id"], "utm_source": random.choice(["google", "linkedin"]),
        "created_at": src["created_at"] + timedelta(days=random.randint(1, 20)),
    })

# ─────────────────────────────────────────────────────────────
# MARKETING TOUCHES (attribution playground)
# ─────────────────────────────────────────────────────────────
touches = []
tid = 0
deal_by_co = {}
for d in deals:
    deal_by_co.setdefault(d["company_id"], []).append(d)

for ld in leads:
    n = random.choices([1, 2, 3, 4, 5], weights=[0.25, 0.3, 0.25, 0.13, 0.07])[0]
    # link the touch to a deal at that lead's company, if one exists
    dl = None
    if ld["company_id"] and ld["company_id"] in deal_by_co:
        dl = random.choice(deal_by_co[ld["company_id"]])["id"]
    t = ld["created_at"]
    for _ in range(n):
        tid += 1
        camp, chan = random.choice(CAMPAIGNS)
        touches.append({
            "id": tid, "lead_id": ld["id"], "deal_id": dl,
            "campaign": camp, "channel": chan, "touched_at": t,
        })
        t = t + timedelta(days=random.randint(3, 40))
        if t > END:
            break

# ─────────────────────────────────────────────────────────────
# EMIT SQL
# ─────────────────────────────────────────────────────────────
def emit():
    out = []
    w = out.append

    w("-- ===========================================================")
    w("-- SEED DATA  (generated by generate.py, seed=42, reproducible)")
    w("--")
    w("-- This data is deliberately messy. See generate.py for what was")
    w("-- seeded and why. Clean data teaches you nothing.")
    w("-- ===========================================================\n")
    w("BEGIN;\n")
    w("TRUNCATE marketing_touches, leads, audit_runs, deal_stage_history, deals, reps, companies RESTART IDENTITY CASCADE;\n")

    w("-- companies (some NULL domains, some subsidiaries)")
    # parents must exist before children -> insert parents first (import order matters!)
    ordered = [c for c in companies if c["parent_id"] is None] + \
              [c for c in companies if c["parent_id"] is not None]
    for c in ordered:
        w(f"INSERT INTO companies (id,name,domain,tier,parent_id) VALUES "
          f"({c['id']},{esc(c['name'])},{esc(c['domain'])},{esc(c['tier'])},"
          f"{c['parent_id'] if c['parent_id'] else 'NULL'});")
    w("")

    w("-- reps (varied start dates: new reps are still ramping)")
    for r in reps:
        w(f"INSERT INTO reps (id,name,region,start_date) VALUES "
          f"({r['id']},{esc(r['name'])},{esc(r['region'])},{esc(r['start_date'])});")
    w("")

    w("-- deals (some NULL rep_id, ~22% never audited)")
    for d in deals:
        w(f"INSERT INTO deals (id,company_id,rep_id,name,amount,stage,deal_type,"
          f"created_at,close_date,risk_score,audited_at,note_count) VALUES "
          f"({d['id']},{d['company_id']},{d['rep_id'] or 'NULL'},{esc(d['name'])},"
          f"{d['amount']},{esc(d['stage'])},{esc(d['deal_type'])},{esc(d['created_at'])},"
          f"{esc(d['close_date'])},{d['risk_score'] if d['risk_score'] else 'NULL'},"
          f"{esc(d['audited_at'])},{d['note_count']});")
    w("")

    w("-- stage history (~8% of deals have NO rows here at all)")
    for h in history:
        w(f"INSERT INTO deal_stage_history (id,deal_id,stage,changed_at) VALUES "
          f"({h['id']},{h['deal_id']},{esc(h['stage'])},{esc(h['changed_at'])});")
    w("")

    w("-- audit_runs (the AI pipeline's own telemetry)")
    for a in audits:
        w(f"INSERT INTO audit_runs (id,deal_id,idempotency_key,company,status,"
          f"validation_error,risk_score,red_flag_count,note_count,model,prompt_version,"
          f"input_tokens,output_tokens,cost_usd,latency_ms,created_at) VALUES "
          f"({a['id']},{a['deal_id']},{esc(a['idem_key'])},{esc(a['company'])},"
          f"{esc(a['status'])},{esc(a['validation_error'])},"
          f"{a['risk_score'] if a['risk_score'] is not None else 'NULL'},"
          f"{a['red_flag_count'] if a['red_flag_count'] is not None else 'NULL'},"
          f"{a['note_count']},{esc(a['model'])},{esc(a['prompt_version'])},"
          f"{a['input_tokens']},{a['output_tokens']},{a['cost_usd']},{a['latency_ms']},"
          f"{esc(a['created_at'])});")
    w("")

    w("-- leads (orphans, typo'd domains, and 30 duplicates differing only by case/whitespace)")
    for l in leads:
        w(f"INSERT INTO leads (id,email,full_name,company_id,utm_source,created_at) VALUES "
          f"({l['id']},{esc(l['email'])},{esc(l['full_name'])},"
          f"{l['company_id'] if l['company_id'] else 'NULL'},{esc(l['utm_source'])},"
          f"{esc(l['created_at'])});")
    w("")

    w("-- marketing touches (multi-touch journeys, for attribution)")
    for t in touches:
        w(f"INSERT INTO marketing_touches (id,lead_id,deal_id,campaign,channel,touched_at) VALUES "
          f"({t['id']},{t['lead_id']},{t['deal_id'] if t['deal_id'] else 'NULL'},"
          f"{esc(t['campaign'])},{esc(t['channel'])},{esc(t['touched_at'])});")
    w("")

    w("COMMIT;")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    sql = emit()
    with open("seed.sql", "w") as f:
        f.write(sql)
    print(f"Wrote seed.sql ({len(sql):,} chars)")

    if args.stats:
        no_hist = len({d["id"] for d in deals}) - len({h["deal_id"] for h in history})
        no_rep = sum(1 for d in deals if d["rep_id"] is None)
        no_dom = sum(1 for c in companies if c["domain"] is None)
        never_aud = sum(1 for d in deals if d["audited_at"] is None)
        closed = [d for d in deals if d["close_date"]]
        won = sum(1 for d in closed if d["stage"] == "Closed Won")
        fails = sum(1 for a in audits if a["status"] != "success")
        contra = sum(1 for a in audits
                     if a["risk_score"] and a["risk_score"] >= 7 and a["red_flag_count"] == 0)
        print(f"""
─── seeded ───────────────────────────────────────
  companies ............ {len(companies)}   ({no_dom} with NULL domain)
  reps ................. {len(reps)}
  deals ................ {len(deals)}   ({no_rep} with NULL rep_id)
  stage history rows ... {len(history)}   ({no_hist} deals have NO history)
  audit runs ........... {len(audits)}   ({fails} schema violations)
  leads ................ {len(leads)}   (30 are duplicates by case/whitespace)
  marketing touches .... {len(touches)}

─── traps you should be able to find ─────────────
  deals never audited .......... {never_aud}   (the coverage gap)
  closed deals ................. {len(closed)}   ({won} won / {len(closed)-won} lost)
  high risk + ZERO flags ....... {contra}   (the model bullshitting)
─────────────────────────────────────────────────
""")
