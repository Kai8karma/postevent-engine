#!/usr/bin/env python3
"""Vercel Python serverless function: /api/run -- v1. POST is retired.

GET still answers (build_get_response(): deployment health + whether a live
lane is available). POST runs nothing: handle_run() returns a refusal naming
its replacement -- see RETIRED_NOTE and api/vercel-api-notes.md's "Retired:
this endpoint no longer runs modules".

The v1 request body is kept for reference as _handle_run_v1_disabled(),
which nothing calls. Everything it reaches -- materialize_event(),
stage_m1/m2/m3/m4(), summarize_m4(), CHAIN_LIVE_NOTE, degraded_note() and
the M4 artifact specs -- is part of that retired v1 body and still speaks
v1's flag convention (--live opts in), which v2 inverted: every module now
runs live by default and --offline is the opt-out.

This file still ships because api/server.py imports it for helpers:
REPO_ROOT, PY, run_module(), build_env(), lines_for_log(), the OpenRouter
key helpers, summarize_m1() and M1_ARTIFACT_SPECS.

The supported entry point is the module API in api/server.py (POST /run with
a module and a phase) -- see docs/module-api.md. There, M4 is driven by
modules/m4-dashboard/dashboard.py, whose CLI takes a phase positional
(seed | sync | analyze | render | all) plus --out, not the single-shot v1
builder this file's retired M4 stage was written against.

Stdlib only -- same constraint as the modules it drives.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Repo root resolution -- must work both locally (repo root two levels above
# this file) and on Vercel (function bundled with modules/ config/ data/
# copied in beside api/). Search upward from this file for the marker path
# rather than assuming a fixed depth.
def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent] + list(here.parent.parents):
        if (candidate / "modules" / "m1-enrichment" / "enrich.py").exists():
            return candidate
    raise RuntimeError(
        "cannot locate repo root: no ancestor directory of "
        f"{here} contains modules/m1-enrichment/enrich.py -- this deployment "
        "did not bundle modules/, config/, and data/ next to api/run.py "
        "(see api/vercel-api-notes.md 'What must ship next to this function')"
    )


try:
    REPO_ROOT = _find_repo_root()
    REPO_ROOT_ERROR = None
except RuntimeError as _exc:
    REPO_ROOT = None
    REPO_ROOT_ERROR = str(_exc)

PY = sys.executable or "python3"
LLM_ENV_FILE = Path.home() / ".config" / "postevent" / "llm.env"

# Fast default model chain (see HANDOFF facts): nemotron-3.5-lightning answers
# a short prompt in ~2s; the two larger nemotron models are kept as fallback
# for when the free-tier fast model is rate-limited or overloaded. M2/M3 hand
# off across the comma-separated chain themselves; M1 only ever uses the
# first entry.
FAST_MODEL_CHAIN = (
    "nvidia/nemotron-3.5-lightning:free,"
    "nvidia/nemotron-3-super-120b-a12b:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free"
)

# Vercel Hobby serverless functions cap at maxDuration:60s (see
# vercel-api-notes.md). Total function time must stay under ~55s, so every
# subprocess this handler runs is bounded well below that, with headroom for
# request parsing / file I/O / JSON response assembly on either side.
#
# Offline lane is measured at ~0.1s per module end to end -- 20s is already
# a >100x margin, purely to absorb a cold Vercel container.
OFFLINE_TIMEOUT_S = 20
# Live lane: measured full-fixture (150 registrant rows) M1 --live took
# 5m24s even leading with the fast nemotron-3.5-lightning model. Re-tested
# with a minimal 2-row CSV (1 row needing LLM inference, the smallest
# possible live call) and it still took well over a minute -- current
# OpenRouter free-tier queueing/latency, not batch count, is the dominant
# cost right now. Given that, no timeout value reliably fits a genuinely
# live call inside Vercel's 60s cap; 45s is set to leave ~10s of headroom
# under the cap for request parsing / tmpdir setup / JSON response assembly
# while still giving a live call its best realistic shot. When it doesn't
# make it, the module times out and fails loud (see notes in handle_run's
# live-lane comment and vercel-api-notes.md) rather than hanging past
# Vercel's cap -- this is an honest, expected outcome under current
# OpenRouter load, not a bug in this handler.
LIVE_TIMEOUT_S = 45

CHAIN_LIVE_NOTE = (
    "live:true with module:'chain' does not run any module live -- a full "
    "M1->M2->M3->M4 live chain cannot fit the ~55s Vercel function budget "
    "even with the fast default model (measured: full-fixture M1 --live "
    "alone took 5m24s; a minimal 2-row M1 --live call still took over a "
    "minute under current OpenRouter free-tier load; M2/M3 --live measured "
    "795s/548s on the slower nemotron-ultra model in earlier testing). Ran "
    "the full offline chain instead. To see a real live LLM call, POST "
    "module:'m1' (or 'm2'/'m3') with live:true directly -- each gets its "
    f"own {LIVE_TIMEOUT_S}s budget, though even that can time out under "
    "current OpenRouter load -- or see the committed live receipts under "
    "out/receipts/ (m1-live-slice-30/, m2-live/, m3-live/, m4-live-portal/)."
)

ARTIFACT_PREVIEW_CAP = 20000


# --------------------------------------------------------------------------
# OpenRouter key detection (same source order the modules themselves use --
# see enrich.py/comms.py/repurpose.py's get_openrouter_key(): env var first,
# else ~/.config/postevent/llm.env. Never printed/logged/returned anywhere.)
def _read_llm_env_file() -> dict:
    if not LLM_ENV_FILE.exists():
        return {}
    values = {}
    try:
        for line in LLM_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return values


def get_openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY") or _read_llm_env_file().get("OPENROUTER_API_KEY", "")


def openrouter_key_present() -> bool:
    return bool(get_openrouter_key())


# --------------------------------------------------------------------------
# Live-vs-degraded lane classification (P0 fix: a --live request whose
# module silently fell back to the rule-table result must never be reported
# as lane:"live" -- see the workstream brief this file was built against).
# Every decision here reads evidence the module itself emitted (its own
# report file, its own stderr) -- never the module's exit code alone, which
# is 0 in both the real-generation and silent-fallback cases.
OPENROUTER_PING_URL = "https://openrouter.ai/api/v1/chat/completions"
_PROBE_CACHE = {"ts": 0.0, "result": None}
PROBE_CACHE_TTL_S = 300  # see build_get_response()'s probe= doc: opt-in AND cached


def probe_openrouter_liveness(key: str) -> dict:
    """Minimal 1-token completion ping against the first model in
    FAST_MODEL_CHAIN. This is the only place in the whole request path that
    surfaces the provider's own error text (e.g. a 429 body) -- enrich.py's
    internal preflight (check_llm_health) deliberately swallows that detail
    before it ever reaches stderr, so it cannot be recovered from a module's
    log after the fact. Never raises -- a probe failure must never break the
    response it's enriching."""
    now = time.time()
    cached = _PROBE_CACHE["result"]
    if cached is not None and (now - _PROBE_CACHE["ts"]) < PROBE_CACHE_TTL_S:
        return cached
    model = FAST_MODEL_CHAIN.split(",")[0]
    result = {"ok": False, "model": model, "detail": "", "checked_at": now}
    if not key:
        result["detail"] = "no OPENROUTER_API_KEY present -- nothing to probe"
        _PROBE_CACHE["ts"], _PROBE_CACHE["result"] = now, result
        return result
    body = json.dumps({
        "model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_PING_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"] or {}
            result["detail"] = f"{err.get('code')} {str(err.get('message'))[:300]}".strip()
        else:
            result["ok"] = True
            result["detail"] = "model responded"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        result["detail"] = f"HTTP {exc.code} {exc.reason}: {detail}"
    except urllib.error.URLError as exc:
        result["detail"] = f"request failed: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 -- a probe must never crash the response it's enriching
        result["detail"] = f"{type(exc).__name__}: {exc}"
    _PROBE_CACHE["ts"], _PROBE_CACHE["result"] = now, result
    return result


NO_BACKEND_RE = re.compile(r"batch parse failed \((.+)\);")


def classify_m1_lane(stderr: str, out_dir: Path):
    """Returns (lane, reason) for an m1 --live request. lane is 'live' or
    'degraded'; reason is None (lane=='live') or a string quoting the
    module's own evidence for why it wasn't. Never trusts the exit code --
    enrich.py exits 0 on a silent rule-table fallback (see this workstream's
    evidence: a buried stderr warning, not a failure)."""
    report_path = out_dir / "live_inference_report.json"
    if not report_path.exists():
        return "degraded", "m1 produced no live_inference_report.json for a live request"
    lr = json.loads(report_path.read_text(encoding="utf-8"))
    batches = lr.get("inference_batches", 0) + lr.get("icp_batches", 0)
    patched = lr.get("inference_rows_patched", 0) + lr.get("icp_rows_annotated", 0)
    parse_failures = lr.get("inference_parse_failures", 0) + lr.get("icp_parse_failures", 0)
    m = NO_BACKEND_RE.search(stderr or "")
    if m and "no LLM backend available" in m.group(1):
        return "degraded", m.group(1)
    if batches > 0 and (patched == 0 or parse_failures >= batches):
        return "degraded", (
            f"m1 attempted {batches} live batch(es) but landed 0 real model output "
            f"({parse_failures} parse failure(s), {patched} row(s) patched) -- see log for the "
            "module's own [warn] line"
        )
    return "live", None


def degraded_note(module: str, reason: str, key_present: bool) -> str:
    """Builds the notes-array entry for a degraded lane: the module's own
    verbatim evidence, a plain-English gloss, and what the reviewer can do
    next -- never just a bare status flip."""
    note = (
        f"live:true was requested for {module} and the module exited 0, but no real model output "
        f"actually landed -- it silently fell back to the deterministic rule-table result. This "
        f"response is labelled lane:'degraded' (not 'live') because of that. Module's own evidence: "
        f"{reason}."
    )
    if key_present:
        probe = probe_openrouter_liveness(get_openrouter_key())
        note += f" Direct OpenRouter probe just now: {probe['detail']}."
    note += (
        " To see a real live run: supply your own OPENROUTER_API_KEY (this deployment's free-tier "
        "quota is exhausted as of this run), or inspect the live receipts committed under "
        "out/receipts/ (m1-live-slice-30/, m2-live/, m3-live/, m4-live-portal/)."
    )
    return note


# --------------------------------------------------------------------------
# Input materialisation
def materialize_event(tmp_dir: Path, registrants_csv, transcript_md, event_name,
                       engagement_json=None, segments_json=None):
    """Writes this request's event inputs into tmp_dir, using the caller's
    values where supplied and the bundled fixture (data/incoming/ or
    data/fixtures/) otherwise. engagement_json/segments_json are already-
    parsed dicts -- v1's M4 inputs, passed as --engagement/--segments to the
    single-shot builder v1 ran. v2's modules/m4-dashboard/dashboard.py has
    neither flag: it reads data/fixtures/engagement.json and
    data/fixtures/segments.json directly (ENGAGEMENT_FIXTURE /
    SEGMENTS_FIXTURE). Retired v1 path -- the caller validates JSON-ness
    before this function ever sees them.
    Returns (registrants_path, event_path, transcript_path, engagement_path,
    segments_path, custom)."""
    incoming = REPO_ROOT / "data" / "incoming"
    fixtures = REPO_ROOT / "data" / "fixtures"
    custom = False

    reg_path = tmp_dir / "registrants.csv"
    if registrants_csv is not None:
        reg_path.write_text(registrants_csv, encoding="utf-8")
        custom = True
    else:
        shutil.copy2(incoming / "registrants.csv", reg_path)

    tr_path = tmp_dir / "transcript.md"
    if transcript_md is not None:
        tr_path.write_text(transcript_md, encoding="utf-8")
        custom = True
    else:
        shutil.copy2(incoming / "transcript.md", tr_path)

    ev_path = tmp_dir / "event.json"
    event_data = json.loads((incoming / "event.json").read_text(encoding="utf-8"))
    if event_name:
        event_data["event_name"] = event_name
        custom = True
    ev_path.write_text(json.dumps(event_data, indent=2), encoding="utf-8")

    eng_path = tmp_dir / "engagement.json"
    if engagement_json is not None:
        eng_path.write_text(json.dumps(engagement_json, indent=2), encoding="utf-8")
        custom = True
    else:
        shutil.copy2(fixtures / "engagement.json", eng_path)

    seg_path = tmp_dir / "segments.json"
    if segments_json is not None:
        seg_path.write_text(json.dumps(segments_json, indent=2), encoding="utf-8")
        custom = True
    else:
        shutil.copy2(fixtures / "segments.json", seg_path)

    return reg_path, ev_path, tr_path, eng_path, seg_path, custom


# --------------------------------------------------------------------------
# Subprocess runner
def run_module(cmd, env, timeout_s, cwd=None):
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            cwd=str(cwd or REPO_ROOT), env=env,
        )
        elapsed = time.perf_counter() - start
        return {
            "ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr,
            "elapsed": elapsed, "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        return {
            "ok": False, "returncode": None,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            "elapsed": elapsed, "timed_out": True,
        }
    except FileNotFoundError as exc:
        elapsed = time.perf_counter() - start
        return {
            "ok": False, "returncode": None, "stdout": "", "stderr": str(exc),
            "elapsed": elapsed, "timed_out": False,
        }


