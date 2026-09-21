#!/usr/bin/env python3
"""M2 -- Post-Event Communications engine. Live by default.

One LLM extraction pass over the real transcript (takeaways, verbatim quotes,
the moments a no-show missed, the per-function/per-industry angles), then one
call per segment that writes the actual email copy. Every quote and timestamp
that reaches a rendered body is verified against the transcript before the run
is allowed to finish.

Lanes
  live (default)  1 extraction call + 1 call per segment (attendee / no_show /
                  speaker) = 4, hard budget 5 (MAX_LLM_CALLS). A successful run
                  refreshes sample_output/ + .fingerprint.json so a reviewer
                  without API keys can replay exactly what the model produced.
  --offline       replays sample_output/ when its fingerprint matches the
                  transcript + event; otherwise refuses and says why.
  --live-dry-run  prints the prompts it would send. Zero network.

Writes under --out: dispatch_plan.json (the contract in docs/module-api.md
§M2), extraction.json, grounding.json, emails/<segment>.md, approval_gate.json,
comms.json, receipts/m2_llm_calls.json.

This module never sends mail. Every plan lands `pending_human_approval`; the
dispatch + HubSpot-logging path is n8n's (orchestrator/n8n/railway/README.md).

Python 3 stdlib only.

Usage:
    python3 comms.py --out out/w2/m2 --enriched out/w2/m1/hubspot_ready.csv
    python3 comms.py --out out/w2/m2 --offline
    python3 comms.py --out out/w2/m2-dry --live-dry-run
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
SHARED_DIR = REPO_ROOT / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from utm import with_utm  # shared/utm.py -- canonical impl, see its docstring  # noqa: E402

SAMPLE_DIR = MODULE_DIR / "sample_output"
FINGERPRINT_PATH = SAMPLE_DIR / ".fingerprint.json"
EXTRACTION_SAMPLE = SAMPLE_DIR / "extraction.json"
VARIANTS_SAMPLE = SAMPLE_DIR / "variants.json"

DEFAULT_EVENT = REPO_ROOT / "data" / "incoming" / "event.json"
DEFAULT_SEGMENTS = REPO_ROOT / "data" / "fixtures" / "segments.json"
DEFAULT_TRANSCRIPT = REPO_ROOT / "data" / "incoming" / "transcript.md"
DEFAULT_SPEAKERS = REPO_ROOT / "data" / "incoming" / "speakers.json"
DEFAULT_REGISTRANTS = REPO_ROOT / "data" / "incoming" / "registrants.csv"
DEFAULT_ICP = REPO_ROOT / "config" / "icp.yaml"

SEGMENTS = ("attendee", "no_show", "speaker")
PROMPT_FILES = {
    "extraction": "extraction.md",
    "attendee": "attendee.md",
    "no_show": "no_show.md",
    "speaker": "speaker.md",
}
EMAIL_FILES = {seg: f"{seg}.md" for seg in SEGMENTS}
# One demo inbox per bulk segment: registrant people are synthetic, so the demo
# dispatch redirects one real-shaped message per segment to a Kai-controlled
# alias. Speakers already carry alias addresses, so they are never redirected.
DEMO_REDIRECT = {
    "attendee": "kai8karma+attendee@gmail.com",
    "no_show": "kai8karma+noshow@gmail.com",
}

# --- LLM backends -----------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL_FALLBACKS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "anthropic/claude-sonnet-5",
]
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"
MAX_LLM_CALLS = 5          # hard budget per run; every HTTP attempt counts
DEFAULT_DEADLINE_S = 120.0  # LLM_BATCH_DEADLINE_S: per-call wall clock

FUNCTIONS = ["marketing", "revops", "sales", "executive", "customer_success", "general"]
FUNCTION_RULES = [
    ("customer_success", re.compile(r"customer success|client success", re.I)),
    ("revops", re.compile(r"revops|revenue operations|people analytics|hris", re.I)),
    ("marketing", re.compile(r"marketing|growth|demand gen|content", re.I)),
    ("sales", re.compile(r"\bsales\b|account executive|business development|\bbdr\b|\bsdr\b", re.I)),
    ("executive", re.compile(r"\bceo\b|\bcfo\b|\bcoo\b|\bcto\b|\bchro\b|chief\s|founder|\bpresident\b", re.I)),
]
# Second personalization axis. Buckets follow the raw `industry` values M1
# actually emits (SaaS / IT Services / Ecommerce / Fintech / Other / Unknown):
# IT Services gets its own bucket, everything else rolls into one commercial
# bucket rather than several near-empty ones.
INDUSTRIES = ["saas", "services_it", "other_commercial"]
INDUSTRY_RULES = [
    ("saas", re.compile(r"\bsaas\b|software", re.I)),
    ("services_it", re.compile(r"\bit services\b|information technology|consulting", re.I)),
]

FIELD_ALIASES = {
    "email": ["email"],
    "first_name": ["firstname", "first_name", "first name"],
    "last_name": ["lastname", "last_name", "last name"],
    "job_title": ["jobtitle", "job_title", "title", "job title"],
    "company": ["company", "company_name", "company name"],
    "icp_tier": ["icp_tier", "icptier", "tier"],
    "function": ["function", "buyer_function"],
    "industry": ["industry"],
    "attendance_status": ["attendance_status"],
    "suppression_reason": ["suppression_reason"],
    "hubspot_contact_id": ["hubspot_contact_id", "hs_object_id", "contact_id"],
    "minutes": ["time_in_session_minutes", "time in session (minutes)", "minutes"],
}

UTM_SOURCE = "webinar"
UTM_MEDIUM = "email"

TS_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
QUOTE_RE = re.compile(r"[\"“”«»]([^\"“”«»]{8,400})[\"“”«»]")
TOKEN_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}")


# ---------------------------------------------------------------------------
# inputs
def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def classify_function(job_title: str) -> str:
    title = (job_title or "").strip()
    for func, pattern in FUNCTION_RULES:
        if pattern.search(title):
            return func
    return "general"


def classify_industry(industry: str) -> str:
    value = (industry or "").strip()
    for bucket, pattern in INDUSTRY_RULES:
        if pattern.search(value):
            return bucket
    return "other_commercial"


def load_icp_tiers(icp_path: Path) -> dict:
    """Targeted extractor for this repo's icp.yaml tier/titles shape -- not a
    general YAML parser (stdlib-only rule). Unused when --enriched carries an
    icp_tier column, which M1 always does."""
    if not icp_path.exists():
        return {}
    text = icp_path.read_text(encoding="utf-8")
    tiers = {}
    for tier in ("tier1", "tier2"):
        m = re.search(rf"{tier}:\s*\n\s*titles:\s*\[(.*?)\]", text, re.S)
        if m:
            titles = [t.strip().strip('"').strip("'") for t in m.group(1).split(",")]
            tiers[tier] = {t.lower() for t in titles if t}
    return tiers


def classify_icp_tier(job_title: str, tiers: dict) -> str:
    title = (job_title or "").strip().lower()
    if title and title in tiers.get("tier1", set()):
        return "tier1"
    if title and title in tiers.get("tier2", set()):
        return "tier2"
    return "tier3"


def _get_aliased(row: dict, key: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in FIELD_ALIASES[key]:
        val = lowered.get(alias)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def load_enriched(path: Path) -> dict:
    """M1's HubSpot-ready output, keyed by lowercased email. CSV (spec'd) or
    JSON -- whichever M1 hands over."""
    by_email = {}
    if path.suffix.lower() == ".json":
        data = load_json(path)
        rows = data if isinstance(data, list) else data.get("contacts", [])
    else:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    for row in rows:
        email = _get_aliased(row, "email")
        if not email:
            continue
        by_email[email.lower()] = {
            "email": email,
            "first_name": _get_aliased(row, "first_name"),
            "company": _get_aliased(row, "company"),
            "job_title": _get_aliased(row, "job_title"),
            "icp_tier": _get_aliased(row, "icp_tier") or None,
            "function": _get_aliased(row, "function") or None,
            "industry": _get_aliased(row, "industry") or None,
            "hubspot_contact_id": _get_aliased(row, "hubspot_contact_id") or None,
            "minutes": _get_aliased(row, "minutes"),
            "attendance_status": _get_aliased(row, "attendance_status").lower(),
            "suppression_reason": _get_aliased(row, "suppression_reason"),
        }
    return by_email


def load_registrants(path: Path) -> dict:
    """Fallback contact source when M1's enriched file isn't available."""
    by_email = {}
    if not path.exists():
        return by_email
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            email = (row.get("Email") or "").strip()
            if not email:
                continue
            by_email[email.lower()] = {
                "first_name": (row.get("First Name") or "").strip(),
                "company": (row.get("Company") or "").strip(),
                "job_title": (row.get("Job Title") or "").strip(),
                "attended": (row.get("Attended") or "").strip().lower() == "yes",
                "minutes": (row.get("Time in Session (minutes)") or "").strip(),
            }
    return by_email


def segment_from_enriched(enriched_by_email: dict) -> tuple:
    """(attendee_emails, no_show_emails, suppressed) off M1's row set -- which
    already reflects M1's dedupe (a merged-away address has no row, so it can
    never be mailed) and M1's suppression flags. Sorted for determinism."""
    attendee, no_show, suppressed = [], [], []
    for rec in enriched_by_email.values():
        status = rec.get("attendance_status", "")
        if status not in ("attended", "no_show"):
            continue
        reason = rec.get("suppression_reason", "")
        if reason:
            suppressed.append({"email": rec["email"], "attendance_status": status, "reason": reason})
            continue
        (attendee if status == "attended" else no_show).append(rec["email"])
    return sorted(attendee), sorted(no_show), sorted(suppressed, key=lambda s: s["email"])


