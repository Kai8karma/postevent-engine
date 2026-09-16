#!/usr/bin/env python3
"""M4 -- Lead Intelligence Dashboard, phased.

    python3 dashboard.py <phase> --out <dir> [--event data/incoming/event.json]
        [--event-tag <slug>] [--offline] [--live-dry-run] [--budget N]

    phases: seed | sync | analyze | render | all

The HubSpot portal is the source of truth, not a local file: M1 pushed the event's
contacts (tagged with the `postevent_event` contact property), M2 logged the email
engagements as CRM v3 `emails` objects, and `seed` writes this event's engagement
stream and lifecycle changes INTO HubSpot before `sync` reads anything back.

Lanes
  live (default)   real HubSpot + real OpenRouter calls; every call writes a receipt.
  --offline        zero network. Snapshot is rebuilt from the repo fixtures and every
                   record says so (`source: "fixture"`, lane `offline`); the narrative
                   is templated from the deterministic numbers and labelled `rules`.
  --live-dry-run   builds every live prompt and every HubSpot request from real data,
                   prints them (method, URL, body size), sends nothing, exits 0. It
                   writes only receipts/m4_dry_run.json -- no snapshot, no analysis and
                   no dashboard, because none of those would have come from the portal.

Honesty: the registrant people are synthetic, so the engagement stream `seed` writes
is simulated. Everything that came from it is labelled `source: "seeded"` in the seed
receipt, in the snapshot, in analysis.json and on the rendered page. Nothing here ever
invents a contact, a count or a narrative -- a missing backend is a labelled lane or a
non-zero exit, never a placeholder.

Python 3 stdlib only.
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
PROMPTS_DIR = MODULE_DIR / "prompts"
TEMPLATE_PATH = MODULE_DIR / "template.html"

DEFAULT_EVENT = REPO_ROOT / "data" / "incoming" / "event.json"
FIXTURE_DIR = REPO_ROOT / "data" / "fixtures"
ENGAGEMENT_FIXTURE = FIXTURE_DIR / "engagement.json"
HUBSPOT_FIXTURE = FIXTURE_DIR / "hubspot_existing.json"
SEGMENTS_FIXTURE = FIXTURE_DIR / "segments.json"
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"

PHASES = ("seed", "sync", "analyze", "render", "all")

# Deterministic lead-interest weighting. A product call (a pricing-form fill is worth
# more than an open), not something a model invents per run -- the LLM scores on top of
# these rows and its score is compared against this ranking, never swapped for it.
WEIGHTS = {"form_fill": 10.0, "click": 3.0, "pageview": 1.0, "open": 0.5}

LIFECYCLE_RANK = {
    "subscriber": 0, "lead": 1, "marketingqualifiedlead": 2,
    "salesqualifiedlead": 3, "opportunity": 4, "customer": 5, "evangelist": 6,
}
MQL_RANK = LIFECYCLE_RANK["marketingqualifiedlead"]
LIFECYCLE_STAGE_SUFFIXES = ("_pending", "_new")

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "protonmail.com", "live.com", "msn.com", "rediffmail.com",
    "rediff.com", "ymail.com", "mail.com", "gmx.com", "yandex.com",
}

COMMITTEE_MIN_CONTACTS = 2      # docs/module-api.md M4: >=2 engaged contacts at one company

# ---------------------------------------------------------------------------
# HubSpot wiring
# ---------------------------------------------------------------------------
HUBSPOT_ENV_PATH = Path.home() / ".config" / "postevent" / "hubspot.env"
API_BASE = "https://api.hubapi.com"
PAGE_SIZE = 100
BATCH_CHUNK = 100
EVENT_PROPERTY = "postevent_event"       # written by M1's push_to_hubspot.py
EMAIL_TO_CONTACT_ASSOC_TYPE = 198        # HUBSPOT_DEFINED email->contact (M2 logs with this)

# Counter properties the fallback lane sets. Created with the same body shape and the
# same "409 already exists counts as success" idiom as push_to_hubspot.py.
SEED_PROPERTIES = [
    {"name": "postevent_opens", "label": "Post-Event Opens", "type": "number",
     "fieldType": "number", "groupName": "contactinformation"},
    {"name": "postevent_clicks", "label": "Post-Event Clicks", "type": "number",
     "fieldType": "number", "groupName": "contactinformation"},
    {"name": "postevent_pageviews", "label": "Post-Event Pageviews", "type": "number",
     "fieldType": "number", "groupName": "contactinformation"},
    {"name": "postevent_form_fills", "label": "Post-Event Form Fills", "type": "number",
     "fieldType": "number", "groupName": "contactinformation"},
    {"name": "postevent_last_engaged", "label": "Post-Event Last Engaged", "type": "datetime",
     "fieldType": "date", "groupName": "contactinformation"},
]
COUNTER_PROPERTIES = ["postevent_opens", "postevent_clicks", "postevent_pageviews",
                      "postevent_form_fills", "postevent_last_engaged"]
COUNTER_BY_TYPE = {"open": "postevent_opens", "click": "postevent_clicks",
                   "pageview": "postevent_pageviews", "form_fill": "postevent_form_fills"}

CONTACT_PROPERTIES = [
    "email", "firstname", "lastname", "company", "jobtitle", "country",
    "lifecyclestage", "icp_tier", "attendance_status", EVENT_PROPERTY,
    "hubspot_owner_id",
] + COUNTER_PROPERTIES
COMPANY_PROPERTIES = ["name", "domain"]
EMAIL_PROPERTIES = ["hs_email_subject", "hs_timestamp"]

# ---------------------------------------------------------------------------
# OpenRouter wiring (same loader/receipt idiom as modules/m3-repurpose/repurpose.py)
# ---------------------------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_ENV_PATH = Path.home() / ".config" / "postevent" / "llm.env"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_BUDGET = 4
MAX_PROMPT_CONTACTS = 30
MAX_PROMPT_ACCOUNTS = 15
MAX_PROMPT_TRANSITIONS = 40


class M4Error(RuntimeError):
    """Anything that must fail the phase loudly."""


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False), encoding="utf-8")
    return path


def domain_of(email: str) -> str:
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def account_key(email: str):
    d = domain_of(email)
    return None if (not d or d in FREEMAIL_DOMAINS) else d


def display_company(domain: str) -> str:
    return domain.split(".")[0].replace("-", " ").replace("_", " ").title() if domain else ""


def display_name(contact: dict) -> str:
    name = f"{(contact.get('firstname') or '').strip()} {(contact.get('lastname') or '').strip()}".strip()
    if name:
        return name
    local = (contact.get("email") or "").split("@")[0]
    return local.replace(".", " ").replace("_", " ").title() or contact.get("email", "")


def normalize_stage(stage) -> str:
    stage = (stage or "").strip().lower()
    for suffix in LIFECYCLE_STAGE_SUFFIXES:
        if stage.endswith(suffix):
            return stage[: -len(suffix)]
    return stage


def stage_rank(stage) -> int:
    return LIFECYCLE_RANK.get(normalize_stage(stage), -1)


def parse_ts(ts):
    """Naive-UTC datetime from either fixture ('2026-08-13T11:00:00') or HubSpot
    ('2026-08-13T11:00:00.123Z') timestamps, so the two never get compared as
    aware-vs-naive. Returns None on anything unparseable."""
    if not ts:
        return None
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
        return datetime.utcfromtimestamp(int(ts) / 1000.0)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt) -> str:
    return dt.isoformat(timespec="seconds") if isinstance(dt, datetime) else ""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def load_event(path) -> dict:
    return load_json(path)


def event_identity(event: dict, slug: str) -> dict:
    host = event.get("host_company", "")
    return {
        "name": event.get("event_name", ""),
        "slug": slug,
        "date": event.get("date", ""),
        "host_company": host,
        "host_domain": event.get("host_domain", ""),
        "speakers": [
            {"name": s.get("name", ""), "title": s.get("title", ""),
             "company": s.get("company", "") + (" (host)" if s.get("company") == host else "")}
            for s in event.get("speakers", [])
        ],
    }


_REGION_LINE = re.compile(r"^\s{4}([A-Z]+):\s*\[(.*)\]\s*$")
_OWNER_LINE = re.compile(r'^\s{4}([A-Z]+):\s*"([^"]+)"\s*$')


def load_routing(path=ICP_CONFIG):
    """config/icp.yaml's `regions:` (region -> ISO-2 countries) and `owners:`
    (region -> SDR pod address). Same table M1 routes on, read here so the
    dashboard's region/owner columns cannot drift from M1's."""
    regions, owners = {}, {}
    if not Path(path).exists():
        return regions, owners
    section = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped in ("regions:", "owners:", "owner_map:"):
            section = stripped[:-1]
            continue
        if stripped and not line.startswith("    "):
            section = None
        if section == "regions":
            m = _REGION_LINE.match(line)
            if m:
                regions[m.group(1)] = [c.strip().strip('"') for c in m.group(2).split(",") if c.strip()]
        elif section == "owners":
            m = _OWNER_LINE.match(line)
            if m:
                owners[m.group(1)] = m.group(2)
    return regions, owners


def region_for(country: str, regions: dict) -> str:
    country = (country or "").strip().upper()
    if not country:
        return ""
    for region, codes in regions.items():
        if country in codes:
            return region
    return ""


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

def resolve_hubspot_token():
    """HUBSPOT_TOKEN env var, else ~/.config/postevent/hubspot.env. Same loader as
    modules/m1-enrichment/push_to_hubspot.py::resolve_token. Never printed."""
    token = os.environ.get("HUBSPOT_TOKEN")
    if token:
        return token.strip()
    if HUBSPOT_ENV_PATH.exists():
        for line in HUBSPOT_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "HUBSPOT_TOKEN":
                return value.strip().strip('"').strip("'")
    return None


class HubSpotClient:
    """Thin Bearer-token wrapper. Every call appends a row to `self.calls`
    ({method, url, status, records, ms}) so each phase's receipt is a byte-level
    record of what actually left the machine."""

    def __init__(self, token: str):
        self._token = token
        self.calls = []

    def call(self, method: str, path: str, body=None, timeout: int = 30, _retried: bool = False):
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw) if raw else {}
                self._record(method, url, resp.status, started, parsed)
                return resp.status, parsed, ""
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            if e.code == 401:
                self._record(method, url, 401, started, None)
                raise M4Error(
                    f"HubSpot rejected the token (401) on {method} {path} -- check HUBSPOT_TOKEN "
                    f"or {HUBSPOT_ENV_PATH}.")
            if e.code == 429 and not _retried:
                try:
                    wait = float(e.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    wait = 1.0
                self._record(method, url, 429, started, None)
                print(f"[hubspot] 429 on {method} {path}; sleeping {wait}s, retrying once", file=sys.stderr)
                time.sleep(wait)
                return self.call(method, path, body, timeout=timeout, _retried=True)
            self._record(method, url, e.code, started, None)
            return e.code, None, text
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._record(method, url, None, started, None)
            return None, None, str(e)[:300]

    def _record(self, method, url, status, started, parsed):
        records = len(parsed.get("results", [])) if isinstance(parsed, dict) else 0
        self.calls.append({"method": method, "url": url, "status": status,
                           "records": records, "ms": int((time.monotonic() - started) * 1000)})

    # -- read helpers -------------------------------------------------------

    def search_contacts(self, slug: str, properties: list):
        """POST /crm/v3/objects/contacts/search on postevent_event EQ <slug>, paged."""
        results, after, pages = [], None, 0
        while True:
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": EVENT_PROPERTY, "operator": "EQ", "value": slug}]}],
                "properties": properties,
                "limit": PAGE_SIZE,
            }
            if after:
                body["after"] = after
            status, parsed, err = self.call("POST", "/crm/v3/objects/contacts/search", body)
            pages += 1
            if status != 200 or parsed is None:
                raise M4Error(f"HubSpot contact search failed (HTTP {status}): {err}")
            results.extend(parsed.get("results", []))
            after = ((parsed.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return results, pages

    def batch_read(self, obj_type: str, ids: list, properties: list, with_history=None):
        out, pages = {}, 0
        for i in range(0, len(ids), BATCH_CHUNK):
            body = {"properties": properties, "inputs": [{"id": str(o)} for o in ids[i:i + BATCH_CHUNK]]}
            if with_history:
                body["propertiesWithHistory"] = with_history
            status, parsed, err = self.call("POST", f"/crm/v3/objects/{obj_type}/batch/read", body)
            pages += 1
            if status not in (200, 207) or parsed is None:
                raise M4Error(f"HubSpot {obj_type} batch/read failed (HTTP {status}): {err}")
            for result in parsed.get("results", []):
                out[result["id"]] = result
        return out, pages

    def associated_ids(self, to_object_type: str, contact_ids: list):
        out, pages = {}, 0
        for i in range(0, len(contact_ids), BATCH_CHUNK):
            body = {"inputs": [{"id": str(c)} for c in contact_ids[i:i + BATCH_CHUNK]]}
            status, parsed, err = self.call(
                "POST", f"/crm/v4/associations/contacts/{to_object_type}/batch/read", body)
            pages += 1
            if status not in (200, 207) or parsed is None:
                raise M4Error(f"HubSpot contacts->{to_object_type} associations failed "
                              f"(HTTP {status}): {err}")
            for result in parsed.get("results", []):
                from_id = (result.get("from") or {}).get("id")
                if not from_id:
                    continue
                out.setdefault(str(from_id), []).extend(
                    str(t.get("toObjectId") or t.get("id")) for t in result.get("to", [])
                    if (t.get("toObjectId") or t.get("id")))
        return out, pages


# ---------------------------------------------------------------------------
# fixture-side engagement maths (shared by every lane: seed writes it, offline reads it)
# ---------------------------------------------------------------------------

def engagement_totals(engagement: dict) -> dict:
    """{email: {opens, clicks, pageviews, form_fills, last_engaged}} from the event's
    engagement stream. Totals are SET, never incremented, so re-seeding the portal
    yields the same values."""
    totals = {}
    for ev in engagement.get("events", []):
        email = (ev.get("email") or "").strip().lower()
        prop = COUNTER_BY_TYPE.get(ev.get("type"))
        if not email or not prop:
            continue
        row = totals.setdefault(email, {p: 0 for p in COUNTER_BY_TYPE.values()})
        row[prop] += 1
        ts = ev.get("ts")
        if ts and (not row.get("postevent_last_engaged") or ts > row["postevent_last_engaged"]):
            row["postevent_last_engaged"] = ts
    return totals


def final_stages(engagement: dict) -> dict:
    """{email: {"stage": <latest to-stage>, "ts": ...}} from lifecycle_changes."""
    latest = {}
    for change in engagement.get("lifecycle_changes", []):
        email = (change.get("email") or "").strip().lower()
        ts, to = change.get("ts"), normalize_stage(change.get("to"))
        if not email or not ts or not to:
            continue
        if email not in latest or ts > latest[email]["ts"]:
            latest[email] = {"stage": to, "ts": ts}
    return latest


def lifecycle_history_from_fixture(engagement: dict) -> dict:
    """{email: [{stage, ts, source: "seeded"}]} ascending. The fixture rows are the
    stream `seed` pushes into HubSpot, so they carry the seeded label in every lane."""
    history = defaultdict(list)
    for change in engagement.get("lifecycle_changes", []):
        email = (change.get("email") or "").strip().lower()
        ts, to = change.get("ts"), normalize_stage(change.get("to"))
        if not email or not ts or not to:
            continue
        history[email].append({"stage": to, "ts": ts, "source": "seeded"})
    for rows in history.values():
        rows.sort(key=lambda r: r["ts"])
    return dict(history)


def counters_payload(row: dict) -> dict:
    props = {p: str(row.get(p, 0)) for p in COUNTER_BY_TYPE.values()}
    last = row.get("postevent_last_engaged")
    if last:
        dt = parse_ts(last)
        # HubSpot datetime properties take ISO-8601 UTC; the fixture stamps are naive.
        props["postevent_last_engaged"] = dt.replace(microsecond=0).isoformat() + "Z" if dt else last
    return props


# ---------------------------------------------------------------------------
# phase: seed
# ---------------------------------------------------------------------------

def seed_event_definition_body(slug: str) -> dict:
    return {
        "label": "Post-Event Engagement",
        "name": f"pe_{re.sub(r'[^a-z0-9_]', '_', slug.lower())}"[:50],
        "description": "Post-event opens, clicks, pageviews and form fills for a webinar audience.",
        "primaryObject": "CONTACT",
        "propertyDefinitions": [
            {"label": "Event slug", "name": "event_slug", "type": "string"},
            {"label": "Asset", "name": "asset", "type": "string"},
            {"label": "Interaction", "name": "interaction", "type": "string"},
        ],
    }


def seed_plan(slug: str, totals: dict, stages: dict) -> list:
    """Every HubSpot request the live seed would make, with real body sizes. Used by
    --live-dry-run; sends nothing."""
    def size(body):
        return len(json.dumps(body).encode("utf-8"))

    plan = [{"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/search",
             "body_bytes": size({"filterGroups": [{"filters": [
                 {"propertyName": EVENT_PROPERTY, "operator": "EQ", "value": slug}]}],
                 "properties": ["email"], "limit": PAGE_SIZE}),
             "note": f"page through contacts tagged {EVENT_PROPERTY}={slug}"}]
    plan.append({"method": "POST", "url": f"{API_BASE}/events/v3/event-definitions",
                 "body_bytes": size(seed_event_definition_body(slug)),
                 "note": "tried once; 403 MISSING_SCOPES falls back to counter properties"})
    for prop in SEED_PROPERTIES:
        plan.append({"method": "POST", "url": f"{API_BASE}/crm/v3/properties/contacts",
                     "body_bytes": size(prop),
                     "note": f"create {prop['name']} if missing (409 = already there)"})
    emails = sorted(set(totals) | set(stages))
    for i in range(0, max(len(emails), 1), BATCH_CHUNK):
        chunk = emails[i:i + BATCH_CHUNK]
        body = {"inputs": [{"id": "<contact id>", "properties": dict(
            counters_payload(totals.get(e, {})),
            **({"lifecyclestage": stages[e]["stage"]} if e in stages else {}))} for e in chunk]}
        plan.append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/batch/update",
                     "body_bytes": size(body),
                     "note": f"set counters + lifecyclestage for {len(chunk)} matched contact(s)"})
    return plan


def phase_seed(ctx) -> dict:
    engagement = load_json(ENGAGEMENT_FIXTURE)
    totals = engagement_totals(engagement)
    stages = final_stages(engagement)
    started = now_iso()
    receipt = {
        "event_slug": ctx["slug"],
        "lane": ctx["lane"],
        "method": "offline",
        "source": "seeded",
        "contacts_matched": 0,
        "events_written": 0,
        "lifecycle_updates": 0,
        "errors": [],
        "endpoints": [],
        "timestamps": {"started": started, "finished": None},
    }

    if ctx["lane"] == "offline":
        receipt["contacts_matched"] = len(set(totals) | set(stages))
        receipt["events_written"] = len(engagement.get("events", []))
        receipt["lifecycle_updates"] = len(stages)
        receipt["timestamps"]["finished"] = now_iso()
        write_json(ctx["out"] / "receipts" / "m4_seed.json", receipt)
        summary(ctx, "seed", f"{receipt['contacts_matched']} contacts matched, "
                             f"{receipt['events_written']} engagement events, "
                             f"{receipt['lifecycle_updates']} lifecycle updates, method=offline")
        return receipt

    client = ctx["client"]
    # 1. match portal contacts to the engagement stream by email
    results, pages = client.search_contacts(ctx["slug"], ["email"])
    id_by_email = {}
    for row in results:
        email = ((row.get("properties") or {}).get("email") or "").strip().lower()
        if email:
            id_by_email[email] = row["id"]
    receipt["endpoints"].append({"method": "POST",
                                 "url": f"{API_BASE}/crm/v3/objects/contacts/search",
                                 "pages": pages, "count": len(id_by_email), "status": 200})
    matched = [e for e in sorted(set(totals) | set(stages)) if e in id_by_email]
    receipt["contacts_matched"] = len(matched)
    if not matched:
        receipt["errors"].append(
            f"no contact in the portal carries {EVENT_PROPERTY}={ctx['slug']} and appears in the "
            "engagement stream -- run M1's push first")
        receipt["timestamps"]["finished"] = now_iso()
        write_json(ctx["out"] / "receipts" / "m4_seed.json", receipt)
        raise M4Error(receipt["errors"][-1])

    # 2. custom behavioural events, once; 403 MISSING_SCOPES is the expected answer on
    #    this portal and falls straight through to the counter properties.
    status, _, err = client.call("POST", "/events/v3/event-definitions",
                                 seed_event_definition_body(ctx["slug"]))
    receipt["endpoints"].append({"method": "POST", "url": f"{API_BASE}/events/v3/event-definitions",
                                 "pages": 1, "count": 0, "status": status})
    use_custom_events = status in (200, 201, 409)
    if not use_custom_events:
        print(f"[seed] event definitions unavailable (HTTP {status}) -- falling back to contact "
              f"properties: {', '.join(COUNTER_PROPERTIES)}", file=sys.stderr)

    if use_custom_events:
        receipt["method"] = "custom_events"
        name = seed_event_definition_body(ctx["slug"])["name"]
        sent, failed = 0, 0
        for ev in engagement.get("events", []):
            email = (ev.get("email") or "").strip().lower()
            if email not in id_by_email:
                continue
            body = {"eventName": name, "objectId": id_by_email[email],
                    "occurredAt": (iso(parse_ts(ev.get("ts"))) or "") + "Z",
                    "properties": {"event_slug": ctx["slug"], "asset": ev.get("asset", ""),
                                   "interaction": ev.get("type", "")}}
            st, _, e_txt = client.call("POST", "/events/v3/send", body)
            if st in (200, 202, 204):
                sent += 1
            else:
                failed += 1
                if len(receipt["errors"]) < 10:
                    receipt["errors"].append(f"events/v3/send HTTP {st}: {e_txt}")
        receipt["events_written"] = sent
        receipt["endpoints"].append({"method": "POST", "url": f"{API_BASE}/events/v3/send",
                                     "pages": sent + failed, "count": sent,
                                     "status": 200 if sent and not failed else (None if not sent else 207)})
        if failed and not sent:
            receipt["timestamps"]["finished"] = now_iso()
            write_json(ctx["out"] / "receipts" / "m4_seed.json", receipt)
            raise M4Error(f"custom-event send failed for every event ({failed} attempts)")
    else:
        receipt["method"] = "contact_properties"
        ok, bad = 0, 0
        for prop in SEED_PROPERTIES:
            st, _, e_txt = client.call("POST", "/crm/v3/properties/contacts", prop)
            if st in (200, 201, 409):
                ok += 1
            else:
                bad += 1
                receipt["errors"].append(f"create property {prop['name']} HTTP {st}: {e_txt}")
        receipt["endpoints"].append({"method": "POST", "url": f"{API_BASE}/crm/v3/properties/contacts",
                                     "pages": len(SEED_PROPERTIES), "count": ok,
                                     "status": 200 if not bad else 207})
        if bad:
            receipt["timestamps"]["finished"] = now_iso()
            write_json(ctx["out"] / "receipts" / "m4_seed.json", receipt)
            raise M4Error(f"{bad} counter propert(ies) could not be created -- seed aborted")
        receipt["events_written"] = sum(
            sum(totals[e][p] for p in COUNTER_BY_TYPE.values()) for e in matched if e in totals)

    # 3. batch-update counters + final lifecycle stage (<=100 per call, values SET)
    inputs = []
    for email in matched:
        props = counters_payload(totals.get(email, {})) if not use_custom_events else {}
        if email in stages:
            props["lifecyclestage"] = stages[email]["stage"]
        if props:
            inputs.append({"id": id_by_email[email], "properties": props})
    updated, update_calls, bad_calls = 0, 0, 0
    for i in range(0, len(inputs), BATCH_CHUNK):
        chunk = inputs[i:i + BATCH_CHUNK]
        st, parsed, e_txt = client.call("POST", "/crm/v3/objects/contacts/batch/update",
                                        {"inputs": chunk})
        update_calls += 1
        if st in (200, 207) and parsed is not None:
            updated += len(parsed.get("results", []))
        else:
            bad_calls += 1
            receipt["errors"].append(f"contacts/batch/update HTTP {st}: {e_txt}")
    receipt["endpoints"].append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/batch/update",
                                 "pages": update_calls, "count": updated,
                                 "status": 200 if not bad_calls else 207})
    receipt["lifecycle_updates"] = sum(1 for e in matched if e in stages)
    receipt["timestamps"]["finished"] = now_iso()
    write_json(ctx["out"] / "receipts" / "m4_seed.json", receipt)
    if bad_calls:
        raise M4Error(f"{bad_calls} of {update_calls} batch updates failed -- see "
                      f"{ctx['out'] / 'receipts' / 'm4_seed.json'}")
    summary(ctx, "seed", f"{receipt['contacts_matched']} contacts matched, "
                         f"{receipt['events_written']} engagement events, "
                         f"{receipt['lifecycle_updates']} lifecycle updates, "
                         f"method={receipt['method']}")
    return receipt


# ---------------------------------------------------------------------------
# phase: sync
# ---------------------------------------------------------------------------

def blank_engagement(source: str) -> dict:
    return {"opens": 0, "clicks": 0, "pageviews": 0, "form_fills": 0,
            "last_engaged": None, "source": source}


def contact_record(cid, email, source, regions, owners, **kw) -> dict:
    country = kw.get("country", "")
    region = kw.get("region") or region_for(country, regions)
    return {
        "id": str(cid),
        "email": email,
        "firstname": kw.get("firstname", ""),
        "lastname": kw.get("lastname", ""),
        "company": kw.get("company", ""),
        "domain": domain_of(email),
        "jobtitle": kw.get("jobtitle", ""),
        "country": country,
        "lifecyclestage": normalize_stage(kw.get("lifecyclestage", "")),
        "lifecycle_history": kw.get("lifecycle_history", []),
        "icp_tier": kw.get("icp_tier", ""),
        "region": region,
        "owner": kw.get("owner") or owners.get(region, ""),
        "attendance_status": kw.get("attendance_status", ""),
        "postevent_event": kw.get("postevent_event", ""),
        "engagement": kw.get("engagement", blank_engagement(source)),
    }


def companies_from_contacts(contacts: list, known=None) -> list:
    """One company per non-freemail contact domain, carrying the contact ids on it.
    `known` ({domain: {id, name}}) comes from the portal in the live lane."""
    known = known or {}
    by_domain = {}
    for c in contacts:
        ak = account_key(c["email"])
        if not ak:
            continue
        row = by_domain.setdefault(ak, {"id": None, "name": "", "domain": ak, "contact_ids": []})
        row["contact_ids"].append(c["id"])
        if not row["name"] and c.get("company"):
            row["name"] = c["company"]
    out = []
    for i, ak in enumerate(sorted(by_domain), start=1):
        row = by_domain[ak]
        hit = known.get(ak, {})
        row["id"] = str(hit.get("id") or f"fx-co-{i:03d}")
        row["name"] = hit.get("name") or row["name"] or display_company(ak)
        out.append(row)
    return out


def sync_offline(ctx) -> dict:
    """Snapshot rebuilt from the repo fixtures. Every record says `fixture`; the
    lifecycle rows keep the `seeded` label because they are the same stream `seed`
    pushes into the portal."""
    engagement = load_json(ENGAGEMENT_FIXTURE)
    existing = load_json(HUBSPOT_FIXTURE)
    segments = load_json(SEGMENTS_FIXTURE)
    regions, owners = load_routing()

    totals = engagement_totals(engagement)
    history = lifecycle_history_from_fixture(engagement)
    attendees = {e.lower() for e in segments.get("attendees", [])}
    no_shows = {e.lower() for e in segments.get("no_shows", [])} - attendees
    speakers = {e.lower() for e in segments.get("speakers", [])}
    by_email = {(r.get("email") or "").lower(): r for r in existing if r.get("email")}
    # The 40 seeded portal rows carry real company names; reuse them for every contact
    # on the same domain instead of title-casing the domain for all of them.
    name_by_domain = {}
    for row in existing:
        dom = domain_of(row.get("email", ""))
        if dom and row.get("company") and dom not in name_by_domain:
            name_by_domain[dom] = row["company"]

    emails = sorted(set(by_email) | set(totals) | set(history) | attendees | no_shows | speakers)
    contacts = []
    for i, email in enumerate(emails, start=1):
        fixture_row = by_email.get(email, {})
        ak = account_key(email)
        hist = history.get(email, [])
        stage = hist[-1]["stage"] if hist else normalize_stage(fixture_row.get("lifecyclestage", ""))
        counters = totals.get(email)
        eng = blank_engagement("fixture")
        if counters:
            eng.update({
                "opens": counters["postevent_opens"], "clicks": counters["postevent_clicks"],
                "pageviews": counters["postevent_pageviews"],
                "form_fills": counters["postevent_form_fills"],
                "last_engaged": counters.get("postevent_last_engaged"),
            })
        contacts.append(contact_record(
            fixture_row.get("vid") or f"fx-{i:04d}", email, "fixture", regions, owners,
            firstname=fixture_row.get("firstname", ""), lastname=fixture_row.get("lastname", ""),
            company=(fixture_row.get("company") or name_by_domain.get(ak or "")
                     or (display_company(ak) if ak else "")),
            jobtitle=fixture_row.get("jobtitle", ""), lifecyclestage=stage,
            lifecycle_history=hist, engagement=eng, postevent_event=ctx["slug"],
            attendance_status=("attended" if email in attendees else
                               "no_show" if email in no_shows else ""),
        ))

    events = [{"email": (e.get("email") or "").lower(), "type": e.get("type"),
               "ts": e.get("ts"), "asset": e.get("asset", ""), "source": "fixture"}
              for e in engagement.get("events", [])]
    return {
        "event_slug": ctx["slug"],
        "pulled_at": now_iso(),
        "lane": "offline",
        "method": "offline",
        "contacts": contacts,
        "companies": companies_from_contacts(contacts),
        "email_engagements": [],
        "events": events,
        "notes": [
            f"offline lane: contacts, engagement counters and lifecycle history rebuilt from "
            f"{ENGAGEMENT_FIXTURE.name}, {HUBSPOT_FIXTURE.name} and {SEGMENTS_FIXTURE.name}; "
            "no portal was contacted.",
            "email_engagements is empty offline: the CRM `emails` objects M2 logs only exist in "
            "the portal, and this lane does not invent them.",
            "region and owner are derived from config/icp.yaml's regions/owners tables -- the same "
            "routing M1 applies.",
        ],
    }


def sync_live(ctx) -> dict:
    client = ctx["client"]
    regions, owners = load_routing()
    endpoints = []

    properties, notes = CONTACT_PROPERTIES, []
    try:
        results, pages = client.search_contacts(ctx["slug"], properties)
    except M4Error as e:
        if "400" not in str(e):
            raise
        properties = [p for p in CONTACT_PROPERTIES if p not in COUNTER_PROPERTIES]
        notes.append("the portal rejected the postevent_* counter properties on read (they are "
                     "created by the seed phase): this sync read the base properties only, so "
                     "every engagement block is zero.")
        print(f"[sync] {notes[-1]}", file=sys.stderr)
        results, pages = client.search_contacts(ctx["slug"], properties)
    endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/search",
                      "pages": pages, "count": len(results), "status": 200})
    if not results:
        raise M4Error(f"no contact in the portal carries {EVENT_PROPERTY}={ctx['slug']} -- "
                      "M1's push has not run for this event")
    contact_ids = [r["id"] for r in results]

    # lifecyclestage history: search cannot return it, batch/read can.
    with_history, hpages = client.batch_read("contacts", contact_ids, properties,
                                             with_history=["lifecyclestage"])
    endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/batch/read",
                      "pages": hpages, "count": len(with_history),
                      "status": 200, "note": "propertiesWithHistory=lifecyclestage"})

    seed_method = ctx.get("seed_method")
    engagement_source = "seeded" if seed_method in ("contact_properties", "custom_events") else "hubspot"
    contacts = []
    for row in results:
        props = dict(row.get("properties") or {})
        detail = with_history.get(row["id"], {})
        props.update({k: v for k, v in (detail.get("properties") or {}).items() if v is not None})
        email = (props.get("email") or "").strip().lower()
        if not email:
            continue
        hist_rows = ((detail.get("propertiesWithHistory") or {}).get("lifecyclestage") or [])
        history = sorted(
            ({"stage": normalize_stage(h.get("value")), "ts": iso(parse_ts(h.get("timestamp"))),
              "source": "hubspot_history"} for h in hist_rows if h.get("value") and h.get("timestamp")),
            key=lambda r: r["ts"])
        counters = {k: int(float(props.get(k) or 0)) for k in COUNTER_BY_TYPE.values()}
        eng = blank_engagement(engagement_source if any(counters.values()) else "hubspot")
        eng.update({"opens": counters["postevent_opens"], "clicks": counters["postevent_clicks"],
                    "pageviews": counters["postevent_pageviews"],
                    "form_fills": counters["postevent_form_fills"],
                    "last_engaged": iso(parse_ts(props.get("postevent_last_engaged"))) or None})
        contacts.append(contact_record(
            row["id"], email, "hubspot", regions, owners,
            firstname=props.get("firstname", ""), lastname=props.get("lastname", ""),
            company=props.get("company", ""), jobtitle=props.get("jobtitle", ""),
            country=props.get("country", ""), lifecyclestage=props.get("lifecyclestage", ""),
            lifecycle_history=history, icp_tier=props.get("icp_tier", ""),
            owner=props.get("hubspot_owner_id", ""),
            attendance_status=props.get("attendance_status", ""),
            postevent_event=props.get(EVENT_PROPERTY, ctx["slug"]), engagement=eng))

    # companies: associations + batch read
    assoc_co, apages = client.associated_ids("companies", contact_ids)
    company_ids = sorted({cid for ids in assoc_co.values() for cid in ids})
    endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v4/associations/contacts/companies/batch/read",
                      "pages": apages, "count": len(company_ids), "status": 200})
    company_rows, cpages = ({}, 0)
    if company_ids:
        company_rows, cpages = client.batch_read("companies", company_ids, COMPANY_PROPERTIES)
        endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/companies/batch/read",
                          "pages": cpages, "count": len(company_rows), "status": 200})
    contacts_by_id = {c["id"]: c for c in contacts}
    companies = []
    for cid, row in sorted(company_rows.items()):
        props = row.get("properties") or {}
        members = [k for k, ids in assoc_co.items() if cid in ids and k in contacts_by_id]
        companies.append({"id": str(cid), "name": props.get("name") or display_company(props.get("domain", "")),
                          "domain": (props.get("domain") or "").lower(), "contact_ids": sorted(members)})
    # contacts whose company record is not in the portal still need an account bucket
    covered = {d["domain"] for d in companies if d["domain"]}
    known = {c["domain"]: {"id": c["id"], "name": c["name"]} for c in companies if c["domain"]}
    for extra in companies_from_contacts(contacts, known=known):
        if extra["domain"] not in covered:
            companies.append(extra)

    # email engagements: contact -> emails associations, then batch read of `emails`
    assoc_em, epages = client.associated_ids("emails", contact_ids)
    email_ids = sorted({eid for ids in assoc_em.values() for eid in ids})
    endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v4/associations/contacts/emails/batch/read",
                      "pages": epages, "count": len(email_ids), "status": 200,
                      "note": f"email->contact association type {EMAIL_TO_CONTACT_ASSOC_TYPE}"})
    email_engagements = []
    if email_ids:
        email_rows, mpages = client.batch_read("emails", email_ids, EMAIL_PROPERTIES)
        endpoints.append({"method": "POST", "url": f"{API_BASE}/crm/v3/objects/emails/batch/read",
                          "pages": mpages, "count": len(email_rows), "status": 200})
        for contact_id, ids in sorted(assoc_em.items()):
            for eid in ids:
                row = email_rows.get(eid)
                if not row:
                    continue
                props = row.get("properties") or {}
                email_engagements.append({
                    "id": str(eid), "contact_id": str(contact_id),
                    "subject": props.get("hs_email_subject") or "",
                    "ts": iso(parse_ts(props.get("hs_timestamp"))), "source": "hubspot"})

    if seed_method == "contact_properties":
        notes.append("engagement counters were seeded onto contact properties (the portal refuses "
                     "custom event definitions), so `events` is empty and the per-contact counters "
                     "carry the stream; every engagement block is labelled source: \"seeded\".")
    elif seed_method is None:
        notes.append("no m4_seed.json in this run directory: engagement counters are whatever the "
                     "portal already held.")
    snapshot = {
        "event_slug": ctx["slug"],
        "pulled_at": now_iso(),
        "lane": "live",
        "method": seed_method or "contact_properties",
        "contacts": contacts,
        "companies": companies,
        "email_engagements": email_engagements,
        "events": [],
        "notes": notes,
    }
    ctx["sync_endpoints"] = endpoints
    return snapshot


def phase_sync(ctx) -> dict:
    seed_receipt_path = ctx["out"] / "receipts" / "m4_seed.json"
    if seed_receipt_path.exists():
        try:
            ctx["seed_method"] = load_json(seed_receipt_path).get("method")
        except (ValueError, OSError):
            ctx["seed_method"] = None
    snapshot = sync_offline(ctx) if ctx["lane"] == "offline" else sync_live(ctx)
    write_json(ctx["out"] / "snapshot.json", snapshot)

    endpoints = ctx.get("sync_endpoints", [])
    totals = {
        "contacts": len(snapshot["contacts"]),
        "companies": len(snapshot["companies"]),
        "email_engagements": len(snapshot["email_engagements"]),
        "events": len(snapshot["events"]),
        "lifecycle_history_rows": sum(len(c["lifecycle_history"]) for c in snapshot["contacts"]),
        "requests": len(ctx["client"].calls) if ctx.get("client") else 0,
    }
    write_json(ctx["out"] / "receipts" / "m4_hubspot_sync.json",
               {"event_slug": ctx["slug"], "lane": snapshot["lane"], "pulled_at": snapshot["pulled_at"],
                "endpoints": endpoints, "totals": totals})
    summary(ctx, "sync", f"{totals['contacts']} contacts, {totals['companies']} companies, "
                         f"{totals['email_engagements']} email engagements, "
                         f"{totals['events']} events, method={snapshot['method']}")
    return snapshot


# ---------------------------------------------------------------------------
# deterministic analysis (the validator the LLM is graded against)
# ---------------------------------------------------------------------------

def weighted_score(engagement: dict) -> float:
    return round(engagement.get("opens", 0) * WEIGHTS["open"]
                 + engagement.get("clicks", 0) * WEIGHTS["click"]
                 + engagement.get("pageviews", 0) * WEIGHTS["pageview"]
                 + engagement.get("form_fills", 0) * WEIGHTS["form_fill"], 1)


def engagement_total(engagement: dict) -> int:
    return int(engagement.get("opens", 0) + engagement.get("clicks", 0)
               + engagement.get("pageviews", 0) + engagement.get("form_fills", 0))


def outlier_threshold(values: list):
    """Tukey IQR fence (Q3 + 1.5*IQR) over this run's own per-contact engagement
    totals -- 'unusual for THIS event' is a question about a distribution, so the
    threshold moves with the data instead of being a constant nobody can justify.
    Returns (threshold, rationale)."""
    totals = sorted(v for v in values if v > 0)
    n = len(totals)
    if n < 4:
        return 5, (f"fallback floor: only {n} contact(s) have post-event engagement -- too few "
                   "for a quartile fence, so a fixed floor of 5 total interactions applies.")
    q1, _, q3 = statistics.quantiles(totals, n=4, method="inclusive")
    iqr = q3 - q1
    threshold = max(3, math.ceil(q3 + 1.5 * iqr))
    return threshold, (f"Tukey IQR fence over {n} engaged contacts (median "
                       f"{statistics.median(totals):.1f}, Q1 {q1:.1f}, Q3 {q3:.1f}, IQR {iqr:.1f}): "
                       f"threshold = ceil(Q3 + 1.5*IQR) = {threshold} interactions.")


def movement_windows(contacts: list, as_of: datetime, days=(7, 14, 30)) -> dict:
    """Stage transitions counted from each contact's lifecycle_history inside each
    window ending at `as_of`. A transition exactly on the cutoff instant counts as
    inside the window."""
    rows = []
    for c in contacts:
        for h in c.get("lifecycle_history", []):
            dt = parse_ts(h.get("ts"))
            if dt:
                rows.append({"email": c["email"], "company": c.get("company", ""),
                             "stage": h.get("stage", ""), "ts": h.get("ts"), "dt": dt,
                             "source": h.get("source", "seeded")})
    rows.sort(key=lambda r: r["ts"])
    out = {}
    for d in days:
        cutoff = as_of - timedelta(days=d)
        window = [r for r in rows if cutoff <= r["dt"] <= as_of]
        out[f"{d}d"] = {
            "transitions": len(window),
            "contacts": len({r["email"] for r in window}),
            "by_stage": dict(Counter(r["stage"] for r in window)),
            "source_mix": dict(Counter(r["source"] for r in window)),
            "window_start": iso(cutoff),
            "window_end": iso(as_of),
        }
    return out, rows


def deterministic_analysis(snapshot: dict) -> dict:
    contacts = snapshot["contacts"]
    by_email = {c["email"]: c for c in contacts}
    companies = {c["domain"]: c for c in snapshot["companies"] if c.get("domain")}

    scores = {c["email"]: weighted_score(c["engagement"]) for c in contacts}
    totals = {c["email"]: engagement_total(c["engagement"]) for c in contacts}
    engaged = {e for e, t in totals.items() if t > 0}

    attendees = {c["email"] for c in contacts if c.get("attendance_status") == "attended"}
    if not attendees:      # portal without attendance_status: fall back to everyone tagged
        attendees = {c["email"] for c in contacts}
    mqls = {e for e in attendees if stage_rank(by_email[e]["lifecyclestage"]) >= MQL_RANK}
    sqls = {e for e in attendees
            if stage_rank(by_email[e]["lifecyclestage"]) >= LIFECYCLE_RANK["salesqualifiedlead"]}
    mql_rate = round(len(mqls) / len(attendees), 4) if attendees else 0.0

    stamps = [parse_ts(c["engagement"].get("last_engaged")) for c in contacts]
    stamps += [parse_ts(h.get("ts")) for c in contacts for h in c.get("lifecycle_history", [])]
    stamps += [parse_ts(e.get("ts")) for e in snapshot.get("events", [])]
    stamps += [parse_ts(e.get("ts")) for e in snapshot.get("email_engagements", [])]
    stamps = [s for s in stamps if s]
    as_of = max(stamps) if stamps else datetime.now(timezone.utc).replace(tzinfo=None)
    movement, transition_rows = movement_windows(contacts, as_of)
    daily = Counter(r["dt"].date().isoformat() for r in transition_rows)
    movement_timeline = [{"date": d, "count": n} for d, n in sorted(daily.items())]

    # accounts
    account_rows = []
    for domain, company in sorted(companies.items()):
        members = [by_email[c["email"]] for c in contacts if c["domain"] == domain]
        engaged_members = [m for m in members if totals.get(m["email"], 0) > 0]
        if not members:
            continue
        account_rows.append({
            "company": company["name"], "domain": domain,
            "score": round(sum(scores.get(m["email"], 0.0) for m in members), 1),
            "engaged_contacts": len(engaged_members),
            "known_contacts": len(members),
            "coverage_pct": round(100 * len(engaged_members) / len(members), 1),
            "contacts": sorted(
                [{"email": m["email"], "name": display_name(m), "title": m.get("jobtitle", ""),
                  "company": m.get("company", "") or company["name"],
                  "stage": m["lifecyclestage"], "score": scores.get(m["email"], 0.0),
                  "engagement": m["engagement"]} for m in engaged_members],
                key=lambda r: -r["score"]),
        })
    top_accounts = sorted(account_rows, key=lambda a: (-a["score"], a["company"]))[:10]
    committees = [
        {"company": a["company"], "domain": a["domain"],
         "contacts": [c["email"] for c in a["contacts"]],
         "why": (f"{a['engaged_contacts']} engaged contacts at one account "
                 f"({a['coverage_pct']}% of the known roster), combined weighted score {a['score']}"),
         "why_source": "rules"}
        for a in sorted(account_rows, key=lambda a: (-a["engaged_contacts"], -a["score"]))
        if a["engaged_contacts"] >= COMMITTEE_MIN_CONTACTS
    ][:10]

    top_contacts = sorted(
        ({"email": c["email"], "name": display_name(c), "title": c.get("jobtitle", ""),
          "company": c.get("company", ""), "stage": c["lifecyclestage"],
          "score": scores[c["email"]], "engagement": c["engagement"],
          "interactions": totals[c["email"]]}
         for c in contacts if c["email"] in engaged),
        key=lambda r: (-r["score"], r["email"]))[:10]

    # Lead-interest score, deterministic half: the weighted engagement score normalised
    # to 0-100 against this run's top scorer. The LLM scores the same contacts with a
    # rationale; both are kept, neither overwrites the other.
    top_score = max([s for s in scores.values()] or [0.0]) or 1.0
    interest_scores = [
        {"contact_id": c["email"], "score": int(round(100 * scores[c["email"]] / top_score)),
         "rationale": (f"weighted engagement {scores[c['email']]} of this run's top score "
                       f"{top_score} (form fills {c['engagement'].get('form_fills', 0)}, clicks "
                       f"{c['engagement'].get('clicks', 0)}, pageviews "
                       f"{c['engagement'].get('pageviews', 0)}, opens "
                       f"{c['engagement'].get('opens', 0)})"),
         "evidence": [f"{k}={c['engagement'].get(k, 0)}"
                      for k in ("opens", "clicks", "pageviews", "form_fills")],
         "source": "rules"}
        for c in sorted((by_email[e] for e in engaged),
                        key=lambda c: (-scores[c["email"]], c["email"]))[:25]]

    # anomalies: an outlier fence over this run's own distribution, plus the stalled
    # high-intent contacts (engagement over the fence, stage never moved).
    threshold, rationale = outlier_threshold(list(totals.values()))
    anomalies = []
    for email in sorted(engaged, key=lambda e: (-totals[e], e)):
        if totals[email] < threshold:
            continue
        c = by_email[email]
        eng = c["engagement"]
        evidence = [{"metric": k, "value": eng.get(k, 0)}
                    for k in ("opens", "clicks", "pageviews", "form_fills")]
        evidence.append({"metric": "last_engaged", "value": eng.get("last_engaged")})
        evidence.append({"metric": "lifecycle_history", "value": len(c.get("lifecycle_history", []))})
        for row in [e for e in snapshot.get("events", []) if e.get("email") == email][:8]:
            evidence.append({"metric": f"event:{row.get('type')}", "value": row.get("asset"),
                             "ts": row.get("ts"), "source": row.get("source")})
        anomalies.append({"contact": email, "company": c.get("company", ""),
                          "metric": "engagement_interactions", "value": totals[email],
                          "threshold": threshold, "evidence_rows": evidence})
        if not c.get("lifecycle_history"):
            anomalies.append({"contact": email, "company": c.get("company", ""),
                              "metric": "stalled_high_engagement", "value": totals[email],
                              "threshold": threshold,
                              "evidence_rows": evidence + [{"metric": "stage_transitions", "value": 0}]})
    anomalies = anomalies[:12]

    return {
        "as_of": iso(as_of),
        "mql_rate": mql_rate,
        "attendee_to_mql": {"attendees": len(attendees), "mqls": len(mqls), "sqls": len(sqls)},
        "movement": movement,
        "movement_timeline": movement_timeline,
        "top_accounts": top_accounts,
        "top_contacts": top_contacts,
        "interest_scores": interest_scores,
        "committees": committees,
        "anomalies": anomalies,
        "anomaly_detection": {"threshold": threshold, "rationale": rationale,
                              "engaged_contacts": len(engaged), "flagged": len(anomalies)},
        "counts": {"contacts": len(contacts), "companies": len(companies),
                   "engaged_contacts": len(engaged),
                   "email_engagements": len(snapshot.get("email_engagements", [])),
                   "events": len(snapshot.get("events", []))},
        "transitions": [{"email": r["email"], "company": r["company"], "stage": r["stage"],
                         "ts": r["ts"], "source": r["source"]} for r in transition_rows],
    }


def rules_narrative(det: dict, event: dict) -> str:
    """Templated from the deterministic numbers. Labelled narrative_source: "rules"
    everywhere it is used -- it is never presented as model output."""
    m = det["movement"]
    a2m = det["attendee_to_mql"]
    mixes = []
    for w in ("7d", "14d", "30d"):
        mix = m[w]["source_mix"]
        mixes.append(f"{w}: {m[w]['transitions']} transitions across {m[w]['contacts']} contacts"
                     + (f" ({', '.join(f'{k} {v}' for k, v in sorted(mix.items()))})" if mix else ""))
    top = ", ".join(f"{a['company']} ({a['score']})" for a in det["top_accounts"][:3]) or "none"
    stalled = [x["contact"] for x in det["anomalies"] if x["metric"] == "stalled_high_engagement"]
    return (
        f"Lifecycle movement for {event.get('name', 'this event')} as of {det['as_of']} -- "
        + "; ".join(mixes) + ". "
        f"{a2m['mqls']} of {a2m['attendees']} attendees are now MQL or later "
        f"({round(det['mql_rate'] * 100, 1)}%), {a2m['sqls']} at SQL or later. "
        f"Top engaged accounts by weighted engagement: {top}. "
        f"{len(det['committees'])} account(s) show {COMMITTEE_MIN_CONTACTS}+ engaged contacts. "
        f"{det['anomaly_detection']['flagged']} contact(s) clear the "
        f"{det['anomaly_detection']['threshold']}-interaction outlier fence"
        + (f", of which {len(stalled)} have no stage movement at all" if stalled else "")
        + ". This paragraph is assembled from the deterministic counts above, not from a model.")


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------

def openrouter_key() -> str:
    """OPENROUTER_API_KEY env var, else ~/.config/postevent/llm.env. Never printed."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if LLM_ENV_PATH.exists():
        for line in LLM_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "OPENROUTER_API_KEY":
                    return v.strip().strip('"').strip("'")
    return ""


