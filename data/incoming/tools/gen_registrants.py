#!/usr/bin/env python3
"""Regenerate the 4 Darwinbox v2 fixtures (seeded, deterministic): registrants.csv,
segments.json, hubspot_existing.json, engagement.json. Stdlib only."""
import csv
import json
import random
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20260813
REPO_ROOT = Path(__file__).resolve().parents[3]
CSV_OUT = REPO_ROOT / "data" / "incoming" / "registrants.csv"
SEGMENTS_OUT = REPO_ROOT / "data" / "fixtures" / "segments.json"
HUBSPOT_OUT = REPO_ROOT / "data" / "fixtures" / "hubspot_existing.json"
ENGAGEMENT_OUT = REPO_ROOT / "data" / "fixtures" / "engagement.json"

HEADER = ("First Name,Last Name,Email,Job Title,Company,Country/Region,"
          "Registration Time,Attended,Time in Session (minutes)")

SPEAKER_EMAILS = ["kai8karma+speaker-qhamirani@gmail.com", "kai8karma+speaker-sudikorba@gmail.com"]

# --- company registry: (name, domain, country_code), packed "name|domain|cc;..." per line
def _co(block):
    return [tuple(t.split("|")) for line in block.strip().splitlines() for t in line.split(";")]


IN_CO = _co("""
Infosys|infosys.com|IN;HCLTech|hcltech.com|IN;Tech Mahindra|techmahindra.com|IN;Mphasis|mphasis.com|IN
Persistent Systems|persistent.com|IN;HDFC Bank|hdfcbank.com|IN;ICICI Bank|icicibank.com|IN;Axis Bank|axisbank.com|IN
Bajaj Finserv|bajajfinserv.in|IN;HDFC Life|hdfclife.com|IN;Dr. Reddy's Laboratories|drreddys.com|IN;Sun Pharma|sunpharma.com|IN
Cipla|cipla.com|IN;Biocon|biocon.com|IN;Tata Steel|tatasteel.com|IN;Mahindra & Mahindra|mahindra.com|IN
Asian Paints|asianpaints.com|IN;Havells|havells.com|IN;Titan Company|titan.co.in|IN;Reliance Retail|ril.com|IN
Nykaa|nykaa.com|IN;Marico|marico.com|IN;Dabur|dabur.com|IN;Godrej Consumer Products|godrejcp.com|IN
Zomato|zomato.com|IN;Swiggy|swiggy.com|IN;Flipkart|flipkart.com|IN;PhonePe|phonepe.com|IN
Razorpay|razorpay.com|IN;Freshworks|freshworks.com|IN;Indian Hotels Company|ihcltata.com|IN;OYO|oyorooms.com|IN
Apollo Hospitals|apollohospitals.com|IN;Narayana Health|narayanahealth.org|IN;Lenskart|lenskart.com|IN;Delhivery|delhivery.com|IN
""")
SEA_CO = _co("""
Grab|grab.com|SG;GoTo|gotocompany.com|ID;Sea Limited|sea.com|SG;DBS Bank|dbs.com|SG
OCBC|ocbc.com|SG;Maybank|maybank.com|MY;Axiata|axiata.com|MY;Petronas|petronas.com|MY
Bank Mandiri|bankmandiri.co.id|ID;Telkom Indonesia|telkom.co.id|ID;Ayala Corporation|ayala.com|PH;Jollibee Foods|jollibee.com.ph|PH
Globe Telecom|globe.com.ph|PH;Central Group|centralgroup.com|TH;SCG|scg.com|TH;AirAsia|airasia.com|MY
Ninja Van|ninjavan.co|SG;FPT Corporation|fpt.com|VN;Vinamilk|vinamilk.com.vn|VN
""")
MENA_CO = _co("""
Emirates Group|emirates.com|AE;e&|eand.com|AE;Emaar Properties|emaar.com|AE;Majid Al Futtaim|majidalfuttaim.com|AE
Al-Futtaim Group|alfuttaim.com|AE;Chalhoub Group|chalhoubgroup.com|AE;Landmark Group|landmarkgroup.com|AE;Careem|careem.com|AE
noon|noon.com|AE;Talabat|talabat.com|AE;Saudi Aramco|aramco.com|SA;stc|stc.com.sa|SA
Almarai|almarai.com|SA;Qatar Airways|qatarairways.com|QA;DAMAC Properties|damacproperties.com|AE
""")
USUK_CO = _co("""
HighLevel|gohighlevel.com|US;Toast|toasttab.com|US;Datadog|datadoghq.com|US;Chewy|chewy.com|US
Wayfair|wayfair.com|US;Sweetgreen|sweetgreen.com|US;Planet Fitness|planetfitness.com|US;Carvana|carvana.com|US
Ocado|ocado.com|UK;Deliveroo|deliveroo.co.uk|UK
""")
REGION_BUCKETS = {"IN": IN_CO, "SEA": SEA_CO, "MENA": MENA_CO, "USUK": USUK_CO}
REGION_WEIGHTS = [45, 20, 20, 15]
REAL_DOMAINS = {d for lst in REGION_BUCKETS.values() for _, d, _ in lst}
HOT_PICKS = [("Infosys", "infosys.com", "IN"), ("HDFC Bank", "hdfcbank.com", "IN"),
             ("Grab", "grab.com", "SG"), ("Emirates Group", "emirates.com", "AE")]