def build_contacts(emails: list, enriched_by_email: dict, registrants_by_email: dict, icp_tiers: dict) -> list:
    contacts = []
    for raw_email in emails:
        key = raw_email.strip().lower()
        base = enriched_by_email.get(key) or registrants_by_email.get(key) or {}
        job_title = base.get("job_title") or ""
        first_name = base.get("first_name") or raw_email.split("@")[0].split(".")[0].title()
        contacts.append({
            "email": raw_email,
            "first_name": first_name,
            "company": base.get("company") or "",
            "job_title": job_title,
            "function": base.get("function") or classify_function(job_title),
            "icp_tier": base.get("icp_tier") or classify_icp_tier(job_title, icp_tiers),
            "industry_bucket": classify_industry(base.get("industry") or ""),
            "hubspot_contact_id": base.get("hubspot_contact_id") or None,
        })
    return contacts


def match_speaker_emails(event_speakers: list, speaker_emails: list) -> list:
    """Speaker -> inbox. The explicit `email` field on the speaker record wins
    (event.json / speakers.json carry demo-routed aliases); only a speaker
    without one falls back to the old rule of matching an address in
    segments.json by name localpart, then by company-domain substring."""
    matched = []
    for sp in event_speakers:
        email = str(sp.get("email") or "").strip()
        if not email:
            name_parts = [re.sub(r"[^a-z0-9]", "", p.lower()) for p in str(sp.get("name", "")).split()]
            for em in speaker_emails:
                local = re.sub(r"[^a-z0-9]", "", em.split("@")[0].lower())
                if local and any(p and p in local for p in name_parts):
                    email = em
                    break
        if not email:
            company_key = re.sub(r"[^a-z0-9]", "", str(sp.get("company", "")).lower())
            for em in speaker_emails:
                domain_key = re.sub(r"[^a-z0-9]", "", em.split("@")[-1].split(".")[0].lower())
                if domain_key and company_key and domain_key in company_key:
                    email = em
                    break
        matched.append({**sp, "email": email})
    return matched


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-")


def event_slug(event: dict) -> str:
    name = event["event_name"].split(":", 1)[0]
    return f"{slugify(name)}-{event['date']}"


def event_close_ts(event: dict) -> str:
    """event.json date + start_time_local + duration_min, in event.json's own
    timezone -- the timestamp n8n's 24-hour SLA is measured from."""
    start = str(event.get("start_time_local") or "00:00")
    naive = datetime.strptime(f"{event['date']} {start}", "%Y-%m-%d %H:%M")
    tz_name = str(event.get("timezone") or "UTC")
    tzinfo = timezone.utc
    try:
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        print(f"WARNING: timezone {tz_name!r} unavailable -- event_close_ts computed in UTC", file=sys.stderr)
    close = naive.replace(tzinfo=tzinfo) + timedelta(minutes=int(event.get("duration_min") or 0))
    return close.isoformat()


# ---------------------------------------------------------------------------
# fingerprint / offline replay
def compute_fingerprint(transcript_path: Path, event_path: Path) -> dict:
    transcript_bytes = transcript_path.read_bytes() if transcript_path.exists() else b""
    event_name = ""
    if event_path.exists():
        try:
            event_name = json.loads(event_path.read_text(encoding="utf-8")).get("event_name", "")
        except json.JSONDecodeError:
            event_name = ""
    return {
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "event_name": event_name,
    }


def load_replay(transcript_path: Path, event_path: Path) -> tuple:
    """--offline: replay sample_output/ only if it was generated from exactly
    this transcript + event. Any mismatch fails loud with the reason."""
    current = compute_fingerprint(transcript_path, event_path)
    missing = [p.name for p in (FINGERPRINT_PATH, EXTRACTION_SAMPLE, VARIANTS_SAMPLE) if not p.exists()]
    if missing:
        raise RuntimeError(
            f"--offline cannot replay: sample_output/ is missing {', '.join(missing)} -- "
            "nothing was ever generated for this event. Run live (drop --offline) with an "
            "OpenRouter key to generate it."
        )
    stored = load_json(FINGERPRINT_PATH)
    reasons = []
    if stored.get("transcript_sha256") != current["transcript_sha256"]:
        reasons.append(
            f"transcript sha256 {str(stored.get('transcript_sha256'))[:12]}... (cached) != "
            f"{current['transcript_sha256'][:12]}... ({transcript_path})"
        )
    if stored.get("event_name") != current["event_name"]:
        reasons.append(f"event_name {stored.get('event_name')!r} (cached) != {current['event_name']!r}")
    if reasons:
        raise RuntimeError(
            "--offline cannot replay: sample_output/ was generated from different inputs -- "
            + "; ".join(reasons)
            + ". Run live (drop --offline) to regenerate."
        )
    return load_json(EXTRACTION_SAMPLE), load_json(VARIANTS_SAMPLE), stored


