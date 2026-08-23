#!/usr/bin/env python3
"""M4 -- Lead Intelligence Dashboard builder.

Reads M1's enriched HubSpot-ready CSV plus engagement.json and segments.json,
computes the attendee-to-MQL funnel, top engaged accounts/contacts, a buying-
committee map, 7/14/30-day lifecycle movement, and two anomaly callouts.
Embeds the computed data as a JSON block into template.html and writes the
result as index.html in --out.

Python 3 stdlib only. Zero network calls.

Usage:
    python3 build_dashboard.py --enriched hubspot_ready.csv \
        --engagement engagement.json --segments segments.json --out dist/
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent

# --- fixed identities (shared across all build units, do not derive) ---
EVENT_NAME = "Pipeline After the Webinar: Turning Event Engagement into Revenue"
EVENT_DATE = "2026-08-19"
HOST_COMPANY = "ACME Revenue Cloud"
HOST_DOMAIN = "acmerevenue.example"
SPEAKERS = [
    {"name": "Priya Nair", "title": "VP Marketing", "company": "ACME Revenue Cloud (host)"},
    {"name": "Daniel Kim", "title": "Head of Demand Gen", "company": "Northwind Analytics"},
    {"name": "Sara Alvarez", "title": "RevOps Lead", "company": "Meridian Software"},
]

WEIGHTS = {"form_fill": 10, "click": 3, "pageview": 1, "open": 0.5}

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "protonmail.com", "live.com", "msn.com",
}

LIFECYCLE_RANK = {
    "subscriber": 0,
    "lead": 1,
    "marketingqualifiedlead": 2,
    "salesqualifiedlead": 3,
    "opportunity": 4,
    "customer": 5,
}

# M1's lifecycle_default (config/icp.yaml) can carry a "_pending"/"_new"
# suffix (e.g. "marketingqualifiedlead_pending") for stages awaiting SDR
# review. M1 itself strips these before writing hubspot_ready.csv, but
# normalize defensively here too so a stage carrying either suffix still
# ranks correctly instead of silently falling to -1 (unranked).
LIFECYCLE_STAGE_SUFFIXES = ("_pending", "_new")


def normalize_lifecycle_stage(stage):
    stage = (stage or "").strip().lower()
    for suffix in LIFECYCLE_STAGE_SUFFIXES:
        if stage.endswith(suffix):
            return stage[: -len(suffix)]
    return stage

# Column-name aliases so this reads whatever reasonable header M1 ships,
# without the two modules needing to agree on exact spelling in advance.
FIELD_ALIASES = {
    "email": ["email"],
    "firstname": ["firstname", "first_name", "first name"],
    "lastname": ["lastname", "last_name", "last name"],
    "jobtitle": ["jobtitle", "job_title", "title", "job title"],
    "company": ["company", "company_name", "company name"],
    "country": ["country", "country_region", "country/region", "country_code"],
    "region": ["region"],
    "industry": ["industry"],
    "icp_tier": ["icp_tier", "tier", "icp tier"],
    "lifecyclestage": ["lifecyclestage", "lifecycle_stage", "lifecycle stage"],
    "owner": ["owner", "owner_email", "hubspot_owner", "sdr_owner"],
    "attendance_status": ["attendance_status", "attendance", "attended"],
}

# M1 (modules/m1-enrichment/enrich.py) reports completeness per-field, not
# per-row -- there is no "contact_completeness_pct" / "company_completeness_pct"
# column on hubspot_ready.csv. Its real field list (quality_report.json's
# "fields" dict) split into contact-level vs company-level attributes, so the
# dashboard's two completeness KPIs can be derived from it instead of reading
# columns that don't exist.
CONTACT_COMPLETENESS_FIELDS = {
    "email", "firstname", "lastname", "jobtitle", "function", "seniority",
    "country", "region", "hubspot_owner_email", "lifecyclestage", "icp_tier",
}
COMPANY_COMPLETENESS_FIELDS = {"company", "industry", "numemployees"}


def normalize_row(raw_row):
    lower = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
    out = {}
    for field, aliases in FIELD_ALIASES.items():
        val = ""
        for alias in aliases:
            if lower.get(alias):
                val = lower[alias]
                break
        out[field] = val
    return out


def load_enriched(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            norm = normalize_row(r)
            if norm.get("email"):
                norm["email"] = norm["email"].lower()
                rows.append(norm)
    return rows


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_quality_report(enriched_path):
    """M1 writes quality_report.json alongside hubspot_ready.csv in the same
    --out dir (orchestrator contract: both come from one M1 run). Missing or
    malformed report -> None, so completeness KPIs fall back to null/"--"
    instead of crashing the build.
    """
    path = Path(enriched_path).parent / "quality_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def avg_completeness(quality_report, field_names):
    if not quality_report:
        return None
    fields = quality_report.get("fields", {})
    vals = [fields[f] for f in field_names if f in fields]
    return round(sum(vals) / len(vals), 1) if vals else None


def domain_of(email):
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def account_key(email):
    d = domain_of(email)
    if not d or d in FREEMAIL_DOMAINS:
        return None
    return d


def display_company(domain):
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def parse_ts(ts):
    return datetime.fromisoformat(ts)


def load_fallback_narrative():
    """Read fallback_narrative.md sitting next to this script.

    Expects a small frontmatter block:
        <!-- generated_at: 2026-08-21T16:00:00+05:30 -->
    followed by the narrative paragraphs.
    """
    path = MODULE_DIR / "fallback_narrative.md"
    text = path.read_text(encoding="utf-8")
    generated_at = None
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("<!-- generated_at:") and stripped.endswith("-->"):
            generated_at = stripped[len("<!-- generated_at:"):-len("-->")].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    return {
        "generated_at": generated_at or datetime.now().isoformat(),
        "paragraphs": paragraphs,
    }


def build(enriched_path, engagement_path, segments_path):
    enriched_rows = load_enriched(enriched_path)
    engagement = load_json(engagement_path)
    segments = load_json(segments_path)
    quality_report = load_quality_report(enriched_path)

    contacts = {r["email"]: r for r in enriched_rows}

    # Canonical basis: M1's deduped rows are authoritative for who attended.
    # segments.json still lists pre-dedupe email variants, so counting it raw
    # inflates the funnel denominator against a deduped numerator.
    alias = {}
    dedupe_path = Path(enriched_path).parent / "dedupe_report.json"
    if dedupe_path.exists():
        for pair in load_json(dedupe_path).get("within_batch_duplicates", []):
            dup, primary = pair.get("duplicate_email"), pair.get("primary_email")
            if dup and primary:
                alias[dup.lower()] = primary.lower()

    def canon(email):
        return alias.get(email, email)

    if enriched_rows and "attendance_status" in enriched_rows[0]:
        attendees = {r["email"].lower() for r in enriched_rows if r.get("attendance_status") == "attended"}
        no_shows = {r["email"].lower() for r in enriched_rows if r.get("attendance_status") == "no_show"}
    else:
        attendees = {canon(e.lower()) for e in segments.get("attendees", [])}
        no_shows = {canon(e.lower()) for e in segments.get("no_shows", [])} - attendees
    speaker_emails = {canon(e.lower()) for e in segments.get("speakers", [])}

    events = engagement.get("events", [])
    lifecycle_changes = [
        {**c, "email": canon(c["email"].lower())}
        for c in engagement.get("lifecycle_changes", [])
        if c.get("email") and c.get("ts")
    ]

    # ---------------- engagement scores ----------------
    scores = defaultdict(float)
    events_by_contact = defaultdict(list)
    for e in events:
        email = canon((e.get("email") or "").lower())
        if not email:
            continue
        scores[email] += WEIGHTS.get(e.get("type"), 0)
        events_by_contact[email].append(e)

    engaged_emails = {e for e, s in scores.items() if s > 0}

    # ---------------- funnel ----------------
    total_registrants = max(len(contacts), len(attendees | no_shows))
    total_attendees = len(attendees)
    engaged_attendees = engaged_emails & attendees

    def stage_rank(email):
        stage = normalize_lifecycle_stage(contacts.get(email, {}).get("lifecyclestage", ""))
        return LIFECYCLE_RANK.get(stage, -1)

    mql_plus = {e for e in attendees if stage_rank(e) >= LIFECYCLE_RANK["marketingqualifiedlead"]}
    sql_plus = {e for e in attendees if stage_rank(e) >= LIFECYCLE_RANK["salesqualifiedlead"]}

    funnel = [
        {"stage": "Registrants", "count": total_registrants},
        {"stage": "Attendees", "count": total_attendees},
        {"stage": "Engaged Post-Event", "count": len(engaged_attendees)},
        {"stage": "MQL+", "count": len(mql_plus)},
        {"stage": "SQL+", "count": len(sql_plus)},
    ]
    attendee_to_mql_pct = round(100 * len(mql_plus) / total_attendees, 1) if total_attendees else 0.0

    # ---------------- accounts / buying committee ----------------
    account_roster = defaultdict(set)  # all known contacts per account (registrant roster)
    for email in contacts:
        ak = account_key(email)
        if ak:
            account_roster[ak].add(email)
    for email in attendees | no_shows:
        ak = account_key(email)
        if ak:
            account_roster[ak].add(email)

    account_scores = defaultdict(float)
    account_engaged = defaultdict(set)
    for email, sc in scores.items():
        ak = account_key(email)
        if ak:
            account_scores[ak] += sc
            account_engaged[ak].add(email)

    def contact_display(email):
        row = contacts.get(email, {})
        name = f"{row.get('firstname', '').strip()} {row.get('lastname', '').strip()}".strip()
        if not name:
            name = email.split("@")[0].replace(".", " ").title()
        return {
            "email": email,
            "name": name,
            "title": row.get("jobtitle") or "Unknown",
            "lifecyclestage": row.get("lifecyclestage") or "unknown",
            "score": round(scores.get(email, 0.0), 1),
        }

    def company_name_for(ak):
        for email in account_roster.get(ak, ()):
            c = contacts.get(email, {}).get("company")
            if c:
                return c
        return display_company(ak)

    def account_record(ak):
        engaged = account_engaged.get(ak, set())
        roster = account_roster.get(ak, set()) | engaged
        contacts_list = sorted((contact_display(e) for e in engaged), key=lambda c: -c["score"])
        return {
            "account": company_name_for(ak),
            "domain": ak,
            "score": round(account_scores.get(ak, 0.0), 1),
            "engaged_contact_count": len(engaged),
            "total_known_contact_count": len(roster),
            "committee_coverage_pct": round(100 * len(engaged) / len(roster), 1) if roster else 0.0,
            "is_buying_committee": len(engaged) >= 3,
            "contacts": contacts_list[:6],
        }

    all_account_keys = set(account_scores) | set(account_roster)
    ranked_by_score = sorted(all_account_keys, key=lambda ak: -account_scores.get(ak, 0.0))
    top_accounts = [account_record(ak) for ak in ranked_by_score[:10]]

    committee_accounts = sorted(
        (account_record(ak) for ak in all_account_keys if len(account_engaged.get(ak, ())) >= 3),
        key=lambda a: (-a["engaged_contact_count"], -a["score"]),
    )[:10]

    top_contacts = sorted(
        (contact_display(e) for e in engaged_emails), key=lambda c: -c["score"]
    )[:10]

    # ---------------- lifecycle movement 7/14/30d ----------------
    dated_changes = []
    for c in lifecycle_changes:
        try:
            dated_changes.append({**c, "_dt": parse_ts(c["ts"])})
        except ValueError:
            continue

    all_ts = [c["_dt"] for c in dated_changes] + [
        parse_ts(e["ts"]) for e in events if e.get("ts")
    ]
    as_of = max(all_ts) if all_ts else datetime.now()

    def window_stats(days):
        cutoff = as_of - timedelta(days=days)
        window = [c for c in dated_changes if c["_dt"] >= cutoff]
        by_stage = Counter(c["to"] for c in window)
        return {"total": len(window), "by_stage": dict(by_stage)}

    movement = {
        "7d": window_stats(7),
        "14d": window_stats(14),
        "30d": window_stats(30),
    }

    daily_counts = Counter(c["_dt"].date().isoformat() for c in dated_changes)
    movement_timeline = [{"date": d, "count": n} for d, n in sorted(daily_counts.items())]

    # ---------------- anomaly detection ----------------
    # Rule: contacts with >=15 total engagement events whose activity is
    # heavily concentrated on a single calendar day -- either a buying-
    # committee research sprint, or a hot lead the lifecycle engine never
    # re-scored. Ranked by raw same-day event count; top 2 reported.
    candidates = []
    for email, evs in events_by_contact.items():
        if len(evs) < 15:
            continue
        day_counts = Counter(e["ts"][:10] for e in evs)
        top_day, top_count = day_counts.most_common(1)[0]
        candidates.append((email, len(evs), top_day, top_count))
    candidates.sort(key=lambda r: -r[3])

    anomalies = []
    for email, total, day, count in candidates[:2]:
        progressed = any(c.get("email", "").lower() == email for c in lifecycle_changes)
        day_assets = Counter(e["asset"] for e in events_by_contact[email] if e["ts"][:10] == day)
        top_assets = [a for a, _ in day_assets.most_common(3)]
        ak = account_key(email)
        share = round(100 * count / total, 1)
        headline = (
            f"{count} of {total} touches landed in a single day ({day}) -- a "
            f"compressed research sprint ({share}% of all their activity)."
        )
        if progressed:
            headline += " Lifecycle stage already reflects it."
        else:
            headline += " Lifecycle stage never moved -- a stalled hot lead worth a manual RevOps check."
        anomalies.append({
            "contact": contact_display(email),
            "company": company_name_for(ak) if ak else "(personal email domain)",
            "date": day,
            "events_that_day": count,
            "events_total": total,
            "share_of_activity_pct": share,
            "lifecycle_progressed": progressed,
            "top_assets": top_assets,
            "headline": headline,
        })

    # ---------------- KPIs ----------------
    kpis = {
        "total_registrants": total_registrants,
        "total_attendees": total_attendees,
        "attendance_rate_pct": round(100 * total_attendees / total_registrants, 1) if total_registrants else 0.0,
        "engaged_post_event": len(engaged_attendees),
        "mql_plus": len(mql_plus),
        "sql_plus": len(sql_plus),
        "attendee_to_mql_pct": attendee_to_mql_pct,
        "avg_contact_completeness_pct": avg_completeness(quality_report, CONTACT_COMPLETENESS_FIELDS),
        "avg_company_completeness_pct": avg_completeness(quality_report, COMPANY_COMPLETENESS_FIELDS),
        "buying_committee_accounts": len(committee_accounts),
        "speaker_count": len(speaker_emails),
    }

    data = {
        "generated_at": datetime.now().isoformat(),
        "as_of": as_of.isoformat(),
        "event": {
            "name": EVENT_NAME,
            "date": EVENT_DATE,
            "host_company": HOST_COMPANY,
            "host_domain": HOST_DOMAIN,
            "speakers": SPEAKERS,
        },
        "kpis": kpis,
        "funnel": funnel,
        "top_accounts": top_accounts,
        "top_contacts": top_contacts,
        "committee_accounts": committee_accounts,
        "movement": movement,
        "movement_timeline": movement_timeline,
        "anomalies": anomalies,
        "narrative_fallback": load_fallback_narrative(),
    }
    return data


def render(data, template_path):
    template = template_path.read_text(encoding="utf-8")
    json_blob = json.dumps(data, indent=2).replace("</", "<\\/")
    marker = "__DASHBOARD_DATA_JSON__"
    if marker not in template:
        print(f"error: marker {marker!r} not found in {template_path}", file=sys.stderr)
        sys.exit(1)
    return template.replace(marker, json_blob)


def main():
    ap = argparse.ArgumentParser(description="Build the M4 Lead Intelligence Dashboard.")
    ap.add_argument("--enriched", required=True, help="M1 hubspot_ready.csv")
    ap.add_argument("--engagement", required=True, help="engagement.json")
    ap.add_argument("--segments", required=True, help="segments.json")
    ap.add_argument("--out", required=True, help="output directory")
    args = ap.parse_args()

    for label, p in (("--enriched", args.enriched), ("--engagement", args.engagement), ("--segments", args.segments)):
        if not Path(p).exists():
            print(f"error: {label} path not found: {p}", file=sys.stderr)
            sys.exit(1)

    data = build(args.enriched, args.engagement, args.segments)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render(data, MODULE_DIR / "template.html")
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"wrote {out_path}")
    print(f"  registrants={data['kpis']['total_registrants']} attendees={data['kpis']['total_attendees']} "
          f"engaged={data['kpis']['engaged_post_event']} mql_plus={data['kpis']['mql_plus']} "
          f"attendee_to_mql_pct={data['kpis']['attendee_to_mql_pct']}")
    print(f"  top_accounts={len(data['top_accounts'])} committee_accounts={len(data['committee_accounts'])} "
          f"anomalies={len(data['anomalies'])}")


if __name__ == "__main__":
    main()