def lines_for_log(*texts, limit=60):
    out = []
    for text in texts:
        for line in (text or "").splitlines():
            line = line.strip()
            if line:
                out.append(line)
    return out[:limit]


# --------------------------------------------------------------------------
# Artifact helpers
def read_artifact(path: Path, name: str, kind: str):
    data = path.read_bytes()
    size = len(data)
    text = data.decode("utf-8", errors="replace")
    if len(text) > ARTIFACT_PREVIEW_CAP:
        preview = text[:ARTIFACT_PREVIEW_CAP] + f"\n... [truncated, {size} bytes total]"
    else:
        preview = text
    return {"name": name, "kind": kind, "bytes": size, "preview": preview}


def collect_artifacts(out_dir: Path, specs, name_prefix=""):
    """specs: list of (relative_path, kind), looked up relative to out_dir.
    Skips files that don't exist (e.g. live_inference_report.json /
    extraction.json only appear in certain lanes) -- never fabricates a
    missing artifact. name_prefix is prepended to the returned artifact
    "name" only (e.g. "m1/") -- it plays no part in the on-disk lookup."""
    artifacts = []
    for rel, kind in specs:
        p = out_dir / rel
        if p.exists() and p.is_file():
            artifacts.append(read_artifact(p, name_prefix + rel, kind))
    return artifacts