INTERNAL = [("Darwinbox", "darwinbox.com", "IN")]
COMPETITORS = [("Keka", "keka.com", "IN"), ("greytHR", "greythr.com", "IN"),
               ("PeopleStrong", "peoplestrong.com", "IN"), ("Rippling", "rippling.com", "US")]
COMPETITOR_COUNTS = [2, 1, 1, 1]
CONSULTANTS = [("Deloitte", "deloitte.com", "IN"), ("EY", "ey.com", "IN")]
STUDENTS = [("IIM Bangalore", "iimb.ac.in", "IN"), ("XLRI", "xlri.ac.in", "IN"), ("NMIMS", "nmims.edu", "IN")]
FREEMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"]
NOISE_DOMAINS = {d for _, d, _ in INTERNAL + COMPETITORS + CONSULTANTS + STUDENTS}

# --- name pools: tag -> ("first,first,...", "last,last,...") --------------
_RAW_POOLS = {
    "IN": ("Amit,Priya,Rahul,Sneha,Vikram,Anjali,Rohan,Neha,Arjun,Divya,Karan,Pooja,Sanjay,Meera,Ajay,Nisha,Deepak,Kavya,Suresh,Ritu",
           "Sharma,Verma,Gupta,Nair,Iyer,Menon,Reddy,Rao,Kapoor,Malhotra,Mukherjee,Joshi,Kulkarni,Patel,Shah,Pillai,Krishnan,Bose,Desai,Chatterjee"),
    "MY": ("Ahmad,Siti,Muhammad,Nurul,Farid,Aisyah,Hafiz,Zainab,Azman,Fatimah", "Abdullah,Rahman,Ismail,Yusof,Hassan,Ibrahim,Osman,Aziz"),
    "ID": ("Budi,Siti,Agus,Dewi,Eko,Rina,Andi,Sri,Bambang,Wati", "Wijaya,Santoso,Kusuma,Hidayat,Pratama,Setiawan,Susanto,Gunawan"),
    "PH": ("Juan,Maria,Jose,Ana,Miguel,Rosa,Carlos,Elena,Antonio,Grace", "Santos,Reyes,Cruz,Bautista,Garcia,Mendoza,Torres,Flores"),
    "TH": ("Somchai,Siriporn,Anan,Suda,Prasert,Naree,Chai,Malee", "Srisuk,Chaiyaporn,Boonmee,Saetang,Rattanakorn,Wongsa"),
    "VN": ("Minh,Linh,Huy,Mai,Anh,Duc,Thao,Nam", "Nguyen,Tran,Le,Pham,Hoang,Vu,Do,Bui"),
    "MENA": ("Mohammed,Fatima,Ahmed,Aisha,Khalid,Layla,Omar,Noura,Yousef,Maryam", "Al-Farsi,Al-Sayed,Al-Mansouri,Al-Otaibi,Al-Harthi,Al-Qahtani,Al-Zaabi,Al-Suwaidi"),
    "WEST": ("James,Emily,Michael,Sarah,David,Jessica,Daniel,Amanda,Robert,Laura", "Smith,Johnson,Williams,Brown,Jones,Miller,Davis,Wilson,Anderson,Taylor"),
}
NAME_POOLS = {k: (f.split(","), l.split(",")) for k, (f, l) in _RAW_POOLS.items()}
COUNTRY_NAMES = {"IN": "India", "SG": "Singapore", "ID": "Indonesia", "MY": "Malaysia", "PH": "Philippines",
                 "TH": "Thailand", "VN": "Vietnam", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
                 "QA": "Qatar", "US": "United States", "UK": "United Kingdom"}