# ---------------------------------------------------------------------------
# grounding
def _normalize_for_match(text: str) -> str:
    text = (text or "").lower().replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"').replace("—", " ").replace("–", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class Grounder:
    """Every quote and every [MM:SS] that reaches a rendered body must exist in
    the transcript. Quote match is normalized (case + punctuation ignored);
    timestamps must appear verbatim. Proper nouns from event.json (event name,
    speaker names, companies) are allowed because they are inputs, not claims
    extracted from the recording."""

    def __init__(self, transcript_text: str, allowed_extra=()):
        self.transcript_norm = _normalize_for_match(transcript_text)
        self.timestamps = set(TS_RE.findall(transcript_text))
        self.allowed = {_normalize_for_match(a) for a in allowed_extra if a}
        self.checks = []

    def _add(self, scope, field, kind, value, ok, reason=""):
        self.checks.append({
            "scope": scope, "field": field, "kind": kind,
            "value": value[:160], "pass": bool(ok), "reason": reason,
        })
        return ok

    def check_quote_text(self, scope, field, text) -> bool:
        norm = _normalize_for_match(text)
        if not norm:
            return self._add(scope, field, "quote", str(text), False, "empty quote")
        ok = norm in self.transcript_norm or any(norm in a or a in norm for a in self.allowed)
        return self._add(scope, field, "quote", text, ok, "" if ok else "not found in transcript")

    def check_timestamp(self, scope, field, ts) -> bool:
        ts = str(ts).strip().strip("[]")
        ok = ts in self.timestamps
        return self._add(scope, field, "timestamp", ts, ok, "" if ok else "no such timestamp in transcript")

    def probe(self, text, ts) -> bool:
        """Same test as check_quote_text/check_timestamp, but records nothing --
        used to decide whether an extracted quote is usable at all."""
        norm = _normalize_for_match(text)
        found = bool(norm) and (norm in self.transcript_norm or any(norm in a or a in norm for a in self.allowed))
        return found and str(ts).strip().strip("[]") in self.timestamps

    def check_text(self, scope, field, text) -> bool:
        """Scan any generated prose: quoted spans must be verbatim, [MM:SS]
        anchors must be real."""
        ok = True
        for quoted in QUOTE_RE.findall(text or ""):
            ok = self.check_quote_text(scope, field, quoted) and ok
        for ts in TS_RE.findall(text or ""):
            ok = self.check_timestamp(scope, field, ts) and ok
        return ok

    def failures(self):
        return [c for c in self.checks if not c["pass"]]

    def report(self, transcript_sha: str, dropped: list, status: str) -> dict:
        return {
            "transcript_sha256": transcript_sha,
            "transcript_timestamps": len(self.timestamps),
            "checks_total": len(self.checks),
            "passed": sum(1 for c in self.checks if c["pass"]),
            "failed": len(self.failures()),
            "quotes_dropped_at_extraction": dropped,
            "status": status,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# LLM plumbing (receipts + per-call deadline + hard call budget)
LLM_CALLS = []


def _deadline_s() -> float:
    raw = os.environ.get("LLM_BATCH_DEADLINE_S", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_DEADLINE_S
    except ValueError:
        value = DEFAULT_DEADLINE_S
    return max(5.0, value)


def _budget_left() -> int:
    return MAX_LLM_CALLS - len(LLM_CALLS)


def _check_budget(purpose: str) -> None:
    if _budget_left() <= 0:
        raise RuntimeError(
            f"LLM call budget exhausted ({len(LLM_CALLS)}/{MAX_LLM_CALLS}) before {purpose} -- "
            "see receipts/m2_llm_calls.json for what was spent"
        )


def _record(backend, model, purpose, prompt_chars, latency_ms, http_status, parse_ok, error=None) -> dict:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model,
        "purpose": purpose,
        "prompt_chars": prompt_chars,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "parse_ok": parse_ok,
    }
    if error:
        entry["error"] = str(error)[:300]
    LLM_CALLS.append(entry)
    return entry


def _openrouter_key() -> str:
    """OPENROUTER_API_KEY env var, else a KEY=VALUE line in
    ~/.config/postevent/llm.env. Never logged or printed."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if OPENROUTER_CONFIG_PATH.exists():
        for line in OPENROUTER_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OPENROUTER_API_KEY":
                return v.strip().strip('"').strip("'")
    return ""


class _OpenRouterModelError(RuntimeError):
    """This model id failed (rejected, rate-limited, or past the deadline) --
    the caller moves on to the next id in OPENROUTER_MODEL."""


def _strip_json_fence(text: str) -> str:
    """First complete JSON value in the output. Tolerates markdown fences, a
    preamble, and trailing prose (reasoning models add both)."""
    if text is None:
        raise ValueError("LLM returned empty content")
    text = text.strip()
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
    raise ValueError("no JSON value found in LLM output")


def _openrouter_models_to_try() -> list:
    env_models = [m.strip() for m in os.environ.get("OPENROUTER_MODEL", "").split(",") if m.strip()]
    if env_models:
        return env_models + [m for m in OPENROUTER_MODEL_FALLBACKS if m not in env_models]
    return list(OPENROUTER_MODEL_FALLBACKS)


def _openrouter_request(key: str, model: str, prompt: str, purpose: str) -> str:
    """One model, one wall-clock deadline (LLM_BATCH_DEADLINE_S). A hung or
    rate-limited request is abandoned at the deadline and reported as a model
    error so the caller tries the next id. Every HTTP attempt costs budget and
    lands in the receipts file."""
    # max_tokens is also what OpenRouter's affordability check bills against,
    # so it is tunable (LLM_MAX_TOKENS) -- a near-empty key 402s on a large
    # reservation it would never have spent.
    try:
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "").strip() or 8000)
    except ValueError:
        max_tokens = 8000
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    deadline = _deadline_s()
    started = time.monotonic()
    last_err = None
    for attempt in (1, 2):
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 2:
            raise _OpenRouterModelError(
                f"{model!r}: abandoned after {deadline:.0f}s deadline (LLM_BATCH_DEADLINE_S); last error: {last_err}"
            )
        _check_budget(purpose)
        if attempt > 1:  # one short backoff before a retry, never past the deadline
            time.sleep(min(5.0, max(0.0, remaining - 2)))
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://kai8karma.github.io/agentkai/",
                "X-Title": "Post-Event Engine",
            },
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                status = getattr(resp, "status", 200)
                data = json.loads(resp.read().decode("utf-8"))
            latency = int((time.monotonic() - t0) * 1000)
            # OpenRouter returns provider/rate-limit errors as HTTP 200 with an
            # {"error": ...} body and no choices -- common on free-tier models.
            if isinstance(data, dict) and data.get("error"):
                err = data["error"] or {}
                msg = f"provider error {err.get('code')}: {str(err.get('message'))[:200]}"
                _record("openrouter", model, purpose, len(prompt), latency, status, False, msg)
                last_err = msg
                continue
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                fr = (data.get("choices") or [{}])[0].get("finish_reason")
                msg = f"empty content (finish_reason={fr})"
                _record("openrouter", model, purpose, len(prompt), latency, status, False, msg)
                raise _OpenRouterModelError(f"{model!r}: {msg}")
            _record("openrouter", model, purpose, len(prompt), latency, status, False)
            return content
        except urllib.error.HTTPError as e:
            latency = int((time.monotonic() - t0) * 1000)
            body = e.read().decode("utf-8", errors="replace")[:300]
            _record("openrouter", model, purpose, len(prompt), latency, e.code, False, body)
            if e.code in (400, 401, 402, 403, 404):  # rejected/unaffordable: next model, no retry
                raise _OpenRouterModelError(f"{model!r} rejected (HTTP {e.code}): {body}") from None
            last_err = f"HTTP {e.code}: {body}"
            continue
        except (socket.timeout, TimeoutError) as e:
            latency = int((time.monotonic() - t0) * 1000)
            _record("openrouter", model, purpose, len(prompt), latency, None, False, f"deadline: {e}")
            raise _OpenRouterModelError(
                f"{model!r}: no response within the {deadline:.0f}s deadline (LLM_BATCH_DEADLINE_S)"
            ) from None
        except urllib.error.URLError as e:
            latency = int((time.monotonic() - t0) * 1000)
            _record("openrouter", model, purpose, len(prompt), latency, None, False, str(e.reason))
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise _OpenRouterModelError(
                    f"{model!r}: no response within the {deadline:.0f}s deadline (LLM_BATCH_DEADLINE_S)"
                ) from None
            raise _OpenRouterModelError(f"{model!r}: request failed: {e.reason}") from None
    raise _OpenRouterModelError(f"{model!r}: exhausted attempts within deadline; last error: {last_err}")


def _call_openrouter(prompt: str, purpose: str) -> str:
    key = _openrouter_key()
    if not key:
        raise RuntimeError(
            "OpenRouter API key not found (set OPENROUTER_API_KEY or ~/.config/postevent/llm.env)"
        )
    last_err = None
    for model in _openrouter_models_to_try():
        try:
            return _openrouter_request(key, model, prompt, purpose)
        except _OpenRouterModelError as e:
            print(f"[warn] {purpose}: {e}", file=sys.stderr)
            last_err = e
            continue
    raise RuntimeError(f"openrouter: all candidate models failed for {purpose}: {last_err}")


def _call_claude_cli(prompt: str, purpose: str) -> str:
    """`claude -p` backend. USER must not propagate into the child process or
    keychain auth 401s (documented gotcha, see docs/architecture.md's
    "How it runs today" section)."""
    _check_budget(purpose)
    env = os.environ.copy()
    env.pop("USER", None)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt], capture_output=True, text=True, env=env,
            timeout=_deadline_s(), stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        _record("claude", "claude-cli", purpose, len(prompt), 0, None, False, "claude CLI not on PATH")
        raise RuntimeError("`claude` CLI not found on PATH") from None
    except subprocess.TimeoutExpired:
        latency = int((time.monotonic() - t0) * 1000)
        _record("claude", "claude-cli", purpose, len(prompt), latency, None, False, "deadline exceeded")
        raise RuntimeError(f"claude -p exceeded the {_deadline_s():.0f}s deadline for {purpose}") from None
    latency = int((time.monotonic() - t0) * 1000)
    if proc.returncode != 0:
        _record("claude", "claude-cli", purpose, len(prompt), latency, None, False, proc.stderr.strip()[:300])
        raise RuntimeError(f"claude -p failed for {purpose}: {proc.stderr.strip()[:300]}")
    _record("claude", "claude-cli", purpose, len(prompt), latency, None, False)
    return proc.stdout.strip()


def _llm_call(prompt: str, purpose: str) -> str:
    """One live call through LLM_BACKEND (openrouter | claude | auto). Default:
    openrouter when a key is present, else the claude CLI. Fails loud."""
    backend = os.environ.get("LLM_BACKEND", "").strip().lower()
    if backend not in ("auto", "claude", "openrouter"):
        backend = "openrouter" if _openrouter_key() else "claude"
    if backend == "claude":
        return _call_claude_cli(prompt, purpose)
    if backend == "openrouter":
        return _call_openrouter(prompt, purpose)
    try:
        return _call_claude_cli(prompt, purpose)
    except RuntimeError as claude_err:
        try:
            return _call_openrouter(prompt, purpose)
        except RuntimeError as e:
            raise RuntimeError(f"claude -p failed ({claude_err}); openrouter also failed: {e}") from e


def llm_json(prompt: str, purpose: str, validate) -> dict:
    """Call, parse JSON, validate. One corrective retry if the budget allows --
    the model is told exactly what was wrong. Never returns unvalidated copy."""
    attempt_prompt = prompt
    last_err = None
    for attempt in (1, 2):
        raw = _llm_call(attempt_prompt, purpose if attempt == 1 else f"{purpose}_retry")
        try:
            parsed = json.loads(_strip_json_fence(raw))
            if not isinstance(parsed, dict):
                raise ValueError(f"top-level JSON is {type(parsed).__name__}, expected object")
            parsed = validate(parsed)
            LLM_CALLS[-1]["parse_ok"] = True
            return parsed
        except (ValueError, KeyError) as e:
            last_err = e
            LLM_CALLS[-1]["parse_ok"] = False
            LLM_CALLS[-1]["error"] = str(e)[:300]
            if attempt == 2 or _budget_left() <= 0:
                break
            print(f"[warn] {purpose}: rejected ({e}); retrying once", file=sys.stderr)
            attempt_prompt = (
                f"{prompt}\n\n## Correction required\n"
                f"Your previous answer was rejected: {e}\n"
                "Return ONLY the corrected JSON object. No prose, no markdown fence."
            )
    raise RuntimeError(f"{purpose}: model output unusable: {last_err}")


# ---------------------------------------------------------------------------
# prompts
def _prompt_text(name: str) -> str:
    return (MODULE_DIR / "prompts" / PROMPT_FILES[name]).read_text(encoding="utf-8").strip()


def _event_facts(event: dict, speakers: list) -> dict:
    return {
        "event_name": event["event_name"],
        "date": event["date"],
        "host_company": event.get("host_company", ""),
        "topic": event.get("topic", ""),
        "audience": event.get("audience", ""),
        "duration_min": event.get("duration_min"),
        "speakers": [
            {"name": s.get("name"), "title": s.get("title"), "company": s.get("company"), "role": s.get("role")}
            for s in speakers
        ],
    }


def build_extraction_prompt(event: dict, speakers: list, transcript_text: str, function_counts: dict,
                            industry_counts: dict) -> str:
    context = {
        "event": _event_facts(event, speakers),
        "functions_to_cover": FUNCTIONS,
        "functions_in_audience": function_counts,
        "industry_buckets_to_cover": INDUSTRIES,
        "industry_buckets_in_audience": industry_counts,
    }
    return (
        f"{_prompt_text('extraction')}\n\n"
        f"## Context\n\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
        f"## Transcript\n\n{transcript_text}\n"
    )


def _extraction_for_segment(extraction: dict, segment: str, speakers=None) -> dict:
    """The segment calls never see the transcript -- only the verified material
    the extraction call produced (that is what keeps the run inside 5 calls and
    the copy inside the grounding rule)."""
    trimmed = {
        "premise": extraction.get("premise", ""),
        "takeaways": extraction.get("takeaways", []),
        "quotes": extraction.get("quotes", []),
    }
    if segment == "no_show":
        trimmed["no_show_moments"] = extraction.get("no_show_moments", [])
    if segment == "speaker":
        trimmed["quotes_by_speaker"] = {
            sp["name"]: [q for q in extraction.get("quotes", []) if q.get("speaker") == sp["name"]]
            for sp in (speakers or [])
        }
    return trimmed


def build_segment_prompt(segment: str, event: dict, speakers: list, extraction: dict, snapshot=None) -> str:
    context = {
        "event": _event_facts(event, speakers),
        "extraction": _extraction_for_segment(extraction, segment, speakers),
    }
    if segment == "speaker":
        context["performance_snapshot"] = snapshot
    return (
        f"{_prompt_text(segment)}\n\n"
        f"## Context\n\n```json\n{json.dumps(context, indent=2)}\n```\n"
    )


# ---------------------------------------------------------------------------
# validation of model output
def make_extraction_validator(speaker_names):
    """The speaker email is built from that speaker's own lines, so an extraction
    that quotes only the loudest panelist is rejected (one retry is budgeted)."""
    def validate_extraction(data: dict) -> dict:
        takeaways = []
        for t in data.get("takeaways") or []:
            if not isinstance(t, dict):
                continue
            headline, body, ts = str(t.get("headline", "")).strip(), str(t.get("body", "")).strip(), str(t.get("timestamp", "")).strip().strip("[]")
            if headline and body and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", ts):
                takeaways.append({"headline": headline, "body": body, "timestamp": ts})
        if not 5 <= len(takeaways) <= 7:
            raise ValueError(f"need 5-7 takeaways with a headline, body and MM:SS timestamp; got {len(takeaways)}")
        quotes = []
        for q in data.get("quotes") or []:
            if not isinstance(q, dict):
                continue
            text, speaker, ts = str(q.get("text", "")).strip().strip('"'), str(q.get("speaker", "")).strip(), str(q.get("timestamp", "")).strip().strip("[]")
            if text and speaker and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", ts):
                quotes.append({"speaker": speaker, "timestamp": ts, "text": text})
        if not 4 <= len(quotes) <= 6:
            raise ValueError(f"need 4-6 quotes with speaker + MM:SS timestamp + verbatim text; got {len(quotes)}")

        missing_speakers = [n for n in speaker_names
                            if not any(q["speaker"].lower() == n.lower() for q in quotes)]
        if missing_speakers:
            raise ValueError(
                f"quotes must include at least one verbatim line spoken by each named speaker; "
                f"none for {missing_speakers} (got {sorted({q['speaker'] for q in quotes})})")
        moments = []
        for m in data.get("no_show_moments") or []:
            if not isinstance(m, dict):
                continue
            ts, why = str(m.get("timestamp", "")).strip().strip("[]"), str(m.get("why", "")).strip()
            label = str(m.get("label", "")).strip()
            if why and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", ts):
                moments.append({"timestamp": ts, "label": label, "why": why})
        if len(moments) != 3:
            raise ValueError(f"need exactly 3 no_show_moments with an MM:SS timestamp; got {len(moments)}")
        by_function = {}
        raw_bf = data.get("by_function") or {}
        for func in FUNCTIONS:
            tk = raw_bf.get(func)
            if isinstance(tk, dict) and str(tk.get("body", "")).strip():
                by_function[func] = {
                    "headline": str(tk.get("headline", "")).strip(),
                    "body": str(tk["body"]).strip(),
                }
        if "general" not in by_function:
            raise ValueError(f"by_function must cover every function {FUNCTIONS}; missing at least 'general' (got {sorted(by_function)})")
        for func in FUNCTIONS:
            by_function.setdefault(func, by_function["general"])
        by_industry = {}
        raw_bi = data.get("by_industry") or {}
        for bucket in INDUSTRIES:
            val = str(raw_bi.get(bucket, "")).strip()
            if val:
                by_industry[bucket] = val
        if "other_commercial" not in by_industry:
            raise ValueError(f"by_industry must cover {INDUSTRIES}; missing at least 'other_commercial' (got {sorted(by_industry)})")
        for bucket in INDUSTRIES:
            by_industry.setdefault(bucket, by_industry["other_commercial"])
        premise = str(data.get("premise", "")).strip()
        if not premise:
            raise ValueError("premise is required (one line)")
        return {
            "premise": premise,
            "takeaways": takeaways,
            "quotes": quotes,
            "no_show_moments": moments,
            "by_function": by_function,
            "by_industry": by_industry,
        }
    return validate_extraction

REQUIRED_TOKENS = {
    "attendee": ["firstname", "takeaway_headline", "takeaway_body", "recording_link", "cta_link"],
    "no_show": ["firstname", "takeaway_headline", "takeaway_body", "recording_link", "cta_link"],
    "speaker": ["firstname", "recording_link", "cta_link", "snapshot_your_quotes"],
}
SNAPSHOT_TOKENS = [
    "snapshot_registrants", "snapshot_attendees", "snapshot_attendance_rate", "snapshot_avg_minutes",
    "snapshot_median_minutes", "snapshot_no_shows", "snapshot_top_accounts", "snapshot_your_quotes",
]


def _allowed_tokens(segment: str) -> set:
    allowed = {"firstname", "recording_link", "cta_link", "takeaway_headline", "takeaway_body"}
    if segment == "speaker":
        allowed |= set(SNAPSHOT_TOKENS)
        allowed -= {"takeaway_headline", "takeaway_body"}
    return allowed


def make_variant_validator(segment: str):
    def validate(data: dict) -> dict:
        out = {}
        for key in ("subject_a", "subject_b", "preheader", "body_md"):
            val = str(data.get(key, "")).strip()
            if not val:
                raise ValueError(f"missing {key}")
            out[key] = val
        if out["subject_a"] == out["subject_b"]:
            raise ValueError("subject_a and subject_b must be different lines (they are an A/B pair)")
        takeaways = [str(t).strip() for t in (data.get("takeaways") or []) if str(t).strip()]
        if not 3 <= len(takeaways) <= 5:
            raise ValueError(f"takeaways must be a list of 3-5 short strings; got {len(takeaways)}")
        out["takeaways"] = takeaways
        found = set(TOKEN_RE.findall(out["body_md"]))
        missing = [t for t in REQUIRED_TOKENS[segment] if t not in found]
        if missing:
            raise ValueError(f"body_md is missing merge fields: {['{{%s}}' % m for m in missing]}")
        unknown = sorted(found - _allowed_tokens(segment))
        if unknown:
            raise ValueError(f"body_md uses merge fields this engine cannot fill: {unknown}")
        if segment == "speaker":
            snapshot_used = [t for t in found if t.startswith("snapshot_")]
            if len(snapshot_used) < 4:
                raise ValueError(
                    f"speaker body_md must carry the performance snapshot: used {len(snapshot_used)} "
                    f"of {SNAPSHOT_TOKENS}"
                )
        words = len(re.findall(r"\w+", out["body_md"]))
        if words > 400:
            raise ValueError(f"body_md is {words} words; keep it under 400")
        return out
    return validate


# ---------------------------------------------------------------------------
# personalization + snapshot
def resolve_takeaway(extraction: dict, function: str, industry_bucket: str) -> dict:
    """Role takeaway is primary, industry angle is appended as a second
    sentence -- the same resolution HubSpot would do per contact at send time
    from by_function + by_industry."""
    by_function = extraction["by_function"]
    tk = by_function.get(function, by_function["general"])
    angle = extraction["by_industry"].get(industry_bucket, extraction["by_industry"]["other_commercial"])
    body = f"{tk['body']} {angle}".strip() if angle else tk["body"]
    return {"headline": tk["headline"], "body": body, "industry_angle": angle}


def compute_snapshot(enriched_by_email: dict, segments: dict, registrants_by_email: dict) -> dict:
    """Speaker performance snapshot -- real numbers off M1's enriched rows when
    they exist (they carry attendance_status, minutes and company), else off
    segments.json + registrants.csv."""
    rows = [r for r in enriched_by_email.values() if r.get("attendance_status") in ("attended", "no_show")]
    if rows:
        attended = [r for r in rows if r["attendance_status"] == "attended"]
        minutes = []
        for r in attended:
            try:
                minutes.append(float(r.get("minutes") or ""))
            except ValueError:
                continue
        companies = Counter((r.get("company") or "unknown").strip() for r in attended if (r.get("company") or "").strip())
        source = "M1 enriched rows"
        registrants, attendees = len(rows), len(attended)
    else:
        registrants = len(segments["attendees"]) + len(segments["no_shows"])
        attendees = len(segments["attendees"])
        minutes, companies = [], Counter()
        for email in segments["attendees"]:
            rec = registrants_by_email.get(email.lower(), {})
            try:
                minutes.append(float(rec.get("minutes") or ""))
            except ValueError:
                pass
            if (rec.get("company") or "").strip():
                companies[rec["company"].strip()] += 1
        source = "data/fixtures/segments.json + registrants.csv"
    return {
        "registrants": registrants,
        "attendees": attendees,
        "no_shows": registrants - attendees,
        "attendance_rate": round(attendees / registrants, 3) if registrants else 0.0,
        "avg_minutes": round(statistics.mean(minutes), 1) if minutes else 0.0,
        "median_minutes": round(statistics.median(minutes), 1) if minutes else 0.0,
        "top_accounts": [{"company": c, "attendees": n} for c, n in companies.most_common(5)],
        "source": source,
    }


def _speaker_quote_block(quotes: list) -> str:
    if not quotes:
        return "_No verbatim line of yours cleared the transcript check, so this email carries none._"
    return "\n".join(f'> "{q["text"]}" -- [{q["timestamp"]}]' for q in quotes[:3])


def snapshot_fields(snapshot: dict, speaker_quotes: list) -> dict:
    top = ", ".join(f"{a['company']} ({a['attendees']})" for a in snapshot["top_accounts"]) or "n/a"
    return {
        "snapshot_registrants": str(snapshot["registrants"]),
        "snapshot_attendees": str(snapshot["attendees"]),
        "snapshot_attendance_rate": f"{round(100 * snapshot['attendance_rate'])}%",
        "snapshot_avg_minutes": str(snapshot["avg_minutes"]),
        "snapshot_median_minutes": str(snapshot["median_minutes"]),
        "snapshot_no_shows": str(snapshot["no_shows"]),
        "snapshot_top_accounts": top,
        "snapshot_your_quotes": _speaker_quote_block(speaker_quotes),
    }


THIRD_PERSON_ATTRIBUTION_VERBS = r"made|shared|walked|said|noted|argued|pointed out|mentioned|added"


def lint_no_third_person_self(name: str, body: str) -> None:
    """Regression guard for the speaker-email bug: a recipient's own mail must
    never attribute their own point to them in third person ("Sudi made the
    point..." landing in Sudi's inbox). Own contributions are second-person."""
    first_name = name.split()[0]
    pattern = re.compile(rf"\b{re.escape(first_name)}\s+({THIRD_PERSON_ATTRIBUTION_VERBS})\b", re.I)
    m = pattern.search(body)
    if m:
        raise RuntimeError(
            f"speaker lint failed for {name}: their own email refers to them in third person "
            f"({m.group(0)!r}) -- own contributions must read as 'you'"
        )


def personalization_preview(contacts: list, extraction: dict) -> str:
    """Draft-time proof that per-role and per-industry personalization is real
    and grounded. Not part of any sent message -- the merge fields above are
    resolved per contact in dispatch_plan.json's recipients[]."""
    func_counts = Counter(c["function"] for c in contacts)
    industry_counts = Counter(c["industry_bucket"] for c in contacts)
    lines = ["", "---", "", "## Personalization preview (draft-time only)", "",
             "_`{{takeaway_headline}}` / `{{takeaway_body}}` above resolve per contact from two axes: "
             "role (function) is the primary takeaway, the industry angle is appended as a second "
             "sentence. Every recipient's resolved copy is in `dispatch_plan.json`._", "",
             "### Role -- primary takeaway", ""]
    for func in FUNCTIONS:
        n = func_counts.get(func, 0)
        if n == 0:
            continue
        tk = extraction["by_function"].get(func, extraction["by_function"]["general"])
        lines += [f"**{func}** ({n} of {len(contacts)} contacts)", f"> **{tk['headline']}** {tk['body']}", ""]
    lines += ["### Industry -- second-sentence angle", ""]
    for bucket in INDUSTRIES:
        n = industry_counts.get(bucket, 0)
        if n == 0:
            continue
        lines += [f"**{bucket}** ({n} of {len(contacts)} contacts)",
                  f"> {extraction['by_industry'].get(bucket, '')}", ""]
    lines += ["### Resolved sample -- both axes, as the contact receives it", ""]
    for c in contacts[:5]:
        resolved = resolve_takeaway(extraction, c["function"], c["industry_bucket"])
        lines.append(f"- **{c['first_name']}** ({c['function']} / {c['industry_bucket']}): {resolved['body']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rendering
def _inline_md(text: str) -> str:
    out = html.escape(text, quote=False)
    # The URL was escaped by the pass above (utm params carry &) -- only the
    # attribute delimiter still needs handling, or hrefs come out &amp;amp;.
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                 lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    # Bare URLs (copy that writes "Watch it here: <url>" instead of a markdown
    # link) still have to be clickable. Already-linked ones sit behind href=".
    out = re.sub(r'(?<!href=")(?<!>)(https?://[^\s<>")]+)', r'<a href="\1">\1</a>', out)
    return out


def md_to_html(md: str) -> str:
    """Small markdown subset -- headings, bullets, blockquotes,
    bold/italic, links. Enough for an email body, no dependency."""
    out, in_list = [], False
    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if set(stripped) == {"-"} and len(stripped) >= 3:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_md(heading.group(2))}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md(stripped[2:].lstrip())}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if stripped.startswith("> "):
            out.append(f"<blockquote>{_inline_md(stripped[2:])}</blockquote>")
            continue
        out.append(f"<p>{_inline_md(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def md_to_text(md: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", md)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return text.strip()


def render_body(body_md: str, fields: dict) -> str:
    rendered = body_md
    for token, value in fields.items():
        value = str(value)
        # A value that already carries its unit (e.g. "55%") next to copy that
        # spells the unit out ("{{snapshot_attendance_rate}}%") would render
        # "55%%" -- absorb the duplicate rather than ship it.
        if value.endswith("%"):
            rendered = rendered.replace("{{%s}}%%" % token, value)
        rendered = rendered.replace("{{%s}}" % token, value)
    return rendered


def render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f'{k}: "{str(v).replace(chr(34), chr(92) + chr(34))}"')
    lines.append("---")
    return "\n".join(lines)