# --------------------------------------------------------------------------
# Per-module summary extraction (reads the module's own real output files /
# stdout -- never recomputes the numbers itself)
def summarize_m1(out_dir: Path):
    quality = json.loads((out_dir / "quality_report.json").read_text(encoding="utf-8"))
    dedupe = json.loads((out_dir / "dedupe_report.json").read_text(encoding="utf-8"))
    sc = quality["spec_completeness"]
    tiers = {"tier1": 0, "tier2": 0, "tier3": 0}
    ready_csv = out_dir / "hubspot_ready.csv"
    if ready_csv.exists():
        with open(ready_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t = (row.get("icp_tier") or "").strip()
                if t in tiers:
                    tiers[t] += 1
    llm_calls_made = 0
    llm_rows_patched = 0
    live_report_path = out_dir / "live_inference_report.json"
    if live_report_path.exists():
        lr = json.loads(live_report_path.read_text(encoding="utf-8"))
        llm_calls_made = lr.get("inference_batches", 0) + lr.get("icp_batches", 0)
        llm_rows_patched = lr.get("inference_rows_patched", 0) + lr.get("icp_rows_annotated", 0)
    counts = dedupe["counts"]
    return {
        "input_rows": counts["total_input_rows"],
        "output_rows": counts["output_rows"],
        "duplicates_merged": counts["within_batch_duplicate_pairs"],
        "excluded_rows": counts["fake_rows_excluded"],
        "contact_completeness_pct": sc["contact"]["completeness_pct"],
        "company_completeness_pct": sc["company"]["completeness_pct"],
        "tier1": tiers["tier1"], "tier2": tiers["tier2"], "tier3": tiers["tier3"],
        "llm_calls_made": llm_calls_made, "llm_rows_patched": llm_rows_patched,
    }


M1_ARTIFACT_SPECS = [
    ("hubspot_ready.csv", "csv"),
    ("hubspot_contacts.csv", "csv"),
    ("hubspot_companies.csv", "csv"),
    ("quality_report.json", "json"),
    ("dedupe_report.json", "json"),
    ("live_inference_report.json", "json"),
]


def summarize_m2(out_dir: Path):
    comms = json.loads((out_dir / "comms.json").read_text(encoding="utf-8"))
    segments = comms.get("segments", {})
    recipients = sum(int(s.get("to_count", 0)) for s in segments.values())
    subject_variants = sum(1 for s in segments.values() for k in ("subject_a", "subject_b") if s.get(k))
    approval_status = "unknown"
    gate_path = out_dir / "approval_gate.json"
    if gate_path.exists():
        approval_status = json.loads(gate_path.read_text(encoding="utf-8")).get("status", "unknown")
    return {
        "segments": len(segments),
        "recipients": recipients,
        "subject_variants": subject_variants,
        "utm_campaign": comms.get("campaign", ""),
        "approval_status": approval_status,
        # comms.py only sets this key on its --live path (see comms.py's
        # manifest literal) -- .get(..., 0) keeps offline/legacy runs honest.
        "llm_calls_made": comms.get("llm_calls_made", 0),
    }


def m2_artifact_specs(out_dir: Path):
    # Retired v1 spec. sends_log.json has had no writer since M2 moved to
    # dispatch_plan.json + comms.json; collect_artifacts() just skips it.
    specs = [("comms.json", "json"), ("approval_gate.json", "json"), ("sends_log.json", "json")]
    comms_path = out_dir / "comms.json"
    if comms_path.exists():
        comms = json.loads(comms_path.read_text(encoding="utf-8"))
        for seg in comms.get("segments", {}).values():
            f = seg.get("file")
            if f:
                specs.append((f, "markdown"))
    return specs


def summarize_m3(out_dir: Path):
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    spec_check = manifest.get("spec_check", {})
    youtube_chapters = 0
    yt_path = out_dir / "youtube.md"
    if yt_path.exists():
        youtube_chapters = len(re.findall(r"^\d{1,2}:\d{2}\s+\S", yt_path.read_text(encoding="utf-8"), re.MULTILINE))
    return {
        "blog_words": spec_check.get("blog_words", 0),
        "social_posts": spec_check.get("social_posts", 0),
        "youtube_chapters": youtube_chapters,
        "assets": len(manifest.get("assets", {})),
        "blog_within_spec": spec_check.get("blog_within_spec", False),
        "social_within_spec": spec_check.get("social_within_spec", False),
        # repurpose.py only sets this key on its --live path (see run_live())
        # -- .get(..., 0) keeps offline/legacy runs honest.
        "llm_calls_made": manifest.get("llm_calls_made", 0),
    }


M3_ARTIFACT_SPECS = [
    ("blog.md", "markdown"),
    ("youtube.md", "markdown"),
    ("infographic.md", "markdown"),
    ("social.md", "markdown"),
    ("manifest.json", "json"),
    ("extraction.json", "json"),
]


M4_STAT_RE_1 = re.compile(
    r"registrants=(\d+)\s+attendees=(\d+)\s+engaged=(\d+)\s+mql_plus=(\d+)\s+attendee_to_mql_pct=([\d.]+)"
)
M4_STAT_RE_2 = re.compile(r"top_accounts=(\d+)\s+committee_accounts=(\d+)\s+anomalies=(\d+)")


def summarize_m4(stdout: str):
    """Retired v1 helper -- nothing calls it. It scraped the stat lines v1's
    single-shot M4 builder printed, because that script's KPI data only
    existed embedded in index.html's JSON blob. v2's M4 summaries are read
    from real files instead, by api/server.py's own per-phase summarisers
    (summarize_m4_seed/sync/analyze/render over snapshot.json, analysis.json
    and dashboard_data.json)."""
    m1 = M4_STAT_RE_1.search(stdout)
    m2 = M4_STAT_RE_2.search(stdout)
    summary = {
        "registrants": int(m1.group(1)) if m1 else 0,
        "attendees": int(m1.group(2)) if m1 else 0,
        "mql": int(m1.group(4)) if m1 else 0,
        "attendee_to_mql_pct": float(m1.group(5)) if m1 else 0.0,
        "top_accounts": int(m2.group(1)) if m2 else 0,
        "committee_accounts": int(m2.group(2)) if m2 else 0,
        "anomalies": int(m2.group(3)) if m2 else 0,
        # v1's builder had no --live path, so this hard-codes v1's only
        # possible answer. Not true of v2: modules/m4-dashboard/dashboard.py's
        # analyze phase writes narrative_source "live" when the LLM ran and
        # "rules" when it fell back to the templated numbers, and the
        # browser-side refresh (modules/m4-dashboard/api/narrative.js) is a
        # separate path again.
        "narrative_source": "cached_fallback",
    }
    return summary


M4_ARTIFACT_SPECS = [("index.html", "html")]


# --------------------------------------------------------------------------
# Per-module stage runner
def build_env(live: bool):
    env = dict(os.environ)
    if live:
        env["LLM_BACKEND"] = "openrouter"
        env["OPENROUTER_MODEL"] = FAST_MODEL_CHAIN
        key = get_openrouter_key()
        if key:
            env["OPENROUTER_API_KEY"] = key
    return env


def stage_m1(out_dir: Path, reg_path: Path, live: bool, log: list):
    cmd = [
        PY, str(REPO_ROOT / "modules" / "m1-enrichment" / "enrich.py"),
        "--in", str(reg_path),
        "--config", str(REPO_ROOT / "config" / "icp.yaml"),
        "--hubspot", str(REPO_ROOT / "data" / "fixtures" / "hubspot_existing.json"),
        "--out", str(out_dir),
    ]
    if live:
        cmd.append("--live")
    timeout_s = LIVE_TIMEOUT_S if live else OFFLINE_TIMEOUT_S
    log.append(f"[m1] {'live' if live else 'offline'} lane -- invoking enrich.py (timeout {timeout_s}s)")
    result = run_module(cmd, build_env(live), timeout_s)
    log.extend(lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"]:
        raise ModuleFailure(
            "m1", f"m1 timed out after {result['elapsed']:.1f}s (limit {timeout_s}s)",
            "the live lane's LLM batching (inference + ICP scoring) can outrun a single "
            "serverless request for larger registrant lists -- try a smaller CSV, retry "
            "offline, or see the committed live receipt at out/receipts/m1-live-slice-30/",
            log,
        )
    if not result["ok"]:
        raise ModuleFailure(
            "m1", f"m1 exited {result['returncode']}",
            "check the log for the module's own error message", log,
        )
    return result


def stage_m2(out_dir: Path, m1_ready_csv, ev_path: Path, tr_path: Path, live: bool, log: list):
    cmd = [
        PY, str(REPO_ROOT / "modules" / "m2-comms" / "comms.py"),
        "--out", str(out_dir),
        "--event", str(ev_path),
        "--segments", str(REPO_ROOT / "data" / "fixtures" / "segments.json"),
        "--transcript", str(tr_path),
    ]
    if m1_ready_csv is not None:
        cmd += ["--enriched", str(m1_ready_csv)]
    if live:
        cmd.append("--live")
    else:
        # Offline replays cached sample_output copy fingerprinted to the
        # bundled fixture transcript/event; --allow-stale is a no-op when
        # inputs are unchanged and lets a custom transcript replay (with an
        # honest WARNING in the log, see notes) instead of hard-failing.
        cmd.append("--allow-stale")
    timeout_s = LIVE_TIMEOUT_S if live else OFFLINE_TIMEOUT_S
    log.append(f"[m2] {'live' if live else 'offline'} lane -- invoking comms.py (timeout {timeout_s}s)")
    result = run_module(cmd, build_env(live), timeout_s)
    log.extend(lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"]:
        raise ModuleFailure(
            "m2", f"m2 timed out after {result['elapsed']:.1f}s (limit {timeout_s}s)",
            "M2 --live measured ~795s on a slow model in earlier testing -- try offline, "
            "or see the committed live receipt at out/receipts/m2-live/", log,
        )
    if not result["ok"]:
        raise ModuleFailure("m2", f"m2 exited {result['returncode']}", "check the log for the module's own error message", log)
    return result


def stage_m3(out_dir: Path, ev_path: Path, tr_path: Path, live: bool, log: list):
    cmd = [
        PY, str(REPO_ROOT / "modules" / "m3-repurpose" / "repurpose.py"),
        "--out", str(out_dir),
        "--event", str(ev_path),
        "--transcript", str(tr_path),
    ]
    if live:
        cmd.append("--live")
    else:
        cmd.append("--allow-stale")
    timeout_s = LIVE_TIMEOUT_S if live else OFFLINE_TIMEOUT_S
    log.append(f"[m3] {'live' if live else 'offline'} lane -- invoking repurpose.py (timeout {timeout_s}s)")
    result = run_module(cmd, build_env(live), timeout_s)
    log.extend(lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"]:
        raise ModuleFailure(
            "m3", f"m3 timed out after {result['elapsed']:.1f}s (limit {timeout_s}s)",
            "M3 --live measured ~548s on a slow model in earlier testing -- try offline, "
            "or see the committed live receipt at out/receipts/m3-live/", log,
        )
    if not result["ok"]:
        raise ModuleFailure("m3", f"m3 exited {result['returncode']}", "check the log for the module's own error message", log)
    return result


def stage_m4(out_dir: Path, m1_ready_csv: Path, engagement_path: Path, segments_path: Path, log: list):
    """Retired with the rest of the v1 body -- unreachable, since handle_run()
    refuses before _handle_run_v1_disabled() is ever entered.

    It shelled out to v1's single-shot M4 builder with --enriched/--engagement/
    --segments. That script no longer exists and none of those flags do either:
    M4 ships as modules/m4-dashboard/dashboard.py, a phased CLI
    (seed | sync | analyze | render | all) driven by api/server.py -- see
    docs/module-api.md's M4 table. Left as a loud refusal rather than repointed
    at dashboard.py, so nothing can quietly resurrect a v1 M4 lane."""
    raise ModuleFailure(
        "m4", "api/run.py's v1 M4 stage is retired -- it drove a builder that no longer exists",
        "use the module API instead: POST /run {\"module\": \"m4\", \"phase\": "
        "\"seed|sync|analyze|render\"} -- see docs/module-api.md", log)


class ModuleFailure(Exception):
    def __init__(self, module, error, hint, log):
        super().__init__(error)
        self.module = module
        self.error = error
        self.hint = hint
        self.log = log


# --------------------------------------------------------------------------
# Top-level request handling
# This endpoint is retired. Its stage_m1/m2/m3/m4 builders below still speak v1's
# flag convention: they pass "--live" to opt IN to live calls. In v2 the live lane is
# the default in every module and "--offline" is the opt-out, so these builders would
# silently run the live lane when a caller asked for offline, and M2 would fail on an
# unrecognised argument. The supported entry point is the module API in api/server.py
# (POST /run with a module and a phase, documented in docs/module-api.md); nothing in
# the shipped UI calls this path any more. api/server.py still imports this file for
# helpers (REPO_ROOT, run_module, build_env, the summarisers and artifact specs), so
# the file stays -- only the run endpoint is closed, rather than left to misreport
# which lane it ran.
RETIRED_NOTE = (
    "api/run.py's /api/run endpoint is retired. It ran modules with v1's flag "
    "convention, where --live opted in; v2 runs live by default and --offline opts "
    "out, so this path could run the live lane when offline was requested. Use the "
    "module API instead: POST /run {\"module\": \"m1|m2|m3|m4\", \"phase\": ..., "
    "\"live\": true|false} -- see docs/module-api.md and docs/deploy-module-api.md."
)


def handle_run(payload: dict) -> dict:
    return {"ok": False, "module": str(payload.get("module", "?")), "error": RETIRED_NOTE,
            "hint": "see docs/module-api.md for the module API this was replaced by",
            "log": []}


def _handle_run_v1_disabled(payload: dict) -> dict:
    if REPO_ROOT is None:
        return {"ok": False, "module": payload.get("module", "?"), "error": REPO_ROOT_ERROR,
                "hint": "this deployment is missing modules/config/data next to api/run.py", "log": []}

    module = payload.get("module")
    if module not in ("m1", "m2", "m3", "m4", "chain"):
        return {"ok": False, "module": str(module), "error": f"invalid 'module': {module!r} (must be one of m1, m2, m3, m4, chain)",
                "hint": "set \"module\" to one of: m1, m2, m3, m4, chain", "log": []}

    live_requested = bool(payload.get("live", False))
    registrants_csv = payload.get("registrants_csv")
    transcript_md = payload.get("transcript_md")
    event_name = payload.get("event_name")
    for field, value in (("registrants_csv", registrants_csv), ("transcript_md", transcript_md), ("event_name", event_name)):
        if value is not None and not isinstance(value, str):
            return {"ok": False, "module": module, "error": f"'{field}' must be a string if provided",
                    "hint": f"remove {field} or pass it as a JSON string", "log": []}

    # v1's two remaining M4 inputs (its builder's --engagement/--segments;
    # v2's dashboard.py reads both from data/fixtures/ instead): sent as
    # parsed JSON objects in the request body, not strings -- the underlying
    # files are already JSON, so no double-encoding round trip.
    engagement_json = payload.get("engagement_json")
    segments_json = payload.get("segments_json")
    for field, value in (("engagement_json", engagement_json), ("segments_json", segments_json)):
        if value is not None and not isinstance(value, dict):
            return {"ok": False, "module": module, "error": f"'{field}' must be a JSON object if provided",
                    "hint": f"remove {field} or pass it as a parsed object (same shape as data/fixtures/{field.replace('_json','')}.json)",
                    "log": []}

    key_present = openrouter_key_present()
    notes = []
    if live_requested and not key_present:
        notes.append("live:true was requested but no OPENROUTER_API_KEY is present on this deployment "
                      "-- ran the deterministic offline lane and said so.")
        live_requested = False

    tmp_dir = Path(tempfile.mkdtemp(prefix="postevent-run-", dir=tempfile.gettempdir()))
    try:
        reg_path, ev_path, tr_path, eng_path, seg_path, custom_inputs = materialize_event(
            tmp_dir, registrants_csv, transcript_md, event_name, engagement_json, segments_json)
        out_root = tmp_dir / "out"
        log = []
        start = time.perf_counter()

        if module == "chain":
            effective_live = False
            if live_requested:
                notes.append(CHAIN_LIVE_NOTE)
            m1_out, m2_out, m3_out, m4_out = out_root / "m1", out_root / "m2", out_root / "m3", out_root / "m4"
            try:
                stage_m1(m1_out, reg_path, effective_live, log)
                stage_m2(m2_out, m1_out / "hubspot_ready.csv", ev_path, tr_path, effective_live, log)
                stage_m3(m3_out, ev_path, tr_path, effective_live, log)
                m4_result = stage_m4(m4_out, m1_out / "hubspot_ready.csv", eng_path, seg_path, log)
            except ModuleFailure as fail:
                return {"ok": False, "module": fail.module, "error": fail.error, "hint": fail.hint, "log": fail.log}

            summary = {
                "m1": summarize_m1(m1_out), "m2": summarize_m2(m2_out),
                "m3": summarize_m3(m3_out), "m4": summarize_m4(m4_result["stdout"]),
            }
            artifacts = (
                collect_artifacts(m1_out, M1_ARTIFACT_SPECS, name_prefix="m1/")
                + collect_artifacts(m2_out, m2_artifact_specs(m2_out), name_prefix="m2/")
                + collect_artifacts(m3_out, M3_ARTIFACT_SPECS, name_prefix="m3/")
                + collect_artifacts(m4_out, M4_ARTIFACT_SPECS, name_prefix="m4/")
            )
            if custom_inputs and not effective_live:
                notes.append("offline M2/M3 replay cached sample copy regardless of your registrants/transcript "
                              "content -- only M1 (rule-based) and M4 (pure computation) actually reflect your "
                              "input offline. Pass live:true on module:'m1', 'm2', or 'm3' individually to see "
                              "real generation from your data.")
            return {"ok": True, "module": "chain", "lane": "offline", "seconds": round(time.perf_counter() - start, 2),
                    "model": None, "summary": summary, "artifacts": artifacts, "log": log, "notes": notes}

        out_dir = out_root / module
        model = FAST_MODEL_CHAIN.split(",")[0] if live_requested else None
        # Default lane assumption; each branch below can only ever downgrade
        # this from evidence the module itself emitted -- never upgrade it,
        # and never trust exit code 0 alone as proof a model actually ran
        # (see classify_m1_lane's docstring for why that assumption is false
        # for m1 today).
        lane = "live" if live_requested else "offline"
        try:
            if module == "m1":
                m1_result = stage_m1(out_dir, reg_path, live_requested, log)
                summary = summarize_m1(out_dir)
                artifacts = collect_artifacts(out_dir, M1_ARTIFACT_SPECS)
                if live_requested:
                    lane, reason = classify_m1_lane(m1_result["stderr"], out_dir)
                    if lane == "degraded":
                        notes.append(degraded_note("m1", reason, key_present))
            elif module == "m2":
                stage_m2(out_dir, None, ev_path, tr_path, live_requested, log)
                summary = summarize_m2(out_dir)
                artifacts = collect_artifacts(out_dir, m2_artifact_specs(out_dir))
                # m2's --live path fails loud on any backend problem (see
                # comms.py's _llm_call docstring) -- there is no silent
                # rule-table fallback to detect here, so reaching this line
                # with live_requested already proves real generation landed.
                if custom_inputs and not live_requested:
                    notes.append("offline lane replays cached sample copy fingerprinted to the bundled fixture "
                                  "-- it does not regenerate from your transcript. Pass live:true to see real "
                                  "generation from your data.")
            elif module == "m3":
                stage_m3(out_dir, ev_path, tr_path, live_requested, log)
                summary = summarize_m3(out_dir)
                artifacts = collect_artifacts(out_dir, M3_ARTIFACT_SPECS)
                # same fail-loud guarantee as m2 -- see call_llm in repurpose.py.
                if custom_inputs and not live_requested:
                    notes.append("offline lane replays cached sample copy fingerprinted to the bundled fixture "
                                  "-- it does not regenerate from your transcript. Pass live:true to see real "
                                  "generation from your data.")
            elif module == "m4":
                # M4 has no --live flag (pure computation over M1 output + fixtures);
                # it needs an M1 run first, so run M1 offline here to feed it.
                m1_out = out_root / "m1"
                stage_m1(m1_out, reg_path, False, log)
                result = stage_m4(out_dir, m1_out / "hubspot_ready.csv", eng_path, seg_path, log)
                summary = summarize_m4(result["stdout"])
                artifacts = collect_artifacts(out_dir, M4_ARTIFACT_SPECS)
                if live_requested:
                    # live was requested but this module has zero capacity to honor
                    # it -- that is exactly the definition of "degraded", not "live".
                    lane = "degraded"
                    notes.append("v1's M4 builder had no --live flag -- it was pure computation over M1's "
                                  "output plus engagement/segments fixtures. Ran M1 offline to feed it (M1 "
                                  "itself has no LLM dependency for the fields M4 reads). This response is "
                                  "labelled lane:'degraded' because live:true was requested but no model call "
                                  "was ever possible for m4 on this retired path. v2's M4 does have a live "
                                  "lane -- dashboard.py's analyze phase -- reachable only via api/server.py.")
                    model = None
        except ModuleFailure as fail:
            return {"ok": False, "module": fail.module, "error": fail.error, "hint": fail.hint, "log": fail.log}

        return {"ok": True, "module": module, "lane": lane,
                "seconds": round(time.perf_counter() - start, 2), "model": model,
                "summary": summary, "artifacts": artifacts, "log": log, "notes": notes}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_get_response(probe: bool = False) -> dict:
    """ok reflects real deployment health (repo_root_ok) only -- it is
    deliberately NOT downgraded by a failed LLM probe, since the offline
    lane works perfectly well with no OpenRouter key at all and a down
    provider is not a broken deployment. See api/vercel-api-notes.md for
    the probe= contract (opt-in query param, cached) this documents."""
    repo_root_ok = REPO_ROOT is not None
    key_present = openrouter_key_present() if repo_root_ok else False
    resp = {
        "ok": repo_root_ok,
        "repo_root_ok": repo_root_ok,
        "service": "post-event engine module runner",
        "live_available": key_present,
        "model_chain": FAST_MODEL_CHAIN.split(","),
        "modules": ["m1", "m2", "m3", "m4", "chain"],
    }
    if not repo_root_ok:
        resp["error"] = REPO_ROOT_ERROR
    if probe:
        resp["llm_liveness_probe"] = probe_openrouter_liveness(get_openrouter_key()) if key_present else {
            "ok": False, "detail": "no OPENROUTER_API_KEY present -- nothing to probe",
        }
    return resp


# --------------------------------------------------------------------------
# HTTP handler (Vercel Python runtime contract: module-level `handler`)
class handler(BaseHTTPRequestHandler):
    # CORS: this endpoint is meant to be called from more than one origin --
    # the Vercel-hosted console at the site root, the control room page
    # embedded straight from docs/index.html (which also ships inside the
    # submission zip and gets opened as a bare file:// page -- an "Origin:
    # null" request with no way to allowlist a specific domain), and any
    # reviewer's own copy of either page. Wide-open GET/POST with no
    # credentials is the deliberate tradeoff (no auth, no cookies, nothing
    # sensitive in the response) -- see api/vercel-api-notes.md.
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        # Browsers preflight any cross-origin POST with a JSON content-type
        # (it's not a CORS "simple request") -- without this, the actual
        # POST from a page on a different origin (or file://) never fires.
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # probe=1 opts into a live 1-token OpenRouter ping (see
        # probe_openrouter_liveness) instead of running one on every status
        # call -- also cached PROBE_CACHE_TTL_S regardless, so a reviewer
        # rapidly refreshing the status page can't repeatedly burn quota.
        query = parse_qs(urlparse(self.path).query)
        want_probe = query.get("probe", ["0"])[0].lower() in ("1", "true", "yes")
        self._send_json(200, build_get_response(probe=want_probe))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(200, {"ok": False, "module": "?", "error": f"malformed JSON body: {exc}",
                                       "hint": "send a JSON object with at least {\"module\": \"m1\"}", "log": []})
                return
            if not isinstance(payload, dict):
                self._send_json(200, {"ok": False, "module": "?", "error": "request body must be a JSON object",
                                       "hint": "send a JSON object with at least {\"module\": \"m1\"}", "log": []})
                return
            result = handle_run(payload)
            self._send_json(200, result)
        except Exception as exc:  # noqa: BLE001 -- top-level handler must never 500 silently
            self._send_json(200, {"ok": False, "module": "?", "error": f"unhandled {type(exc).__name__}: {exc}",
                                   "hint": "this is a bug in api/run.py -- see server logs", "log": traceback.format_exc().splitlines()[-20:]})

    def log_message(self, format, *args):  # noqa: A002 -- quiet stdlib default access log
        pass