COUNTRY_ALIASES = {"AE": "UAE"}

HR_TITLES = ["Chief People Officer", "VP Human Resources", "VP People", "Head of People", "Director HR",
             "HR Business Partner", "Senior HRBP", "HR Operations Manager", "HRIS Lead", "HRIS Manager",
             "Head of Talent Acquisition", "TA Manager", "Payroll Manager", "Head of L&D",
             "Compensation & Benefits Manager", "People Analytics Lead", "HR Digital Transformation Lead"]
ADJACENT_TITLES = ["CFO", "COO", "CIO", "IT Director", "Head of Shared Services", "Digital Transformation Head"]
NOISE_TITLES = ["Consultant", "Student", "Founder", "Sales Manager", "Account Executive"]
INTERNAL_TITLES = ["Product Manager", "Customer Success Manager", "Sales Development Rep", "QA Engineer"]
SALES_TITLES = ["Account Executive", "Sales Manager", "Business Development Manager", "Regional Sales Director"]
CONSULTANT_TITLES = ["Consultant", "Senior Consultant", "Manager, HR Advisory"]
STUDENT_TITLES = ["Student", "MBA Student"]
PROMOTIONS = {"HR Business Partner": "Senior HR Business Partner", "Senior HRBP": "Head of People",
              "HR Operations Manager": "Director HR", "TA Manager": "Head of Talent Acquisition",
              "HRIS Lead": "HRIS Manager", "Payroll Manager": "Head of L&D",
              "Director HR": "VP Human Resources", "VP People": "Chief People Officer"}
LIFECYCLE_STAGES = ["subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead", "opportunity"]
LEAD_STATUSES = ["NEW", "OPEN", "IN_PROGRESS", "CONNECTED", "UNQUALIFIED"]

EVENT_ASSETS = {
    "click": ["cta-book-demo", "cta-watch-recording"],
    "pageview": ["/blog/start-and-scale-ai-in-hr", "/resources/webinar-how-to-start-and-scale-ai-in-hr",
                 "/product/darwinbox-cortex"],
    "form_fill": ["contact-sales-form", "pricing-request-form"],
}
EVENT_START = datetime(2026, 8, 13, 11, 0, 0)
EVENT_END = datetime(2026, 9, 12, 23, 0, 0)
DECAY_WEIGHTS = [10] * 4 + [4] * 7 + [2] * 10 + [1] * 10  # offsets 0..30

def clean(s):
    return "".join(c for c in s.lower() if c.isalpha())

def make_email(rng, first, last, domain):
    f, l = clean(first), clean(last)
    pattern = rng.choice(["first.last", "flast", "first", "firstl"])
    if pattern == "first.last":
        local = f"{f}.{l}"
    elif pattern == "flast":
        local = f"{f[:1]}{l}"
    elif pattern == "first":
        local = f
    else:
        local = f"{f}{l[:1]}"
    return f"{local}@{domain}"

CC_TO_POOL = {"MY": "MY", "ID": "ID", "PH": "PH", "TH": "TH", "VN": "VN",
              "AE": "MENA", "SA": "MENA", "QA": "MENA", "US": "WEST", "UK": "WEST"}

def gen_name(rng, cc):
    tag = CC_TO_POOL.get(cc, "IN") if cc != "SG" else rng.choice(["MY", "ID", "PH", "TH", "VN"])
    firsts, lasts = NAME_POOLS[tag]
    return rng.choice(firsts), rng.choice(lasts)