def models_to_try() -> list:
    env_models = [m.strip() for m in os.environ.get("OPENROUTER_MODEL", "").split(",") if m.strip()]
    return env_models or [DEFAULT_MODEL]


def deadline_seconds() -> int:
    try:
        return max(10, int(os.environ.get("LLM_BATCH_DEADLINE_S", "120")))
    except ValueError:
        return 120


class ModelRejected(RuntimeError):
    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


class LLMLedger:
    """Call budget + receipt tape: one row per HTTP attempt, successful or not."""

    def __init__(self, budget: int):
        self.budget = budget
        self.calls = []

    @property
    def completions(self):
        return sum(1 for c in self.calls if c.get("ok"))

    @property
    def remaining(self):
        return self.budget - self.completions

    def record(self, **row):
        row.setdefault("ts", now_iso())
        self.calls.append(row)

    def payload(self, lane, model):
        return {"lane": lane, "model": model, "budget": self.budget,
                "completions": self.completions, "attempts": len(self.calls),
                "deadline_s": deadline_seconds(), "calls": self.calls}


def strip_json(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("model returned empty content")
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                _, end = dec.raw_decode(text, i)
                return text[i:end]
            except ValueError:
                continue
    raise ValueError("no JSON value found in model output")


def _post(key: str, model: str, prompt: str, max_tokens: int, timeout: int):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2, "max_tokens": max_tokens}
    if os.environ.get("LLM_REASONING", "off").strip().lower() != "on":
        body["reasoning"] = {"enabled": False}
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://kai8karma.github.io/agentkai/",
                 "X-Title": "Post-Event Engine -- M4"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:300]
        raise ModelRejected(f"HTTP {e.code} for {model!r}: {text}",
                            transient=e.code == 429 or 500 <= e.code < 600) from None
    except TimeoutError:
        raise ModelRejected(f"{model!r} exceeded the {timeout}s deadline", transient=True) from None
    except OSError as e:
        raise ModelRejected(f"transport error for {model!r}: {e}", transient=True) from None
    except json.JSONDecodeError as e:
        raise ModelRejected(f"{model!r} returned unparseable body: {e}", transient=True) from None
    if isinstance(data, dict) and data.get("error"):
        err = data["error"] or {}
        code = err.get("code") if isinstance(err.get("code"), int) else None
        message = str(err.get("message"))[:200]
        transient = code == 429 or (code is not None and 500 <= code < 600)
        raise ModelRejected(f"provider error for {model!r}: {code} {message}", transient=transient)
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ModelRejected(f"unexpected response shape from {model!r}: {e}") from None
    if not content:
        raise ModelRejected(f"{model!r} returned empty content")
    usage = data.get("usage") or {}
    return content, status, usage