def segment_links(event: dict, segment: str, letter: str, campaign: str) -> dict:
    """Both links carry the same utm_content (`<segment>-<variant>`) so the A/B
    arm is attributable end to end. The no-show CTA points at the registration
    page (the on-demand gate); everyone else goes straight to the recording."""
    content = f"{segment}-{letter}"
    recording = with_utm(event["recording_url"], UTM_SOURCE, UTM_MEDIUM, campaign, content)
    cta_target = event.get("registration_page") if segment == "no_show" else event["recording_url"]
    cta = with_utm(cta_target or event["recording_url"], UTM_SOURCE, UTM_MEDIUM, campaign, content)
    return {"recording": recording, "cta": cta, "content": content}


def build_recipients(segment: str, contacts: list, variant: dict, extraction: dict, event: dict,
                     campaign: str, grounder: Grounder, snapshot=None, quotes_by_speaker=None) -> list:
    """One fully rendered message per mailable contact. Subject alternates A/B
    by row index -- deterministic, so a re-run produces the identical split."""
    recipients = []
    for index, contact in enumerate(contacts):
        letter = "a" if index % 2 == 0 else "b"
        subject = variant["subject_a"] if letter == "a" else variant["subject_b"]
        links = segment_links(event, segment, letter, campaign)
        fields = {
            "firstname": contact["first_name"],
            "recording_link": links["recording"],
            "cta_link": links["cta"],
        }
        if segment == "speaker":
            fields.update(snapshot_fields(snapshot, (quotes_by_speaker or {}).get(contact["name"], [])))
        else:
            resolved = resolve_takeaway(extraction, contact["function"], contact["industry_bucket"])
            fields["takeaway_headline"] = resolved["headline"]
            fields["takeaway_body"] = resolved["body"]
        body_md = render_body(variant["body_md"], fields)
        leftover = TOKEN_RE.findall(body_md)
        if leftover:
            raise RuntimeError(f"{segment} body for {contact['email']} still carries unfilled merge fields: {leftover}")
        if segment == "speaker":
            lint_no_third_person_self(contact["name"], body_md)
        if index == 0:
            grounder.check_text(f"rendered:{segment}", "body_md", body_md)
        recipients.append({
            "email": contact["email"],
            "hubspot_contact_id": contact.get("hubspot_contact_id") or None,
            "segment": segment,
            "firstname": contact["first_name"],
            "function": contact.get("function", "general"),
            "industry_bucket": contact.get("industry_bucket", "other_commercial"),
            "subject": subject,
            "body_html": md_to_html(body_md),
            "body_text": md_to_text(body_md),
            "links": {"recording": links["recording"], "cta": links["cta"]},
            "utm": {"source": UTM_SOURCE, "medium": UTM_MEDIUM, "campaign": campaign, "content": links["content"]},
            "demo_redirect_to": None,
            "_icp_tier": contact.get("icp_tier", "tier3"),
        })
    return recipients