def pick_company(rng):
    region = rng.choices(["IN", "SEA", "MENA", "USUK"], weights=REGION_WEIGHTS)[0]
    name, domain, cc = rng.choice(REGION_BUCKETS[region])
    return name, domain, cc

def pick_title(rng):
    r = rng.random() * 100
    if r < 58:
        return rng.choice(HR_TITLES)
    if r < 73:
        return rng.choice(ADJACENT_TITLES)
    if r < 80:
        return rng.choice(NOISE_TITLES)
    return ""

def country_display(rng, cc):
    r = rng.random()
    full = COUNTRY_NAMES.get(cc, cc)
    if r < 0.55:
        return cc
    if r < 0.70:
        return cc.lower()
    if r < 0.85:
        return full
    if r < 0.95:
        return full.lower()
    return COUNTRY_ALIASES.get(cc, cc)

def company_variant(rng, name):
    r = rng.random()
    if r < 0.72:
        return name
    if r < 0.86:
        return name + " Ltd"
    words = name.split()
    if len(words) >= 2:
        return "".join(w[0] for w in words if w[:1].isalpha()).upper()
    return name.upper()

def typo(rng, s):
    if len(s) < 3:
        return s + "x"
    i = rng.randrange(1, len(s) - 1)
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]

def make_identity(rng, name, domain, cc, title=None, name_cc=None):
    first, last = gen_name(rng, name_cc or cc)
    email = make_email(rng, first, last, domain)
    return {"first": first, "last": last, "email": email,
            "title": pick_title(rng) if title is None else title,
            "company": name, "domain": domain, "cc": cc}

def build_identities(rng):
    identities = []
    hot_ids = []
    for name, domain, cc in HOT_PICKS:
        for _ in range(3):
            ident = make_identity(rng, name, domain, cc)
            identities.append(ident)
            hot_ids.append(ident)
    for _ in range(99):
        name, domain, cc = pick_company(rng)
        identities.append(make_identity(rng, name, domain, cc))
    real_ids = list(identities)  # 111 real identities (used for hubspot/dup pools)

    for _ in range(4):
        identities.append(make_identity(rng, *INTERNAL[0], title=rng.choice(INTERNAL_TITLES)))
    for (name, domain, cc), n in zip(COMPETITORS, COMPETITOR_COUNTS):
        for _ in range(n):
            identities.append(make_identity(rng, name, domain, cc, title=rng.choice(SALES_TITLES)))
    for name, domain, cc in CONSULTANTS:
        for _ in range(2):
            identities.append(make_identity(rng, name, domain, cc, title=rng.choice(CONSULTANT_TITLES)))
    for name, domain, cc in STUDENTS:
        identities.append(make_identity(rng, name, domain, cc, title=rng.choice(STUDENT_TITLES)))
    for _ in range(15):
        _, _, cc = pick_company(rng)
        first, last = gen_name(rng, cc)
        fdomain = rng.choice(FREEMAIL_DOMAINS)
        email = make_email(rng, first, last, fdomain)
        if rng.random() < 0.3:
            local, dom = email.split("@")
            email = f"{local}{rng.randint(1, 99)}@{dom}"
        company = rng.choice(["", "", "TCS", "Reliance", "Self-employed", "Freelance", ""])
        identities.append({"first": first, "last": last, "email": email, "title": pick_title(rng),
                            "company": company, "domain": fdomain, "cc": cc})

    return identities, real_ids, hot_ids

def rand_reg_time(rng):
    start = datetime(2026, 7, 21, 0, 0, 0)
    end = datetime(2026, 8, 13, 8, 55, 0)
    frac = rng.betavariate(2, 1)
    return start + timedelta(seconds=frac * (end - start).total_seconds())

def rand_attendance(rng):
    if rng.random() < 0.5:
        return "Yes", str(round(rng.triangular(5, 46, 34)))
    return "No", ""