def call_llm(prompt: str, purpose: str, ledger: LLMLedger, max_tokens: int = 4000):
    """Single chokepoint. Budget-checked, deadline-bounded, up to two retries on 429/5xx with backoff, then the next model in OPENROUTER_MODEL's comma list,
    one receipt row per HTTP attempt (model, prompt chars, tokens, latency, status).
    Fails loud -- never returns a stub."""
    key = openrouter_key()
    if not key:
        raise M4Error("no OPENROUTER_API_KEY (env or ~/.config/postevent/llm.env)")
    if ledger.remaining <= 0:
        raise M4Error(f"LLM budget exhausted ({ledger.budget}) before '{purpose}'")
    timeout = deadline_seconds()
    last = None
    for model in models_to_try():
        for attempt in (1, 2, 3):
            started = time.monotonic()
            try:
                content, status, usage = _post(key, model, prompt, max_tokens, timeout)
            except ModelRejected as e:
                ledger.record(model=model, purpose=purpose, attempt=attempt, ok=False,
                              prompt_chars=len(prompt), tokens=None,
                              latency_ms=int((time.monotonic() - started) * 1000),
                              http_status=_status_from_error(str(e)), error=str(e)[:300])
                last = e
                if e.transient and attempt < 3:
                    print(f"[llm] {purpose}: {e} -- retry {attempt}/2 after {5 * attempt}s", file=sys.stderr)
                    time.sleep(5 * attempt)
                    continue
                break
            ledger.record(model=model, purpose=purpose, attempt=attempt, ok=True,
                          prompt_chars=len(prompt),
                          tokens={"prompt": usage.get("prompt_tokens"),
                                  "completion": usage.get("completion_tokens"),
                                  "total": usage.get("total_tokens")},
                          latency_ms=int((time.monotonic() - started) * 1000),
                          http_status=status, chars=len(content))
            return content, model
    raise M4Error(f"every candidate model failed for '{purpose}': {last}")