TIER_ORDER = ["tier1", "tier2", "tier3"]


def assign_demo_redirect(recipients: list, segment: str) -> dict:
    """The demo dispatch sends one real message per bulk segment to a
    Kai-controlled alias, because the registrant people are synthetic. Pick the
    first tier1 recipient; if this event's enrichment produced no tier1 in the
    segment, fall back to the best tier present and record which tier was used
    rather than leaving the demo lane with nothing to send."""
    target = DEMO_REDIRECT.get(segment)
    if not target or not recipients:
        return {"segment": segment, "selected": None, "tier_used": None}
    for tier in TIER_ORDER:
        for rec in recipients:
            if rec["_icp_tier"] == tier:
                rec["demo_redirect_to"] = target
                return {"segment": segment, "selected": rec["email"], "tier_used": tier,
                        "redirect_to": target, "fallback": tier != "tier1"}
    recipients[0]["demo_redirect_to"] = target
    return {"segment": segment, "selected": recipients[0]["email"], "tier_used": recipients[0]["_icp_tier"],
            "redirect_to": target, "fallback": True}


# ---------------------------------------------------------------------------
def run(args) -> int:
    out_dir = Path(args.out)
    (out_dir / "emails").mkdir(parents=True, exist_ok=True)
    (out_dir / "receipts").mkdir(parents=True, exist_ok=True)
    try:
        return _run(args, out_dir)
    finally:
        (out_dir / "receipts" / "m2_llm_calls.json").write_text(
            json.dumps(LLM_CALLS, indent=2), encoding="utf-8")