def display_row(rng, ident):
    first, last, company = ident["first"], ident["last"], company_variant(rng, ident["company"])
    if rng.random() < 0.15:  # wrong case
        upper = rng.random() < 0.5
        first = first.upper() if upper else first.lower()
        last = last.upper() if upper else last.lower()
        company = company.upper() if upper else company.lower()
    if rng.random() < 0.10:  # stray whitespace
        pad = rng.choice([" ", "  "])
        company = pad + company if rng.random() < 0.5 else company + pad
    attended, duration = rand_attendance(rng)
    return {"First Name": first, "Last Name": last, "Email": ident["email"], "Job Title": ident["title"],
            "Company": company, "Country/Region": country_display(rng, ident["cc"]),
            "Registration Time": rand_reg_time(rng).strftime("%Y-%m-%dT%H:%M:%S"),
            "Attended": attended, "Time in Session (minutes)": duration, "_cc": ident["cc"],
            "_domain": ident["domain"]}

def build_dup_row(rng, ident):
    kind = rng.choice(["case", "personal_email", "name_variant"])
    row = display_row(rng, ident)
    if kind == "case":
        local, dom = ident["email"].split("@")
        row["Email"] = f"{local.upper()}@{dom.upper()}" if rng.random() < 0.5 else f"{local.upper()}@{dom}"
    elif kind == "personal_email":
        fdomain = rng.choice(FREEMAIL_DOMAINS)
        row["Email"] = make_email(rng, ident["first"], ident["last"], fdomain)
        row["_domain"] = fdomain
    else:
        if rng.random() < 0.5:
            row["First Name"] = typo(rng, ident["first"])
        else:
            row["Last Name"] = typo(rng, ident["last"])
    return row, kind

def build_registrants(rng):
    identities, real_ids, hot_ids = build_identities(rng)
    rows = [display_row(rng, ident) for ident in identities]

    non_hot_real = [i for i in real_ids if i is not None and i not in hot_ids]
    dup_sources = rng.sample(non_hot_real, 8)
    dup_pairs = []
    for ident in dup_sources:
        dup_row, kind = build_dup_row(rng, ident)
        rows.append(dup_row)
        dup_pairs.append((ident["email"], dup_row["Email"], kind))

    real_row_idx = [i for i, r in enumerate(rows) if r["_domain"] in REAL_DOMAINS]
    for idx in rng.sample(real_row_idx, 2):
        rows[idx]["First Name"], rows[idx]["Last Name"] = rows[idx]["Last Name"], rows[idx]["First Name"]

    # force exactly 3 "N/A" title rows (distinct from the blank-title bucket)
    na_candidates = [i for i, r in enumerate(rows) if r["_domain"] in REAL_DOMAINS]
    for idx in rng.sample(na_candidates, 3):
        rows[idx]["Job Title"] = "N/A"

    rng.shuffle(rows)
    return rows, dup_pairs, hot_ids, real_ids

def write_csv(rows):
    fields = ["First Name", "Last Name", "Email", "Job Title", "Company", "Country/Region",
              "Registration Time", "Attended", "Time in Session (minutes)"]
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)

def build_segments(rows):
    attendees, no_shows = set(), set()
    for r in rows:
        email = r["Email"].lower()
        if r["Attended"] == "Yes":
            attendees.add(email)
        elif email not in attendees:
            no_shows.add(email)
    no_shows -= attendees
    return {
        "_note": "speakers use kai8karma+speaker-* demo-routed aliases, not real panelist inboxes",
        "attendees": sorted(attendees), "no_shows": sorted(no_shows), "speakers": SPEAKER_EMAILS,
    }