def _status_from_error(msg: str):
    m = re.search(r"HTTP (\d{3})", msg)
    return int(m.group(1)) if m else None


def fill(template: str, **kw) -> str:
    for key, value in kw.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def contact_rows_block(det: dict) -> str:
    lines = []
    for row in sorted(det["top_contacts"] + [c for a in det["top_accounts"] for c in a["contacts"]],
                      key=lambda r: -r["score"]):
        if any(line.startswith(row["email"] + " |") for line in lines):
            continue
        eng = row["engagement"]
        lines.append(
            f"{row['email']} | {row.get('title') or 'unknown title'} | "
            f"{row.get('company') or domain_of(row['email'])} | {row.get('stage') or 'unknown'} | "
            f"opens={eng.get('opens', 0)} clicks={eng.get('clicks', 0)} "
            f"pageviews={eng.get('pageviews', 0)} form_fills={eng.get('form_fills', 0)} | "
            f"last_engaged={eng.get('last_engaged') or 'none'} | "
            f"weighted_score={row['score']}")
        if len(lines) >= MAX_PROMPT_CONTACTS:
            break
    return "\n".join(lines) or "(no engaged contacts in this snapshot)"


def candidates_block(det: dict) -> str:
    rows = [f"{a['contact']} | metric={a['metric']} value={a['value']} threshold={a['threshold']}"
            for a in det["anomalies"]]
    return "\n".join(rows) or "(the outlier fence flagged nobody in this snapshot)"