def _run(args, out_dir: Path) -> int:
    event_path, segments_path, transcript_path = Path(args.event), Path(args.segments), Path(args.transcript)
    for label, path in (("event", event_path), ("segments", segments_path), ("transcript", transcript_path)):
        if not path.exists():
            raise RuntimeError(f"missing required input file: {label} ({path})")
    event = load_json(event_path)
    segments = load_json(segments_path)
    transcript_text = transcript_path.read_text(encoding="utf-8")
    if len(transcript_text.split()) < 200:
        raise RuntimeError(f"transcript {transcript_path} has {len(transcript_text.split())} words -- too short to ground copy in")

    speakers = event["speakers"]
    speakers_path = Path(args.speakers)
    if speakers_path.exists():
        bios = {s["name"]: s for s in load_json(speakers_path)}
        speakers = [{**s, **bios.get(s["name"], {})} for s in speakers]
    matched_speakers = match_speaker_emails(speakers, segments.get("speakers", []))
    unmatched = [sp["name"] for sp in matched_speakers if not sp["email"]]
    if unmatched:
        raise RuntimeError(
            f"no email for speaker(s) {unmatched} -- add an `email` to their record in "
            f"{event_path} or {speakers_path}. Refusing to write a blank recipient."
        )
    for sp in matched_speakers:
        if sp["email"] not in segments.get("speakers", []):
            print(f"WARNING: speaker {sp['name']} mails to {sp['email']}, which is not in "
                  f"{segments_path}'s speakers list", file=sys.stderr)

    registrants_by_email = load_registrants(DEFAULT_REGISTRANTS)
    icp_tiers = load_icp_tiers(DEFAULT_ICP)
    enriched_by_email, enriched_source = {}, "none -- fell back to data/incoming/registrants.csv"
    if args.enriched and Path(args.enriched).exists():
        enriched_by_email = load_enriched(Path(args.enriched))
        enriched_source = str(args.enriched)

    suppressed_rows = []
    if enriched_by_email:
        attendee_emails, no_show_emails, suppressed_rows = segment_from_enriched(enriched_by_email)
        segmentation_source = f"M1 enriched output ({enriched_source})"
    else:
        print("WARNING: no M1 --enriched file -- segmenting off data/fixtures/segments.json, which is "
              "neither deduped nor suppression-filtered. Run M1 first and pass its hubspot_ready.csv.",
              file=sys.stderr)
        attendee_emails, no_show_emails = segments["attendees"], segments["no_shows"]
        segmentation_source = "data/fixtures/segments.json (raw, no M1 dedupe/suppression)"

    # A speaker who also registered shows up in M1's rows (this event's aliases
    # do, with attendance_status=attended). They get the speaker mail, not both.
    speaker_inboxes = {sp["email"].strip().lower() for sp in matched_speakers}
    speaker_also_registered = sorted(e for e in attendee_emails + no_show_emails
                                     if e.strip().lower() in speaker_inboxes)
    if speaker_also_registered:
        attendee_emails = [e for e in attendee_emails if e.strip().lower() not in speaker_inboxes]
        no_show_emails = [e for e in no_show_emails if e.strip().lower() not in speaker_inboxes]
        print(f"NOTE: {len(speaker_also_registered)} speaker address(es) also appear in the registrant "
              f"rows ({', '.join(speaker_also_registered)}) -- removed from the bulk segments so they "
              "receive the speaker email only", file=sys.stderr)

    campaign = event_slug(event)
    contacts = {
        "attendee": build_contacts(attendee_emails, enriched_by_email, registrants_by_email, icp_tiers),
        "no_show": build_contacts(no_show_emails, enriched_by_email, registrants_by_email, icp_tiers),
        "speaker": [{
            "email": sp["email"], "name": sp["name"], "first_name": sp["name"].split()[0],
            "company": sp.get("company", ""), "job_title": sp.get("title", ""),
            "function": "executive", "icp_tier": "tier1", "industry_bucket": "other_commercial",
            "hubspot_contact_id": None,
        } for sp in matched_speakers],
    }
    snapshot = compute_snapshot(enriched_by_email, segments, registrants_by_email)
    audience = contacts["attendee"] + contacts["no_show"]
    function_counts = dict(Counter(c["function"] for c in audience))
    industry_counts = dict(Counter(c["industry_bucket"] for c in audience))

    extraction_prompt = build_extraction_prompt(event, speakers, transcript_text, function_counts, industry_counts)

    # --- dry run: every prompt, no network -------------------------------
    if args.live_dry_run:
        stub = load_json(EXTRACTION_SAMPLE) if EXTRACTION_SAMPLE.exists() else {
            "premise": "<from call 1>", "takeaways": [], "quotes": [], "no_show_moments": [],
            "by_function": {}, "by_industry": {},
        }
        prompts = [("extraction", extraction_prompt)] + [
            (seg, build_segment_prompt(seg, event, speakers, stub, snapshot)) for seg in SEGMENTS
        ]
        for i, (purpose, text) in enumerate(prompts, 1):
            print(f"\n{'=' * 78}\n### PROMPT {i}/{len(prompts)} -- {purpose} ({len(text)} chars)\n{'=' * 78}\n")
            print(text)
        print(f"\n{'=' * 78}\nDRY RUN: {len(prompts)} prompts, budget {MAX_LLM_CALLS}, 0 network calls. "
              f"Extraction context = {'cached sample_output/extraction.json' if EXTRACTION_SAMPLE.exists() else 'empty stub (no cached extraction yet)'}.")
        return 0

    grounder = Grounder(transcript_text, allowed_extra=[event["event_name"], event.get("host_company", "")]
                        + [s.get("name", "") for s in speakers] + [s.get("company", "") for s in speakers])
    transcript_sha = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    dropped_quotes = []
    lane = "offline" if args.offline else "live"

    if args.offline:
        extraction, variants, stored_fp = load_replay(transcript_path, event_path)
        for key, typ in (("premise", str), ("takeaways", list), ("quotes", list),
                         ("no_show_moments", list), ("by_function", dict), ("by_industry", dict)):
            if not isinstance(extraction.get(key), typ):
                raise RuntimeError(f"sample_output/extraction.json is malformed: {key} is not {typ.__name__}")
        if set(variants) != set(SEGMENTS):
            raise RuntimeError(f"sample_output/variants.json covers {sorted(variants)}, expected {list(SEGMENTS)}")
        model_used = stored_fp.get("model", "unknown")
    else:
        extraction = llm_json(extraction_prompt, "extraction",
                              make_extraction_validator([sp["name"] for sp in matched_speakers]))
        # A quote that is not verbatim in the transcript never reaches copy:
        # it is dropped here and listed in grounding.json. Everything that
        # survives is checked (and recorded) in the common pass below.
        kept = []
        for q in extraction["quotes"]:
            (kept if grounder.probe(q["text"], q["timestamp"]) else dropped_quotes).append(q)
        extraction["quotes"] = kept
        if len(kept) < 2:
            (out_dir / "grounding.json").write_text(
                json.dumps(grounder.report(transcript_sha, dropped_quotes, "fail"), indent=2), encoding="utf-8")
            raise RuntimeError(
                f"only {len(kept)} of {len(kept) + len(dropped_quotes)} extracted quotes are verbatim in the "
                "transcript -- not enough grounded material to write copy. See grounding.json."
            )
        variants = {}
        for seg in SEGMENTS:
            prompt = build_segment_prompt(seg, event, speakers, extraction, snapshot)
            variants[seg] = llm_json(prompt, f"variant_{seg}", make_variant_validator(seg))
        model_used = next((c["model"] for c in reversed(LLM_CALLS) if c.get("parse_ok")), "unknown")

    quotes_by_speaker = {
        sp["name"]: [q for q in extraction["quotes"] if q.get("speaker") == sp["name"]] for sp in matched_speakers
    }

    # --- grounding: extraction material (both lanes -- every angle below is
    # merged into some recipient's body) then the generated copy -----------
    for i, q in enumerate(extraction["quotes"]):
        grounder.check_quote_text("extraction", f"quote[{i}:{q.get('speaker', '')}]", q.get("text", ""))
        grounder.check_timestamp("extraction", f"quote[{i}:{q.get('speaker', '')}]", q.get("timestamp", ""))
    for i, t in enumerate(extraction["takeaways"]):
        grounder.check_timestamp("extraction", f"takeaway[{i}]", t.get("timestamp", ""))
        grounder.check_text("extraction", f"takeaway[{i}]", f"{t.get('headline', '')} {t.get('body', '')}")
    for i, m in enumerate(extraction["no_show_moments"]):
        grounder.check_timestamp("extraction", f"no_show_moment[{i}]", m.get("timestamp", ""))
        grounder.check_text("extraction", f"no_show_moment[{i}]", m.get("why", ""))
    for func, tk in extraction["by_function"].items():
        grounder.check_text("extraction", f"by_function[{func}]", f"{tk.get('headline', '')} {tk.get('body', '')}")
    for bucket, angle in extraction["by_industry"].items():
        grounder.check_text("extraction", f"by_industry[{bucket}]", angle)
    for seg, variant in variants.items():
        for field in ("subject_a", "subject_b", "preheader", "body_md"):
            grounder.check_text(f"variant:{seg}", field, variant[field])
        for i, t in enumerate(variant["takeaways"]):
            grounder.check_text(f"variant:{seg}", f"takeaway[{i}]", t)

    # --- recipients -------------------------------------------------------
    all_recipients, demo_selection = [], []
    for seg in SEGMENTS:
        seg_recipients = build_recipients(
            seg, contacts[seg], variants[seg], extraction, event, campaign, grounder,
            snapshot=snapshot, quotes_by_speaker=quotes_by_speaker)
        demo_selection.append(assign_demo_redirect(seg_recipients, seg))
        all_recipients.extend(seg_recipients)

    grounding = grounder.report(transcript_sha, dropped_quotes, "fail" if grounder.failures() else "pass")
    (out_dir / "grounding.json").write_text(json.dumps(grounding, indent=2), encoding="utf-8")
    if grounder.failures():
        sample = "; ".join(f"{c['scope']}.{c['field']} {c['kind']} {c['value']!r} ({c['reason']})"
                           for c in grounder.failures()[:3])
        raise RuntimeError(
            f"grounding failed: {len(grounder.failures())} of {len(grounder.checks)} checks did not match the "
            f"transcript -- {sample}. Full list in {out_dir}/grounding.json. No plan written."
        )

    for rec in all_recipients:
        rec.pop("_icp_tier", None)

    run_id = f"m2-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    generated_at = datetime.now(timezone.utc).isoformat()
    plan = {
        "run_id": run_id,
        "event_slug": campaign,
        "generated_at": generated_at,
        "lane": lane,
        "event_close_ts": event_close_ts(event),
        "variants": {
            seg: {
                "subject_a": variants[seg]["subject_a"],
                "subject_b": variants[seg]["subject_b"],
                "preheader": variants[seg]["preheader"],
                "body_md": variants[seg]["body_md"],
                "takeaways": variants[seg]["takeaways"],
                **({"snapshot": snapshot} if seg == "speaker" else {}),
            } for seg in SEGMENTS
        },
        "recipients": all_recipients,
        "approval": {"status": "pending_human_approval", "approved_by": None, "approved_at": None, "mode": None},
        "counts": {
            "attendee": len(contacts["attendee"]),
            "no_show": len(contacts["no_show"]),
            "speaker": len(contacts["speaker"]),
            "mailable": len(all_recipients),
            "suppressed": len(suppressed_rows),
        },
    }
    (out_dir / "dispatch_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (out_dir / "extraction.json").write_text(json.dumps(extraction, indent=2), encoding="utf-8")

    # --- human-readable drafts -------------------------------------------
    for seg in SEGMENTS:
        variant = variants[seg]
        body = variant["body_md"]
        if seg != "speaker":
            body += personalization_preview(contacts[seg], extraction)
        fm = {
            "segment": seg,
            "subject_a": variant["subject_a"],
            "subject_b": variant["subject_b"],
            "preheader": variant["preheader"],
            "to_count": len(contacts[seg]),
            "utm_campaign": campaign,
            "utm_content_a": f"{seg}-a",
            "utm_content_b": f"{seg}-b",
            "recording_link_variant_a": segment_links(event, seg, "a", campaign)["recording"],
            "recording_link_variant_b": segment_links(event, seg, "b", campaign)["recording"],
            "lane": lane,
        }
        (out_dir / "emails" / EMAIL_FILES[seg]).write_text(
            render_frontmatter(fm) + "\n\n" + body.strip() + "\n", encoding="utf-8")

    approval_gate = {
        "status": "pending_human_approval",
        "approved_by": None,
        "approved_at": None,
        "mode": None,
        "run_id": run_id,
        "requested_at": generated_at,
        "event": event["event_name"],
        "dispatch_plan": "dispatch_plan.json",
        "counts": plan["counts"],
        "demo_recipients": [r["email"] for r in all_recipients if r["demo_redirect_to"]]
                           + [r["email"] for r in all_recipients if r["segment"] == "speaker"],
        "demo_selection": demo_selection,
        "note": "No mail is sent by this module. A human sets status:'approved' with approved_by/approved_at "
                "and mode:'demo'|'full'; only then does the n8n dispatch node read dispatch_plan.json.",
    }
    (out_dir / "approval_gate.json").write_text(json.dumps(approval_gate, indent=2), encoding="utf-8")

    manifest = {
        "run_id": run_id,
        "event": event["event_name"],
        "date": event["date"],
        "campaign": campaign,
        "mode": lane,
        "lane": lane,
        "model": model_used,
        "llm_calls_made": len(LLM_CALLS),
        "enriched_source": enriched_source,
        "grounding": {k: grounding[k] for k in ("checks_total", "passed", "failed", "status")},
        "snapshot": snapshot,
        "recipient_pipeline": {
            "segmentation_source": segmentation_source,
            "unique_attendee_no_show_contacts": len(attendee_emails) + len(no_show_emails) + len(suppressed_rows),
            "suppressed_count": len(suppressed_rows),
            "suppressed": suppressed_rows,
            "speaker_addresses_removed_from_bulk": speaker_also_registered,
            "mailable": len(all_recipients),
        },
        "segments": {
            seg: {
                "to_count": len(contacts[seg]),
                "subject_a": variants[seg]["subject_a"],
                "subject_b": variants[seg]["subject_b"],
                "utm_content_a": f"{seg}-a",
                "utm_content_b": f"{seg}-b",
                "file": f"emails/{EMAIL_FILES[seg]}",
            } for seg in SEGMENTS
        },
    }
    (out_dir / "comms.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if lane == "live":
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        EXTRACTION_SAMPLE.write_text(json.dumps(extraction, indent=2), encoding="utf-8")
        VARIANTS_SAMPLE.write_text(json.dumps(variants, indent=2), encoding="utf-8")
        fingerprint = compute_fingerprint(transcript_path, event_path)
        fingerprint.update({"generated_at": generated_at, "run_id": run_id, "model": model_used,
                            "llm_calls": len(LLM_CALLS)})
        FINGERPRINT_PATH.write_text(json.dumps(fingerprint, indent=2), encoding="utf-8")
        print(f"M2 live: {len(LLM_CALLS)} LLM call(s) via {model_used}; sample_output/ refreshed "
              f"(extraction.json, variants.json, .fingerprint.json) -- `--offline` now replays this generation")
    else:
        print(f"M2 offline: replayed sample_output/ (generated {load_json(FINGERPRINT_PATH).get('generated_at', '?')} "
              f"by {model_used}); 0 LLM calls, fingerprint matched")

    print(f"M2 comms: 3 variants, {len(all_recipients)} rendered recipients "
          f"({plan['counts']['attendee']} attendee, {plan['counts']['no_show']} no-show, "
          f"{plan['counts']['speaker']} speaker; {len(suppressed_rows)} suppressed) -> {out_dir}/dispatch_plan.json")
    print(f"M2 grounding: {grounding['passed']}/{grounding['checks_total']} checks pass"
          + (f", {len(dropped_quotes)} unverifiable quote(s) dropped at extraction" if dropped_quotes else "")
          + f" -> {out_dir}/grounding.json")
    print(f"M2 approval: pending_human_approval (event close {plan['event_close_ts']})")
    return 0


def main():
    parser = argparse.ArgumentParser(description="M2 -- generate and render post-event follow-up email (live by default).")
    parser.add_argument("--event", default=str(DEFAULT_EVENT))
    parser.add_argument("--segments", default=str(DEFAULT_SEGMENTS))
    parser.add_argument("--speakers", default=str(DEFAULT_SPEAKERS))
    parser.add_argument("--transcript", default=str(DEFAULT_TRANSCRIPT))
    parser.add_argument("--enriched", default=None,
                        help="M1's hubspot_ready.csv (or .json). Without it M2 falls back to "
                             "data/fixtures/segments.json, which is neither deduped nor suppressed.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--offline", action="store_true",
                        help="Replay sample_output/ instead of calling an LLM. Refuses if its "
                             "fingerprint does not match this transcript + event.")
    parser.add_argument("--live-dry-run", action="store_true", dest="live_dry_run",
                        help="Print the prompts this run would send, then exit. Zero network.")
    # Legacy flags from v1's offline-first CLI, kept so existing callers keep
    # working: --live is now the default, --allow-stale meant "replay the
    # cached copy" and maps to --offline (which refuses on a mismatch instead
    # of forcing stale copy through).
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-stale", action="store_true", dest="allow_stale", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.allow_stale and not args.live:
        print("NOTE: --allow-stale is retired; running --offline (replays sample_output/, "
              "refuses on a fingerprint mismatch)", file=sys.stderr)
        args.offline = True

    try:
        return run(args)
    except RuntimeError as e:
        print(f"refusing to send: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"refusing to send: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