def build_hubspot(rng, real_ids, csv_emails):
    pool = rng.sample(real_ids, 28)
    groups = [("exact", pool[0:10]), ("diff_email", pool[10:18]), ("drift", pool[18:24]), ("typo", pool[24:28])]
    records, overlap = [], 0
    vid = 100001
    for kind, idents in groups:
        for ident in idents:
            first, last, title, company, email = ident["first"], ident["last"], ident["title"], ident["company"], ident["email"]
            if kind == "diff_email":
                new = make_email(rng, first, last, ident["domain"])
                while new == email:
                    new = make_email(rng, first, last, ident["domain"])
                email = new
            elif kind == "drift":
                if rng.random() < 0.5:
                    title = PROMOTIONS.get(title, "VP Human Resources" if title else "Head of People")
                else:
                    company = company_variant(rng, company)
            elif kind == "typo":
                if rng.random() < 0.5:
                    first = typo(rng, first)
                else:
                    last = typo(rng, last)
            records.append({"vid": vid, "email": email, "firstname": first, "lastname": last,
                             "jobtitle": title or "HR Manager", "company": company,
                             "lifecyclestage": rng.choice(LIFECYCLE_STAGES),
                             "hs_lead_status": rng.choice(LEAD_STATUSES)})
            overlap += 1
            vid += 1

    for _ in range(12):
        name, domain, cc = pick_company(rng)
        first, last = gen_name(rng, cc)
        email = make_email(rng, first, last, domain)
        while email in csv_emails:
            first, last = gen_name(rng, cc)
            email = make_email(rng, first, last, domain)
        records.append({"vid": vid, "email": email, "firstname": first, "lastname": last,
                         "jobtitle": rng.choice(HR_TITLES), "company": name,
                         "lifecyclestage": rng.choice(LIFECYCLE_STAGES),
                         "hs_lead_status": rng.choice(LEAD_STATUSES)})
        vid += 1
    return records, overlap