def accounts_block(det: dict) -> str:
    blocks = []
    for a in det["top_accounts"][:MAX_PROMPT_ACCOUNTS]:
        if a["engaged_contacts"] < COMMITTEE_MIN_CONTACTS:
            continue
        lines = [f"{a['company']} ({a['domain']}) -- engaged {a['engaged_contacts']}/"
                 f"{a['known_contacts']} known contacts, weighted score {a['score']}"]
        for c in a["contacts"]:
            eng = c["engagement"]
            lines.append(f"  - {c['email']} | {c.get('title') or 'unknown title'} | "
                         f"{c.get('stage') or 'unknown'} | opens={eng.get('opens', 0)} "
                         f"clicks={eng.get('clicks', 0)} pageviews={eng.get('pageviews', 0)} "
                         f"form_fills={eng.get('form_fills', 0)} | score={c['score']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no account has more than one engaged contact)"


def transitions_block(det: dict) -> str:
    rows = [f"{t['email']} | {t.get('company') or domain_of(t['email'])} | -> {t['stage']} | "
            f"{t['ts']} | {t['source']}" for t in det["transitions"][-MAX_PROMPT_TRANSITIONS:]]
    return "\n".join(rows) or "(no stage transitions in this snapshot)"


def build_llm_prompts(det: dict, event: dict) -> list:
    event_block = json.dumps({"name": event.get("name"), "date": event.get("date"),
                              "host_company": event.get("host_company"),
                              "slug": event.get("slug")}, indent=2)
    p1 = fill((PROMPTS_DIR / "anomalies.md").read_text(encoding="utf-8"),
              EVENT=event_block, CANDIDATES=candidates_block(det), CONTACT_ROWS=contact_rows_block(det))
    p1b = fill((PROMPTS_DIR / "interest_scores.md").read_text(encoding="utf-8"),
               EVENT=event_block, CONTACT_ROWS=contact_rows_block(det))
    p2 = fill((PROMPTS_DIR / "movement_narrative.md").read_text(encoding="utf-8"),
              EVENT=event_block,
              WINDOWS=json.dumps(det["movement"], indent=2),
              TRANSITIONS=transitions_block(det),
              FUNNEL=json.dumps({"attendee_to_mql": det["attendee_to_mql"],
                                 "mql_rate": det["mql_rate"],
                                 "engaged_contacts": det["counts"]["engaged_contacts"]}, indent=2))
    p3 = fill((PROMPTS_DIR / "committees.md").read_text(encoding="utf-8"),
              EVENT=event_block, ACCOUNT_ROWS=accounts_block(det))
    return [("anomalies", p1), ("interest_scores", p1b), ("movement_narrative", p2), ("committees", p3)]


def build_validator(det: dict, llm: dict) -> dict:
    """Deterministic math grades the model: same fields, compared, every disagreement
    recorded. Nothing the model says overwrites a computed number."""
    agreements, disagreements = 0, []

    def compare(field, deterministic, model):
        nonlocal agreements
        if model is None:
            return
        if deterministic == model:
            agreements += 1
        else:
            disagreements.append({"field": field, "deterministic": deterministic, "llm": model})

    det_top_contacts = [c["email"] for c in det["top_contacts"][:5]]
    compare("top_contacts_top5", det_top_contacts, (llm.get("top_contacts") or None) and
            [str(x).strip().lower() for x in llm["top_contacts"]][:5])

    det_top_accounts = [a["company"] for a in det["top_accounts"][:5]]
    compare("top_accounts_top5", det_top_accounts, (llm.get("top_accounts") or None) and
            [str(x).strip() for x in llm["top_accounts"]][:5])

    det_committees = sorted(c["company"] for c in det["committees"])
    llm_committees = sorted({str(c.get("company", "")).strip()
                             for c in llm.get("committees") or []}) or None
    compare("committee_companies", det_committees, llm_committees)

    det_anomalies = sorted({a["contact"] for a in det["anomalies"] if a.get("contact")})
    llm_anomalies = sorted({str(a.get("contact", "")).strip().lower()
                            for a in llm.get("anomalies") or []}) or None
    compare("anomaly_contacts", det_anomalies, llm_anomalies)

    counts = llm.get("movement_counts") or {}
    for window in ("7d", "14d", "30d"):
        if window in counts:
            try:
                compare(f"movement_{window}", det["movement"][window]["transitions"],
                        int(counts[window]))
            except (TypeError, ValueError):
                disagreements.append({"field": f"movement_{window}",
                                      "deterministic": det["movement"][window]["transitions"],
                                      "llm": counts[window]})

    scored = {str(s.get("contact_id", "")).strip().lower() for s in llm.get("interest_scores") or []}
    known = {c["email"] for c in det["top_contacts"]} | {
        c["email"] for a in det["top_accounts"] for c in a["contacts"]}
    ungrounded = sorted(e for e in scored if e and e not in known)
    if ungrounded:
        disagreements.append({"field": "interest_scores_ungrounded",
                              "deterministic": "contacts present in the snapshot only",
                              "llm": ungrounded})
    return {"agreements": agreements, "disagreements": disagreements}


def coerce_llm_shape(purpose: str, parsed, notes: list) -> dict:
    """The prompts ask for one JSON object per purpose; smaller models sometimes return the
    inner list bare, or a string. Map what came back onto the expected keys by inspecting the
    items, and record every coercion so analysis.json shows the model did not follow the shape.
    Nothing is invented: an unrecognisable payload maps to empty lists and a note."""
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        items = [x for x in parsed if isinstance(x, dict)]
        keys = set().union(*(x.keys() for x in items)) if items else set()
        if purpose == "anomalies":
            out = {"anomalies": items} if keys & {"metric", "evidence", "evidence_rows", "anomaly", "contact"} else {}
        elif purpose == "interest_scores":
            out = {"interest_scores": items} if keys & {"score", "interest_score"} else {}
        elif purpose == "committees":
            out = {"committees": items} if keys & {"company", "contacts"} else {}
        elif purpose == "movement_narrative":
            text = " ".join(x for x in parsed if isinstance(x, str)).strip()
            out = {"narrative": text} if text else {}
        else:
            out = {}
        notes.append(f"{purpose}: model returned a JSON list, mapped to {sorted(out) or 'nothing'}")
        return out
    if isinstance(parsed, str) and purpose == "movement_narrative" and parsed.strip():
        notes.append("movement_narrative: model returned a bare string, used as the narrative")
        return {"narrative": parsed.strip()}
    notes.append(f"{purpose}: model returned {type(parsed).__name__}, ignored")
    return {}


def run_llm_analysis(det: dict, event: dict, ledger: LLMLedger) -> tuple:
    """Up to `ledger.budget` calls over the snapshot. Returns (llm_block, model)."""
    llm = {"anomalies": [], "interest_scores": [], "movement_narrative": "", "committees": [],
           "top_contacts": [], "top_accounts": [], "movement_counts": {}, "stalled_contacts": [],
           "rejected_candidates": [], "not_committees": []}
    model_used = None
    for purpose, prompt in build_llm_prompts(det, event):
        if ledger.remaining <= 0:
            print(f"[llm] budget {ledger.budget} spent before '{purpose}' -- skipped", file=sys.stderr)
            break
        content, model = call_llm(prompt, purpose, ledger)
        model_used = model_used or model
        if model not in llm.setdefault("models_used", []):
            llm["models_used"].append(model)
        try:
            parsed = json.loads(strip_json(content))
        except ValueError as e:
            raise M4Error(f"'{purpose}' returned unparseable JSON: {e}")
        parsed = coerce_llm_shape(purpose, parsed, llm.setdefault("shape_notes", []))
        if purpose == "anomalies":
            llm["anomalies"] = parsed.get("anomalies", [])
            llm["rejected_candidates"] = parsed.get("rejected_candidates", [])
        elif purpose == "interest_scores":
            llm["interest_scores"] = parsed.get("interest_scores", [])
            llm["top_contacts"] = parsed.get("top_contacts", [])
        elif purpose == "movement_narrative":
            llm["movement_narrative"] = parsed.get("narrative", "")
            llm["movement_counts"] = parsed.get("counts", {})
            llm["stalled_contacts"] = parsed.get("stalled_contacts", [])
        elif purpose == "committees":
            llm["committees"] = parsed.get("committees", [])
            llm["top_accounts"] = parsed.get("top_accounts", [])
            llm["not_committees"] = parsed.get("not_committees", [])
    return llm, model_used


def phase_analyze(ctx) -> dict:
    snapshot_path = ctx["out"] / "snapshot.json"
    if not snapshot_path.exists():
        raise M4Error(f"{snapshot_path} not found -- run the sync phase first")
    snapshot = load_json(snapshot_path)
    det = deterministic_analysis(snapshot)
    event = event_identity(load_event(ctx["event_path"]), ctx["slug"])

    ledger = LLMLedger(ctx["budget"])
    lane, model, llm_block, narrative_source = "rules", None, None, "rules"
    reason = None
    if ctx["lane"] == "offline":
        reason = "--offline: the rules lane is the requested lane"
    elif not openrouter_key():
        reason = "no OPENROUTER_API_KEY (env or ~/.config/postevent/llm.env)"
    if reason is None:
        try:
            llm_block, model = run_llm_analysis(det, event, ledger)
        except Exception:
            # Keep the receipt tape even when the analysis fails: every HTTP attempt
            # made so far is evidence, and the failure itself must not erase it.
            write_json(ctx["out"] / "receipts" / "m4_llm_calls.json",
                       ledger.payload("live-failed", None))
            raise
        llm_block["validator"] = build_validator(det, llm_block)
        lane, narrative_source = "live", "live"
        if not (llm_block.get("movement_narrative") or "").strip():
            raise M4Error("the movement-narrative call returned no narrative text")
    narrative = (llm_block["movement_narrative"] if llm_block else rules_narrative(det, event))

    analysis = {
        "event_slug": ctx["slug"],
        "generated_at": now_iso(),
        "lane": lane,
        "model": model,
        "narrative_source": narrative_source,
        "movement_narrative": narrative,
        "deterministic": det,
        "llm": llm_block,
        "notes": ([f"rules lane: {reason}. `llm` is null and movement_narrative is templated from "
                   "the deterministic counts -- nothing here is model output."] if reason else []),
    }
    write_json(ctx["out"] / "analysis.json", analysis)
    write_json(ctx["out"] / "receipts" / "m4_llm_calls.json", ledger.payload(lane, model))
    validator = (llm_block or {}).get("validator", {"agreements": 0, "disagreements": []})
    summary(ctx, "analyze",
            f"mql_rate {round(det['mql_rate'] * 100, 1)}%, "
            f"{len(det['anomalies'])} anomalies, {len(det['committees'])} committees, "
            f"{ledger.completions}/{ledger.budget} LLM calls, "
            f"validator {validator['agreements']} agree / {len(validator['disagreements'])} disagree, "
            f"narrative={narrative_source}", lane=lane)
    return analysis


# ---------------------------------------------------------------------------
# phase: render
# ---------------------------------------------------------------------------

def build_dashboard_data(snapshot: dict, analysis: dict, event: dict) -> dict:
    det = analysis["deterministic"]
    llm = analysis.get("llm") or {}
    scores_by_email = {}
    for row in llm.get("interest_scores") or []:
        cid = str(row.get("contact_id", "")).strip().lower()
        if cid:
            scores_by_email[cid] = {"score": row.get("score"), "rationale": row.get("rationale"),
                                    "evidence": row.get("evidence")}
    llm_why = {}
    for row in llm.get("committees") or []:
        name = str(row.get("company", "")).strip()
        if name:
            llm_why[name] = row.get("why")
    rules_scores = {r["contact_id"]: r["score"] for r in det.get("interest_scores", [])}
    llm_anomaly_note = {}
    for row in llm.get("anomalies") or []:
        key = str(row.get("contact", "")).strip().lower()
        if key:
            llm_anomaly_note[key] = row.get("rationale")

    counts = det["counts"]
    a2m = det["attendee_to_mql"]
    engagement_sources = sorted({c["engagement"].get("source", "unknown") for c in snapshot["contacts"]})
    funnel = [
        {"stage": "Tagged contacts", "count": counts["contacts"]},
        {"stage": "Attendees", "count": a2m["attendees"]},
        {"stage": "Engaged post-event", "count": counts["engaged_contacts"]},
        {"stage": "MQL+", "count": a2m["mqls"]},
        {"stage": "SQL+", "count": a2m["sqls"]},
    ]
    return {
        "generated_at": now_iso(),
        "as_of": det["as_of"],
        "event": event,
        "lane": {"data": snapshot["lane"], "narrative": analysis["narrative_source"],
                 "engagement": "seeded" if "seeded" in engagement_sources else
                               ("fixture" if "fixture" in engagement_sources else "hubspot")},
        "source": {"method": snapshot.get("method", ""), "pulled_at": snapshot.get("pulled_at", ""),
                   "contacts": "hubspot" if snapshot["lane"] == "live" else "fixture",
                   "notes": snapshot.get("notes", [])},
        "kpis": {
            "contacts": counts["contacts"],
            "companies": counts["companies"],
            "attendees": a2m["attendees"],
            "engaged_contacts": counts["engaged_contacts"],
            "email_engagements": counts["email_engagements"],
            "mqls": a2m["mqls"],
            "sqls": a2m["sqls"],
            "mql_rate": det["mql_rate"],
            "mql_rate_pct": round(det["mql_rate"] * 100, 1),
            "committees": len(det["committees"]),
            "anomalies": len(det["anomalies"]),
            "movement_30d": det["movement"]["30d"]["transitions"],
        },
        "funnel": funnel,
        "top_accounts": [
            {"company": a["company"], "domain": a["domain"], "score": a["score"],
             "engaged_contacts": a["engaged_contacts"], "known_contacts": a["known_contacts"],
             "coverage_pct": a["coverage_pct"],
             "is_committee": a["engaged_contacts"] >= COMMITTEE_MIN_CONTACTS,
             "contacts": [{"email": c["email"], "name": c["name"], "title": c["title"],
                           "stage": c["stage"], "score": c["score"]} for c in a["contacts"][:6]]}
            for a in det["top_accounts"]],
        "top_contacts": [
            {"email": c["email"], "name": c["name"], "title": c["title"], "company": c["company"],
             "stage": c["stage"], "score": c["score"], "interactions": c["interactions"],
             "rules_score": rules_scores.get(c["email"]),
             "llm_score": (scores_by_email.get(c["email"]) or {}).get("score"),
             "llm_rationale": (scores_by_email.get(c["email"]) or {}).get("rationale")}
            for c in det["top_contacts"]],
        "committees": [
            {"company": c["company"], "domain": c["domain"], "contacts": c["contacts"],
             "why": llm_why.get(c["company"]) or c["why"],
             "why_source": "llm" if llm_why.get(c["company"]) else "rules"}
            for c in det["committees"]],
        "movement": det["movement"],
        "movement_timeline": det["movement_timeline"],
        "anomalies": [
            {"contact": a.get("contact", ""), "company": a.get("company", ""),
             "metric": a["metric"], "value": a["value"], "threshold": a["threshold"],
             "evidence_rows": a["evidence_rows"],
             "llm_rationale": llm_anomaly_note.get(str(a.get("contact", "")).lower())}
            for a in det["anomalies"]],
        "anomaly_detection": det["anomaly_detection"],
        "narrative": {"text": analysis["movement_narrative"], "source": analysis["narrative_source"],
                      "generated_at": analysis["generated_at"], "model": analysis.get("model")},
        "validator": llm.get("validator", {"agreements": 0, "disagreements": []}),
        "receipts": ["receipts/m4_seed.json", "receipts/m4_hubspot_sync.json",
                     "receipts/m4_llm_calls.json"],
    }


def render_html(data: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    marker = "__DASHBOARD_DATA_JSON__"
    if marker not in template:
        raise M4Error(f"marker {marker!r} missing from {TEMPLATE_PATH}")
    blob = json.dumps(data, indent=2).replace("</", "<\\/")
    return template.replace(marker, blob)


def phase_render(ctx) -> dict:
    snapshot_path, analysis_path = ctx["out"] / "snapshot.json", ctx["out"] / "analysis.json"
    for p in (snapshot_path, analysis_path):
        if not p.exists():
            raise M4Error(f"{p} not found -- run the earlier phases first")
    snapshot, analysis = load_json(snapshot_path), load_json(analysis_path)
    event = event_identity(load_event(ctx["event_path"]), ctx["slug"])
    data = build_dashboard_data(snapshot, analysis, event)
    write_json(ctx["out"] / "dashboard_data.json", data)
    html_path = ctx["out"] / "index.html"
    html_path.write_text(render_html(data), encoding="utf-8")
    summary(ctx, "render",
            f"mql_rate {data['kpis']['mql_rate_pct']}%, {len(data['top_accounts'])} top accounts, "
            f"{len(data['committees'])} committees, {len(data['anomalies'])} anomalies, "
            f"narrative={data['narrative']['source']}, "
            f"index.html {html_path.stat().st_size // 1024} KB")
    return data


# ---------------------------------------------------------------------------
# --live-dry-run
# ---------------------------------------------------------------------------

def print_requests(plan: list) -> None:
    for i, row in enumerate(plan, 1):
        print(f"HUBSPOT {i:02d}: {row['method']} {row['url']} ({row['body_bytes']} body bytes) "
              f"-- {row['note']}")


def dry_run(ctx, phases: list) -> dict:
    """Build every live prompt and every HubSpot request from real data, print them,
    send nothing. Writes receipts/m4_dry_run.json only: no snapshot, no analysis and no
    dashboard, because none of those would have come from the portal."""
    engagement = load_json(ENGAGEMENT_FIXTURE)
    totals, stages = engagement_totals(engagement), final_stages(engagement)
    plan = {"event_slug": ctx["slug"], "lane": "live-dry-run", "generated_at": now_iso(),
            "phases": phases, "hubspot_requests": [], "prompts": []}

    per_phase = {}
    if "seed" in phases:
        requests = seed_plan(ctx["slug"], totals, stages)
        print(f"--- seed: {len(requests)} HubSpot request(s) the live lane would send ---")
        print_requests(requests)
        plan["hubspot_requests"].extend(requests)
        per_phase["seed"] = f"{len(requests)} HubSpot request(s) planned, 0 sent"

    snapshot = None
    if {"sync", "analyze", "render"} & set(phases):
        snapshot_path = ctx["out"] / "snapshot.json"
        snapshot = load_json(snapshot_path) if snapshot_path.exists() else sync_offline(ctx)

    if "sync" in phases:
        ids = "<contact ids>"
        body_bytes = len(json.dumps({"inputs": [{"id": "0"}] * BATCH_CHUNK}).encode("utf-8"))
        requests = [
            {"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/search",
             "body_bytes": len(json.dumps({"filterGroups": [{"filters": [
                 {"propertyName": EVENT_PROPERTY, "operator": "EQ", "value": ctx["slug"]}]}],
                 "properties": CONTACT_PROPERTIES, "limit": PAGE_SIZE}).encode("utf-8")),
             "note": f"contacts tagged {EVENT_PROPERTY}={ctx['slug']}, paged"},
            {"method": "POST", "url": f"{API_BASE}/crm/v3/objects/contacts/batch/read",
             "body_bytes": body_bytes, "note": f"propertiesWithHistory=lifecyclestage for {ids}"},
            {"method": "POST", "url": f"{API_BASE}/crm/v4/associations/contacts/companies/batch/read",
             "body_bytes": body_bytes, "note": "company associations"},
            {"method": "POST", "url": f"{API_BASE}/crm/v3/objects/companies/batch/read",
             "body_bytes": body_bytes, "note": "company name + domain"},
            {"method": "POST", "url": f"{API_BASE}/crm/v4/associations/contacts/emails/batch/read",
             "body_bytes": body_bytes,
             "note": f"email engagements (assoc type {EMAIL_TO_CONTACT_ASSOC_TYPE})"},
            {"method": "POST", "url": f"{API_BASE}/crm/v3/objects/emails/batch/read",
             "body_bytes": body_bytes, "note": "subject + hs_timestamp"},
        ]
        print(f"--- sync: {len(requests)} HubSpot request(s) the live lane would send ---")
        print_requests(requests)
        plan["hubspot_requests"].extend(requests)
        per_phase["sync"] = f"{len(requests)} HubSpot request(s) planned, 0 sent"

    if "analyze" in phases:
        det = deterministic_analysis(snapshot)
        event = event_identity(load_event(ctx["event_path"]), ctx["slug"])
        prompts = build_llm_prompts(det, event)
        dry_dir = ctx["out"] / "dry-run"
        dry_dir.mkdir(parents=True, exist_ok=True)
        print(f"--- analyze: {len(prompts)} prompt(s) the live lane would send "
              f"(budget {ctx['budget']}, model chain {', '.join(models_to_try())}) ---")
        for i, (purpose, prompt) in enumerate(prompts, 1):
            path = dry_dir / f"{i:02d}-{purpose}.prompt.md"
            path.write_text(prompt, encoding="utf-8")
            print(f"=== PROMPT {i}/{len(prompts)}: {purpose} ({len(prompt)} chars) -> {path} ===")
            print(prompt)
            plan["prompts"].append({"purpose": purpose, "chars": len(prompt), "path": str(path)})
        per_phase["analyze"] = (f"{len(prompts)} prompt(s) planned over "
                                f"{det['counts']['contacts']} snapshot contacts, 0 sent")

    if "render" in phases:
        print("--- render: no outbound call at build time; the page fetches "
              "${window.NARRATIVE_ENDPOINT}?refresh=1 on load only when the module API "
              "injects that endpoint (25s timeout, embedded narrative kept on failure) ---")
        per_phase["render"] = "0 build-time requests; 1 refresh-on-load fetch when an endpoint is injected"

    plan["per_phase"] = per_phase
    write_json(ctx["out"] / "receipts" / "m4_dry_run.json", plan)
    for phase in phases:
        summary(ctx, phase, per_phase.get(phase, "nothing planned"))
    if len(phases) > 1:
        summary(ctx, "all", f"{len(plan['hubspot_requests'])} HubSpot request(s) and "
                            f"{len(plan['prompts'])} prompt(s) planned, 0 sent")
    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def summary(ctx, phase: str, body: str, lane: str = None) -> None:
    print(f"M4 {phase} ({lane or ctx['lane']}): {body} -> {ctx['out']}")


def resolve_slug(args, event: dict) -> str:
    return args.event_tag or event.get("event_slug") or ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="M4 -- Lead Intelligence Dashboard (seed | sync | analyze | render | all).")
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    ap.add_argument("--event", default=str(DEFAULT_EVENT), dest="event",
                    help="event.json (name/date/host/speakers/event_slug)")
    ap.add_argument("--event-tag", dest="event_tag", default=None,
                    help=f"event slug to filter the portal's {EVENT_PROPERTY} on "
                         "(default: event.json's event_slug)")
    ap.add_argument("--offline", action="store_true", help="zero network; fixtures + rules lane")
    ap.add_argument("--live-dry-run", dest="live_dry_run", action="store_true",
                    help="print every live HubSpot request and prompt, send nothing, exit 0")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help=f"LLM call ceiling for analyze (default {DEFAULT_BUDGET})")
    args = ap.parse_args(argv)

    if args.offline and args.live_dry_run:
        print("error: --offline and --live-dry-run are different lanes; pick one", file=sys.stderr)
        return 2
    event_path = Path(args.event)
    if not event_path.exists():
        print(f"error: --event not found: {event_path}", file=sys.stderr)
        return 2
    event = load_event(event_path)
    slug = resolve_slug(args, event)
    if not slug:
        print("error: no event slug (event.json has no event_slug and --event-tag was not given)",
              file=sys.stderr)
        return 2

    lane = "offline" if args.offline else ("live-dry-run" if args.live_dry_run else "live")
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    ctx = {"out": out, "lane": lane, "slug": slug, "event_path": event_path,
           "budget": args.budget, "client": None, "seed_method": None}

    phases = ["seed", "sync", "analyze", "render"] if args.phase == "all" else [args.phase]
    try:
        if lane == "live-dry-run":
            dry_run(ctx, phases)
            return 0
        if lane == "live" and set(phases) & {"seed", "sync"}:
            token = resolve_hubspot_token()
            if not token:
                raise M4Error(f"no HUBSPOT_TOKEN (env or {HUBSPOT_ENV_PATH}). The live lane is the "
                              "default; re-run with --offline for the fixture lane or "
                              "--live-dry-run to see the requests.")
            ctx["client"] = HubSpotClient(token)
        for phase in phases:
            if phase == "seed":
                ctx["seed_method"] = phase_seed(ctx).get("method")
            elif phase == "sync":
                phase_sync(ctx)
            elif phase == "analyze":
                phase_analyze(ctx)
            elif phase == "render":
                phase_render(ctx)
        if args.phase == "all":
            summary(ctx, "all", f"{len(phases)} phases complete")
    except M4Error as e:
        print(f"M4 {args.phase} FAILED: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        print(f"M4 {args.phase} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