def build_engagement(rng, segments, hot_ids):
    attendees, no_shows = list(segments["attendees"]), list(segments["no_shows"])
    att_sample = set(rng.sample(attendees, round(len(attendees) * 0.7)))
    ns_sample = set(rng.sample(no_shows, round(len(no_shows) * 0.35)))

    hot_emails = [i["email"].lower() for i in hot_ids]
    for e in hot_emails:
        (att_sample if e in segments["attendees"] else ns_sample).add(e)
    remaining_pool = [e for e in attendees + no_shows if e not in hot_emails]
    anomaly_contacts = rng.sample(remaining_pool, 3)
    for e in anomaly_contacts:
        (att_sample if e in segments["attendees"] else ns_sample).add(e)

    def is_att(email):
        return email in segments["attendees"]

    def pick_event(email):
        t = rng.choices(["open", "click", "pageview", "form_fill"], weights=[35, 30, 20, 15])[0]
        if t == "open":
            asset = "thank-you-email" if is_att(email) else "no-show-catchup-email"
        else:
            asset = rng.choice(EVENT_ASSETS[t])
        return t, asset

    def rand_ts(offset=None):
        off = offset if offset is not None else rng.choices(range(31), weights=DECAY_WEIGHTS)[0]
        day = EVENT_START + timedelta(days=off)
        ts = day.replace(hour=rng.randint(0, 23), minute=rng.randint(0, 59), second=0)
        return max(EVENT_START, min(EVENT_END, ts))

    events = []
    for email in anomaly_contacts:
        offset = rng.randint(1, 29)
        for _ in range(rng.randint(10, 14)):
            t, asset = pick_event(email)
            events.append({"email": email, "type": t, "ts": rand_ts(offset).strftime("%Y-%m-%dT%H:%M:%S"), "asset": asset})
    for email in hot_emails:
        for _ in range(rng.randint(4, 8)):
            t, asset = pick_event(email)
            events.append({"email": email, "type": t, "ts": rand_ts().strftime("%Y-%m-%dT%H:%M:%S"), "asset": asset})

    general = list((att_sample | ns_sample) - set(anomaly_contacts) - set(hot_emails))
    reserved = len(events)
    remaining = rng.randint(max(0, 350 - reserved), max(0, 420 - reserved))
    weights = [2 if e in hot_emails else 1 for e in general]
    for email in rng.choices(general, weights=weights, k=remaining):
        t, asset = pick_event(email)
        events.append({"email": email, "type": t, "ts": rand_ts().strftime("%Y-%m-%dT%H:%M:%S"), "asset": asset})
    events.sort(key=lambda e: e["ts"])

    by_email = {}
    for e in events:
        by_email.setdefault(e["email"], []).append(e)
    lifecycle_changes = []
    for email, evs in by_email.items():
        n = len(evs)
        steps = min(4, n // 3)
        if steps == 0:
            continue
        first_ts = datetime.strptime(min(x["ts"] for x in evs), "%Y-%m-%dT%H:%M:%S")
        for i in range(steps):
            ts = min(first_ts + timedelta(days=(i + 1) * 3 + rng.randint(0, 2)), EVENT_END)
            lifecycle_changes.append({"email": email, "from": LIFECYCLE_STAGES[i], "to": LIFECYCLE_STAGES[i + 1],
                                       "ts": ts.strftime("%Y-%m-%dT%H:%M:%S")})

    meta = {"hot_accounts": [name.lower() for name, _, _ in HOT_PICKS], "anomaly_contacts": anomaly_contacts}
    return {"events": events, "lifecycle_changes": lifecycle_changes, "_meta": meta}

def verify(rows, dup_pairs, hubspot, hs_overlap, engagement, segments):
    assert len(rows) == 150, f"expected 150 rows, got {len(rows)}"
    with open(CSV_OUT) as f:
        first_line = f.readline().rstrip("\n")
    assert first_line == HEADER, "header drifted from v1"
    try:
        v1_header = subprocess.run(["git", "show", "HEAD:data/incoming/registrants.csv"], cwd=REPO_ROOT,
                                    capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
        assert first_line == v1_header, "header no longer matches git HEAD v1 copy"
    except (IndexError, subprocess.SubprocessError):
        pass

    non_blank = [r for r in rows if r["Job Title"] not in ("", "N/A")]
    hr_count = sum(1 for r in non_blank if r["Job Title"] in HR_TITLES)
    hr_pct = hr_count / len(non_blank)
    assert hr_pct >= 0.55, f"HR title share {hr_pct:.2%} below 55%"

    assert len(dup_pairs) == 8, f"expected 8 dup pairs, got {len(dup_pairs)}"

    bad_domains = []
    for r in rows:
        dom = r["Email"].split("@")[-1].lower()
        if dom in FREEMAIL_DOMAINS or dom in NOISE_DOMAINS:
            continue
        if dom not in REAL_DOMAINS:
            bad_domains.append(dom)
    assert not bad_domains, f"invented domains found: {bad_domains}"

    assert len(segments["attendees"]) + len(segments["no_shows"]) == len({r["Email"].lower() for r in rows})

    assert len(hubspot) == 40, f"expected 40 hubspot records, got {len(hubspot)}"
    assert hs_overlap == 28, f"expected 28 registrant overlap, got {hs_overlap}"

    n_events = len(engagement["events"])
    assert 350 <= n_events <= 420, f"engagement event count {n_events} out of range"

    domains = Counter(r["Email"].split("@")[-1].lower() for r in rows)
    attended_count = sum(1 for r in rows if r["Attended"] == "Yes")
    freemail_count = sum(1 for r in rows if r["Email"].split("@")[-1].lower() in FREEMAIL_DOMAINS)
    region_split = Counter(r["_cc"] for r in rows)

    print(f"rows: {len(rows)}")
    print(f"attended: {attended_count}")
    print(f"unique domains: {len(domains)}")
    print(f"dup pairs: {len(dup_pairs)}")
    print(f"freemail rows: {freemail_count}")
    print(f"region split (by country code): {dict(region_split)}")
    print(f"hubspot records: {len(hubspot)}, registrant overlap: {hs_overlap}")
    print(f"engagement events: {n_events}")

def main():
    rng = random.Random(SEED)
    rows, dup_pairs, hot_ids, real_ids = build_registrants(rng)
    write_csv(rows)

    csv_emails = {r["Email"].lower() for r in rows}
    segments = build_segments(rows)
    with open(SEGMENTS_OUT, "w") as f:
        json.dump(segments, f, indent=2)

    hubspot, hs_overlap = build_hubspot(rng, real_ids, csv_emails)
    with open(HUBSPOT_OUT, "w") as f:
        json.dump(hubspot, f, indent=2)

    engagement = build_engagement(rng, segments, hot_ids)
    with open(ENGAGEMENT_OUT, "w") as f:
        json.dump(engagement, f, indent=2)

    verify(rows, dup_pairs, hubspot, hs_overlap, engagement, segments)

if __name__ == "__main__":
    main()
