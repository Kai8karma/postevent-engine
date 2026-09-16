#!/usr/bin/env python3
"""Long-running HTTP module-runner service -- see docs/module-api.md for the
full contract (endpoints, request/response shape, env vars).

Unlike api/run.py (a Vercel serverless function capped at ~60s), this
process has no wall-clock limit imposed by a host -- it runs on Railway next
to n8n. It reuses api/run.py's generic subprocess runner and per-module
summarize/artifact helpers (imported as `legacy` below) with its 20s/45s
timeouts overridden to a generous 1800s subprocess cap, since there is no
serverless deadline to protect here.

M1's prepare/finalize phases need flags enrich.py may not have yet (this
service was built while enrich.py was gaining --offline/--clay-results/
--emit-clay-domains/--limit-rows concurrently) -- see `enrich_help()` and
`m1_cmd()`: every one of those flags is only ever added after confirming
enrich.py's own --help advertises it, and a dropped flag is always logged
in the response's `notes`, never silently swallowed.

Stdlib only -- same constraint as every module this drives.
"""
import argparse
import json
import mimetypes
import os
import re
import socketserver
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as legacy  # noqa: E402 -- api/run.py, same directory; see module docstring

REPO_ROOT = legacy.REPO_ROOT
if REPO_ROOT is None:
    sys.exit(f"cannot start: {legacy.REPO_ROOT_ERROR}")

# This service has no serverless deadline -- remove api/run.py's Vercel-cap
# timeouts wholesale rather than duplicating stage_m1/m2/m3/m4's cmd-building
# logic just to change one number.
SUBPROCESS_TIMEOUT_S = 1800
legacy.OFFLINE_TIMEOUT_S = SUBPROCESS_TIMEOUT_S
legacy.LIVE_TIMEOUT_S = SUBPROCESS_TIMEOUT_S

OUT_API_ROOT = REPO_ROOT / "out" / "api"
MODULE_API_TOKEN = os.environ.get("MODULE_API_TOKEN", "")
DEV_MODE = False  # set from --dev in main()

ENRICH_SCRIPT = REPO_ROOT / "modules" / "m1-enrichment" / "enrich.py"
PUSH_SCRIPT = REPO_ROOT / "modules" / "m1-enrichment" / "push_to_hubspot.py"
COMMS_SCRIPT = REPO_ROOT / "modules" / "m2-comms" / "comms.py"
LOG_DISPATCH_SCRIPT = REPO_ROOT / "modules" / "m2-comms" / "log_dispatch.py"
REPURPOSE_SCRIPT = REPO_ROOT / "modules" / "m3-repurpose" / "repurpose.py"
TRANSCRIBE_BATCH_SCRIPT = REPO_ROOT / "modules" / "m3-repurpose" / "transcribe_batch.py"
BUILD_TRANSCRIPT_SCRIPT = REPO_ROOT / "data" / "incoming" / "tools" / "build_transcript.py"
MEDIA_DIR = REPO_ROOT / "data" / "incoming" / "media"
FFMPEG_TIMEOUT_S = 300  # per-chapter mono/16kHz extraction -- minutes, not SUBPROCESS_TIMEOUT_S's scale
BUILD_TRANSCRIPT_TIMEOUT_S = 60  # pure-Python turn assembly, no network
DEFAULT_EVENT_SLUG = "darwinbox-ai-in-hr-2026-08-13"  # the bundled fixture -- data/incoming/, not data/events/


def _git_sha() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
                               capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


GIT_SHA = _git_sha()

_HELP_CACHE = {}


def script_help(script_path: Path) -> str:
    """Cached --help text for one module script -- the single source of
    truth this service uses to decide whether a flag exists before ever
    passing it, per the brief's 'check --help at run time, drop unsupported
    flags, log it' rule."""
    key = str(script_path)
    if key not in _HELP_CACHE:
        try:
            proc = subprocess.run([legacy.PY, key, "--help"], capture_output=True, text=True, timeout=15)
            _HELP_CACHE[key] = (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            _HELP_CACHE[key] = ""
    return _HELP_CACHE[key]


def supports(script_path: Path, flag: str) -> bool:
    return flag in script_help(script_path)


# --------------------------------------------------------------------------
# Input resolution
class InputError(ValueError):
    """A caller-supplied input was invalid -- reported as ok:false, not a 500."""


def resolve_repo_relative(rel: str) -> Path:
    if not rel or Path(rel).is_absolute():
        raise InputError(f"registrants_csv_path must be a repo-relative path, got {rel!r}")
    if ".." in Path(rel).parts:
        raise InputError("registrants_csv_path must not contain '..'")
    candidate = (REPO_ROOT / rel).resolve()
    repo_resolved = REPO_ROOT.resolve()
    if candidate != repo_resolved and repo_resolved not in candidate.parents:
        raise InputError(f"registrants_csv_path resolves outside the repo: {rel}")
    if not candidate.exists():
        raise InputError(f"registrants_csv_path not found: {rel}")
    return candidate


def download_csv(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "postevent-module-api/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, data = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raise InputError(f"registrants_csv_url fetch failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InputError(f"registrants_csv_url fetch failed: {exc.reason}") from exc
    if status != 200:
        raise InputError(f"registrants_csv_url fetch failed: HTTP {status}")
    if not data:
        raise InputError("registrants_csv_url fetch returned an empty body")
    dest.write_bytes(data)
    return dest


def resolve_registrants_input(inputs: dict, out_dir: Path) -> Path:
    url = inputs.get("registrants_csv_url")
    rel_path = inputs.get("registrants_csv_path")
    if url:
        return download_csv(url, out_dir / "registrants.csv")
    if rel_path:
        return resolve_repo_relative(rel_path)
    default = REPO_ROOT / "data" / "incoming" / "registrants.csv"
    if not default.exists():
        raise InputError("no registrants_csv_url/registrants_csv_path given and no default fixture found")
    return default


def resolve_optional_repo_path(inputs: dict, key: str, default: Path) -> Path:
    val = inputs.get(key)
    return resolve_repo_relative(val) if val else default


def resolve_event_dir(slug) -> Path:
    """data/incoming/ for the bundled default event (omitted slug or the
    literal DEFAULT_EVENT_SLUG); data/events/<slug>/ for any other --
    M3's inputs.event_slug support for multiple events, see
    docs/module-api.md's M3 'Multiple events' paragraph."""
    if not slug or slug == DEFAULT_EVENT_SLUG:
        return REPO_ROOT / "data" / "incoming"
    return REPO_ROOT / "data" / "events" / slug


def resolve_enriched_csv(inputs: dict, notes: list):
    """M2 generate's enriched_csv_path input, per docs/module-api.md: an
    explicit repo-relative path, else inputs.run_id's M1 output, else the
    most recently written M1 run under out/api/. None means "omit --enriched
    from comms.py's cmd" -- comms.py's own --enriched default (data/incoming/
    registrants.csv) takes over, per its --help text."""
    path = inputs.get("enriched_csv_path")
    if path:
        return resolve_repo_relative(path)
    run_id = inputs.get("run_id")
    if run_id:
        candidate = OUT_API_ROOT / run_id / "m1" / "hubspot_ready.csv"
        if not candidate.exists():
            raise InputError(f"no hubspot_ready.csv for inputs.run_id {run_id!r} at {candidate}")
        return candidate
    candidates = sorted(OUT_API_ROOT.glob("m1-*/m1/hubspot_ready.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    notes.append("no inputs.enriched_csv_path/run_id given and no prior M1 run found under out/api/ -- "
                 "omitting --enriched (comms.py falls back to its own default)")
    return None


# --------------------------------------------------------------------------
# M1 (prepare / finalize / omitted) -- see docs/module-api.md's module-phases table
def determine_lane(quality_report: dict, live_requested: bool) -> str:
    if isinstance(quality_report, dict) and quality_report.get("lane"):
        return quality_report["lane"]
    backend_env_present = bool(os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_BACKEND"))
    return "live" if backend_env_present else "offline"


def run_push_stage(out_dir: Path, log: list) -> tuple:
    """push_to_hubspot.py --in out_dir expects out_dir/m1/hubspot_companies.csv
    -- exactly what m1_cmd() below writes there. Dry-run substitutes for a
    real push when HUBSPOT_TOKEN is absent (never a hard failure -- a
    missing token is not this service's fault)."""
    notes = []
    receipts_dir = out_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / "hubspot_push.json"
    cmd = [legacy.PY, str(PUSH_SCRIPT), "--in", str(out_dir), "--verify", "--receipt", str(receipt_path)]
    token_present = bool(os.environ.get("HUBSPOT_TOKEN"))
    if not token_present:
        cmd.append("--dry-run")
        notes.append("HUBSPOT_TOKEN absent -- push stage ran --dry-run (zero network) instead of a real push")
    log.append(f"[m1-push] {'dry-run' if not token_present else 'live'} -- invoking push_to_hubspot.py")
    result = legacy.run_module(cmd, dict(os.environ), SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if not result["ok"]:
        notes.append(f"push_to_hubspot.py exited {result['returncode']} -- see log")
        return None, notes
    return receipt_path, notes


def m1_cmd(reg_path: Path, m1_out: Path, phase, live_requested: bool, inputs: dict, notes: list) -> list:
    help_text = script_help(ENRICH_SCRIPT)
    key_present = legacy.openrouter_key_present()
    cmd = [legacy.PY, str(ENRICH_SCRIPT), "--in", str(reg_path),
           "--config", str(REPO_ROOT / "config" / "icp.yaml"),
           "--hubspot", str(REPO_ROOT / "data" / "fixtures" / "hubspot_existing.json"),
           "--out", str(m1_out)]

    if live_requested and key_present:
        cmd.append("--live")
    elif not live_requested:
        if "--offline" in help_text:
            cmd.append("--offline")
        else:
            notes.append("enrich.py has no --offline flag yet -- omitted (offline is its no-flag default)")

    if phase == "prepare":
        if "--emit-clay-domains" in help_text:
            cmd += ["--emit-clay-domains", str(m1_out / "clay_domains.json")]
        else:
            notes.append("enrich.py does not support --emit-clay-domains yet -- next.clay_domains unavailable")

    clay_results = inputs.get("clay_results")
    if phase == "finalize" and clay_results:
        if "--clay-results" in help_text:
            clay_results_path = m1_out.parent / "clay_results.json"
            clay_results_path.write_text(json.dumps(clay_results), encoding="utf-8")
            cmd += ["--clay-results", str(clay_results_path)]
        else:
            notes.append("enrich.py does not support --clay-results yet -- inputs.clay_results ignored")

    limit_rows = inputs.get("limit_rows")
    if limit_rows is not None:
        if "--limit-rows" in help_text:
            cmd += ["--limit-rows", str(int(limit_rows))]
        else:
            notes.append("enrich.py does not support --limit-rows yet -- inputs.limit_rows ignored")

    return cmd


def run_m1(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    phase = payload.get("phase")
    live_requested = bool(payload.get("live", False))
    notes = []

    try:
        reg_path = resolve_registrants_input(inputs, out_dir)
    except InputError as exc:
        return {"ok": False, "module": "m1", "phase": phase, "error": str(exc), "log": log, "notes": notes}

    m1_out = out_dir / "m1"
    m1_out.mkdir(parents=True, exist_ok=True)
    cmd = m1_cmd(reg_path, m1_out, phase, live_requested, inputs, notes)

    log.append(f"[m1] phase={phase or '(both)'} live={live_requested} -- invoking enrich.py")
    env = legacy.build_env(live_requested and legacy.openrouter_key_present())
    result = legacy.run_module(cmd, env, SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"] or not result["ok"]:
        error = (f"enrich.py timed out after {result['elapsed']:.1f}s" if result["timed_out"]
                 else f"enrich.py exited {result['returncode']}")
        return {"ok": False, "module": "m1", "phase": phase, "error": error, "log": log, "notes": notes}

    summary = {}
    try:
        summary = legacy.summarize_m1(m1_out)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        notes.append(f"could not summarize m1 output: {exc}")

    quality = {}
    quality_path = m1_out / "quality_report.json"
    if quality_path.exists():
        try:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    artifacts = {rel: f"/artifacts/{run_id}/{rel}" for rel, _kind in legacy.M1_ARTIFACT_SPECS
                 if (m1_out / rel).exists()}

    next_block = {}
    clay_domains_path = m1_out / "clay_domains.json"
    if clay_domains_path.exists():
        try:
            next_block["clay_domains"] = json.loads(clay_domains_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    receipts = []
    if phase in (None, "finalize"):
        receipt_path, push_notes = run_push_stage(out_dir, log)
        notes.extend(push_notes)
        if receipt_path is not None:
            receipts.append(f"/artifacts/{run_id}/receipts/{receipt_path.name}")

    return {
        "ok": True, "module": "m1", "phase": phase, "lane": determine_lane(quality, live_requested),
        "run_id": run_id, "model": legacy.FAST_MODEL_CHAIN.split(",")[0] if live_requested else None,
        "summary": summary, "artifacts": artifacts, "receipts": receipts,
        "next": next_block, "notes": notes, "log": log,
    }


# --------------------------------------------------------------------------
# M2 (generate / approve / log) -- see docs/module-api.md's "M2 -- phases and
# files" table for the dispatch_plan.json / dispatch_results.json schemas
# this section builds against. Handled separately from run_generic() below
# (unlike M3/M4, M2's three phases don't share stage_m2()'s old comms.json/
# sends_log.json contract -- that legacy function is untouched but no longer
# called for m2).
def m2_generate_cmd(m2_out: Path, enriched_path, event_path: Path, segments_path: Path,
                     transcript_path: Path, live_requested: bool, notes: list) -> list:
    help_text = script_help(COMMS_SCRIPT)
    cmd = [legacy.PY, str(COMMS_SCRIPT), "--out", str(m2_out), "--event", str(event_path),
           "--segments", str(segments_path), "--transcript", str(transcript_path)]
    if enriched_path is not None:
        cmd += ["--enriched", str(enriched_path)]

    if live_requested:
        if "--live" in help_text:
            cmd.append("--live")
        # else: comms.py is live by default once the M2 rewrite lands -- no flag needed.
    elif "--offline" in help_text:
        cmd.append("--offline")
    elif "--live" in help_text:
        notes.append("comms.py has no --offline flag yet -- omitted (offline is its no-flag default while --live exists)")
    else:
        notes.append("comms.py exposes neither --offline nor --live in --help -- cannot confirm lane; running with no flags")
    return cmd


def run_m2_generate(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    live_requested = bool(payload.get("live", False))
    notes = []
    m2_out = out_dir / "m2"
    m2_out.mkdir(parents=True, exist_ok=True)

    try:
        enriched_path = resolve_enriched_csv(inputs, notes)
        event_path = resolve_optional_repo_path(inputs, "event_json_path", REPO_ROOT / "data" / "incoming" / "event.json")
        transcript_path = resolve_optional_repo_path(inputs, "transcript_md_path", REPO_ROOT / "data" / "incoming" / "transcript.md")
    except InputError as exc:
        return {"ok": False, "module": "m2", "phase": "generate", "error": str(exc), "log": log, "notes": notes}

    segments_path = REPO_ROOT / "data" / "fixtures" / "segments.json"
    cmd = m2_generate_cmd(m2_out, enriched_path, event_path, segments_path, transcript_path, live_requested, notes)

    log.append(f"[m2-generate] live={live_requested} -- invoking comms.py")
    env = legacy.build_env(live_requested and legacy.openrouter_key_present())
    result = legacy.run_module(cmd, env, SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"] or not result["ok"]:
        error = (f"comms.py timed out after {result['elapsed']:.1f}s" if result["timed_out"]
                 else f"comms.py exited {result['returncode']}")
        return {"ok": False, "module": "m2", "phase": "generate", "error": error, "log": log, "notes": notes}

    summary = {}
    plan_path = m2_out / "dispatch_plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            # subjects: {segment: [subject_a, subject_b]} -- per-request addition, read
            # straight from dispatch_plan.json's own variants block, never recomputed.
            subjects = {seg: [variant.get("subject_a"), variant.get("subject_b")]
                        for seg, variant in (plan.get("variants") or {}).items()}
            summary = {"recipients": len(plan.get("recipients", [])), "counts": plan.get("counts", {}),
                       "approval_status": (plan.get("approval") or {}).get("status"),
                       "event_close_ts": plan.get("event_close_ts"), "event_slug": plan.get("event_slug"),
                       "subjects": subjects}
        except json.JSONDecodeError:
            notes.append("dispatch_plan.json present but not valid JSON")
    else:
        notes.append(f"comms.py did not write dispatch_plan.json at {plan_path} -- see log")

    artifacts = {rel: f"/artifacts/{run_id}/{rel}" for rel in ("dispatch_plan.json", "approval_gate.json")
                 if (m2_out / rel).exists()}
    emails_dir = m2_out / "emails"
    if emails_dir.is_dir():
        for p in sorted(emails_dir.glob("*.md")):
            artifacts[f"emails/{p.name}"] = f"/artifacts/{run_id}/emails/{p.name}"

    receipts = []
    receipt_path = m2_out / "receipts" / "m2_llm_calls.json"
    if receipt_path.exists():
        receipts.append(f"/artifacts/{run_id}/receipts/{receipt_path.name}")

    return {"ok": True, "module": "m2", "phase": "generate", "lane": "live" if live_requested else "offline",
            "run_id": run_id, "model": legacy.FAST_MODEL_CHAIN.split(",")[0] if live_requested else None,
            "summary": summary, "artifacts": artifacts, "receipts": receipts,
            "next": {}, "notes": notes, "log": log}


def run_m2_approve(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    approved_by = inputs.get("approved_by")
    mode = inputs.get("mode")
    notes = []
    m2_out = out_dir / "m2"
    plan_path = m2_out / "dispatch_plan.json"

    if mode not in ("demo", "full"):
        return {"ok": False, "module": "m2", "phase": "approve",
                "error": f"inputs.mode must be 'demo' or 'full', got {mode!r}", "log": log, "notes": notes}
    if not approved_by:
        return {"ok": False, "module": "m2", "phase": "approve",
                "error": "inputs.approved_by is required", "log": log, "notes": notes}
    if not plan_path.exists():
        return {"ok": False, "module": "m2", "phase": "approve",
                "error": f"no dispatch_plan.json at {plan_path} -- run the generate phase for this run_id first",
                "log": log, "notes": notes}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "module": "m2", "phase": "approve", "error": f"dispatch_plan.json is not valid JSON: {exc}",
                "log": log, "notes": notes}

    approved_at = datetime.now(timezone.utc).isoformat()
    plan.setdefault("approval", {})
    plan["approval"].update({"status": "approved", "approved_by": approved_by, "approved_at": approved_at, "mode": mode})
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    gate = {"status": "approved", "approved_by": approved_by, "approved_at": approved_at, "mode": mode}
    (m2_out / "approval_gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")

    recipients = plan.get("recipients", [])
    if mode == "demo":
        filtered = [r for r in recipients if r.get("demo_redirect_to") or r.get("segment") == "speaker"]
    else:
        filtered = [r for r in recipients if r.get("email")]

    return {"ok": True, "module": "m2", "phase": "approve", "lane": plan.get("lane", "offline"), "run_id": run_id,
            "model": None, "summary": {"counts": {"selected": len(filtered), "mode": mode}},
            "recipients": filtered,
            "artifacts": {"dispatch_plan.json": f"/artifacts/{run_id}/dispatch_plan.json",
                          "approval_gate.json": f"/artifacts/{run_id}/approval_gate.json"},
            "receipts": [], "next": {}, "notes": notes, "log": log}


def run_m2_log(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    results = inputs.get("results")
    dry_run = bool(inputs.get("dry_run", False))
    notes = []
    m2_out = out_dir / "m2"
    plan_path = m2_out / "dispatch_plan.json"

    if not plan_path.exists():
        return {"ok": False, "module": "m2", "phase": "log",
                "error": f"no dispatch_plan.json at {plan_path} -- run generate (and approve) first",
                "log": log, "notes": notes}
    if results is None:
        return {"ok": False, "module": "m2", "phase": "log",
                "error": "inputs.results is required (n8n's dispatch_results.json array)", "log": log, "notes": notes}

    results_path = m2_out / "dispatch_results.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    receipts_dir = out_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / "m2_hubspot_log.json"

    token_present = bool(os.environ.get("HUBSPOT_TOKEN"))
    cmd = [legacy.PY, str(LOG_DISPATCH_SCRIPT), "--plan", str(plan_path),
           "--results", str(results_path), "--receipt", str(receipt_path)]
    if dry_run or not token_present:
        cmd.append("--dry-run")
        if not token_present and not dry_run:
            notes.append("HUBSPOT_TOKEN absent -- log phase ran --dry-run (zero network) instead of a real push")

    log.append(f"[m2-log] {'dry-run' if (dry_run or not token_present) else 'live'} -- invoking log_dispatch.py")
    result = legacy.run_module(cmd, dict(os.environ), SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if not result["ok"]:
        return {"ok": False, "module": "m2", "phase": "log", "error": f"log_dispatch.py exited {result['returncode']}",
                "log": log, "notes": notes}

    receipt = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            notes.append("m2_hubspot_log.json present but not valid JSON")

    return {"ok": True, "module": "m2", "phase": "log", "lane": "offline" if (dry_run or not token_present) else "live",
            "run_id": run_id, "model": None,
            "summary": {"logged": len(receipt.get("logged", [])), "skipped": len(receipt.get("skipped", [])),
                        "errors": len(receipt.get("errors", []))},
            "artifacts": {"dispatch_results.json": f"/artifacts/{run_id}/dispatch_results.json"},
            "receipts": [f"/artifacts/{run_id}/receipts/{receipt_path.name}"] if receipt_path.exists() else [],
            "next": {}, "notes": notes, "log": log, "receipt": receipt}


def run_m2(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    phase = payload.get("phase")
    if phase == "generate":
        return run_m2_generate(payload, run_id, out_dir, log)
    if phase == "approve":
        return run_m2_approve(payload, run_id, out_dir, log)
    if phase == "log":
        return run_m2_log(payload, run_id, out_dir, log)
    return {"ok": False, "module": "m2", "phase": phase,
            "error": f"invalid m2 phase {phase!r} (must be one of generate, approve, log)", "log": log, "notes": []}


# --------------------------------------------------------------------------
# M3 (transcribe / run / record) -- see docs/module-api.md's "M3 -- phases
# and files" table (and its "Multiple events" paragraph) for the exact
# manifest.json / drive-record / event_slug contract this section builds
# against. repurpose.py is being rewritten concurrently to gain
# --clips/--images/--offline (same script_help()-at-call-time pattern
# already used by M1/M2 above) -- every flag below is only ever added
# after confirming its own live --help advertises it, and a dropped flag
# is always logged in notes, never silently swallowed.
def default_speaker_map(speakers: list) -> dict:
    """0=<host>, 1=<next speaker>, ... in event.json.speakers order, host
    first regardless of its position in the list (build_transcript.py's
    --speaker-map keys are Sarvam's numeric diarization speaker ids)."""
    ordered = sorted(speakers, key=lambda s: 0 if (s.get("role") or "").strip().lower() == "host" else 1)
    return {str(i): s.get("name", f"Speaker {i}") for i, s in enumerate(ordered)}


def download_binary(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "postevent-module-api/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            status, data = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raise InputError(f"audio download failed: HTTP {exc.code} ({url})") from exc
    except urllib.error.URLError as exc:
        raise InputError(f"audio download failed: {exc.reason} ({url})") from exc
    if status != 200:
        raise InputError(f"audio download failed: HTTP {status} ({url})")
    if not data:
        raise InputError(f"audio download returned an empty body ({url})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def resolve_recording_file(rec: dict, out_dir: Path, log: list) -> Path:
    """chapterN.mp4 at data/incoming/media/ (the repo's committed fixture
    media) wins over a download every time -- only reaches for rec['url']
    when that local file is missing."""
    chapter = rec["chapter"]
    local_name = f"chapter{chapter}.mp4"
    local_path = MEDIA_DIR / local_name
    if local_path.exists():
        log.append(f"[m3-transcribe] chapter {chapter}: using local {local_path}")
        return local_path
    url = rec.get("url")
    if not url:
        raise InputError(f"chapter {chapter}: no local file at {local_path} and no url to download it from")
    dest = out_dir / "media" / local_name
    log.append(f"[m3-transcribe] chapter {chapter}: downloading {url} -> {dest}")
    return download_binary(url, dest)


def extract_audio(mp4_path: Path, out_dir: Path, chapter: int, log: list) -> Path:
    mp3_path = out_dir / "audio" / f"chapter{chapter}.mp3"
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(mp4_path), "-ac", "1", "-ar", "16000", "-vn", str(mp3_path)]
    log.append(f"[m3-transcribe] chapter {chapter}: ffmpeg extract -> {mp3_path}")
    result = legacy.run_module(cmd, dict(os.environ), FFMPEG_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if not result["ok"]:
        raise InputError(f"ffmpeg extraction failed on chapter {chapter} (exit {result['returncode']}) -- see log")
    return mp3_path


def run_m3_transcribe(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    notes = []
    slug = inputs.get("event_slug")
    event_dir = resolve_event_dir(slug)
    event_val = inputs.get("event_json_path")
    try:
        event_path = resolve_repo_relative(event_val) if event_val else (event_dir / "event.json")
        if not event_path.exists():
            raise InputError(f"no event.json found at {event_path} (event_slug={slug!r}) -- for a non-default "
                              f"event_slug, put it at data/events/<slug>/event.json")
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (InputError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "module": "m3", "phase": "transcribe", "error": f"cannot load event.json: {exc}",
                "log": log, "notes": notes}

    audio_urls = inputs.get("audio_urls")
    if audio_urls:
        recordings = [{"chapter": i, "url": u, "duration_sec": None} for i, u in enumerate(audio_urls, start=1)]
    else:
        recordings = sorted((dict(r) for r in event.get("recording_files", [])), key=lambda r: r["chapter"])
    if not recordings:
        return {"ok": False, "module": "m3", "phase": "transcribe",
                "error": "no inputs.audio_urls given and event.json has no recording_files", "log": log, "notes": notes}

    mp3_by_chapter = []
    try:
        for rec in recordings:
            mp4_path = resolve_recording_file(rec, out_dir, log)
            mp3_by_chapter.append((rec["chapter"], extract_audio(mp4_path, out_dir, rec["chapter"], log)))
    except InputError as exc:
        return {"ok": False, "module": "m3", "phase": "transcribe", "error": str(exc), "log": log, "notes": notes}

    transcription_dir = out_dir / "receipts" / "transcription"
    cmd = [legacy.PY, str(TRANSCRIBE_BATCH_SCRIPT)]
    for _, mp3_path in mp3_by_chapter:
        cmd += ["--audio", str(mp3_path)]
    cmd += ["--out", str(transcription_dir)]
    log.append(f"[m3-transcribe] invoking transcribe_batch.py for {len(mp3_by_chapter)} file(s)")
    result = legacy.run_module(cmd, dict(os.environ), SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if not result["ok"]:
        return {"ok": False, "module": "m3", "phase": "transcribe",
                "error": f"transcribe_batch.py exited {result['returncode']}", "log": log, "notes": notes}

    speaker_map = inputs.get("speaker_map") or default_speaker_map(event.get("speakers", []))
    transcript_path = out_dir / "transcript.md"
    cmd = [legacy.PY, str(BUILD_TRANSCRIPT_SCRIPT), "--event", str(event_path)]
    for chapter, _ in mp3_by_chapter:
        cmd += ["--chapter", f"{chapter}={transcription_dir / f'chapter{chapter}.sarvam.json'}"]
    for key, name in speaker_map.items():
        cmd += ["--speaker-map", f"{key}={name}"]
    cmd += ["--out", str(transcript_path)]
    log.append("[m3-transcribe] invoking build_transcript.py")
    result2 = legacy.run_module(cmd, dict(os.environ), BUILD_TRANSCRIPT_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result2["stdout"], result2["stderr"]))
    if not result2["ok"]:
        return {"ok": False, "module": "m3", "phase": "transcribe",
                "error": f"build_transcript.py exited {result2['returncode']}", "log": log, "notes": notes}

    m = re.search(r"turns=(\d+)\s+words=(\d+)", result2["stdout"])
    turns, words = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    total_seconds = sum(r.get("duration_sec") or 0 for r in recordings)

    receipt = {}
    tb_receipt_path = transcription_dir / "receipt.json"
    if tb_receipt_path.exists():
        try:
            receipt = json.loads(tb_receipt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            notes.append("transcribe_batch.py's receipt.json is not valid JSON")
    receipt["chapters"] = len(recordings)
    receipt["speaker_map"] = speaker_map
    (out_dir / "receipts").mkdir(parents=True, exist_ok=True)
    (out_dir / "receipts" / "transcription.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    artifacts = {"transcript.md": f"/artifacts/{run_id}/transcript.md",
                 "receipts/transcription.json": f"/artifacts/{run_id}/receipts/transcription.json"}
    for p in sorted(transcription_dir.glob("*.sarvam.json")):
        rel = f"receipts/transcription/{p.name}"
        artifacts[rel] = f"/artifacts/{run_id}/{rel}"

    return {"ok": True, "module": "m3", "phase": "transcribe", "lane": "live", "run_id": run_id, "model": None,
            "summary": {"chapters": len(recordings), "words": words, "turns": turns, "seconds": total_seconds},
            "artifacts": artifacts, "receipts": [f"/artifacts/{run_id}/receipts/transcription.json"],
            "next": {}, "notes": notes, "log": log}


def summarize_m3_run(out_dir: Path, manifest: dict) -> dict:
    """Reads repurpose.py's own output files -- never recomputes generation,
    only counts/measures what's already on disk (same discipline as
    legacy.summarize_m1/m2/m3)."""
    blog_path = out_dir / "blog.md"
    blog_words = len(blog_path.read_text(encoding="utf-8").split()) if blog_path.exists() else 0

    chapters = 0
    yt_path = out_dir / "youtube.md"
    if yt_path.exists():
        chapters = len(re.findall(r"^\d{1,2}:\d{2}\s+\S", yt_path.read_text(encoding="utf-8"), re.MULTILINE))

    linkedin_posts = x_posts = 0
    social_path = out_dir / "social.md"
    if social_path.exists():
        social_text = social_path.read_text(encoding="utf-8")
        linkedin_posts = len(re.findall(r"\*\*Platform:\*\*\s*LinkedIn", social_text))
        x_posts = len(re.findall(r"\*\*Platform:\*\*\s*X\b", social_text))

    files = manifest.get("files", [])
    images = sum(1 for f in files if f.get("kind") == "visual")
    clips = sum(1 for f in files if f.get("kind") == "clip")

    grounding = {"checked": 0, "passed": 0}
    grounding_path = out_dir / "grounding_report.json"
    if grounding_path.exists():
        try:
            totals = json.loads(grounding_path.read_text(encoding="utf-8")).get("totals", {})
            grounding = {"checked": totals.get("claims_checked", 0), "passed": totals.get("verified", 0)}
        except json.JSONDecodeError:
            pass

    return {"blog_words": blog_words, "chapters": chapters, "posts": {"linkedin": linkedin_posts, "x": x_posts},
            "images": images, "clips": clips, "grounding": grounding,
            "lane": manifest.get("lane"), "models": manifest.get("models", {})}


def resolve_run_transcript_and_event(payload: dict, inputs: dict) -> tuple:
    """Precedence: explicit inputs.transcript_md_path/event_json_path >
    this run_id's own freshly transcribed transcript.md (out/api/<run_id>/
    transcript.md, written by a prior 'transcribe' call reusing the same
    run_id) > inputs.event_slug's event dir > the bundled default event
    dir. See docs/module-api.md's M3 'Multiple events' paragraph. Returns
    (transcript_path, event_path, notes); raises InputError if nothing
    resolves to a real file (fail loud, never a silent fixture swap)."""
    notes = []
    run_id = payload.get("run_id")
    slug = inputs.get("event_slug")
    event_dir = resolve_event_dir(slug)

    transcript_val = inputs.get("transcript_md_path")
    if transcript_val:
        transcript_path = resolve_repo_relative(transcript_val)
    else:
        reused = OUT_API_ROOT / run_id / "transcript.md" if run_id else None
        if reused is not None and reused.exists():
            transcript_path = reused
            notes.append(f"no inputs.transcript_md_path given -- reused run {run_id!r}'s own transcript.md "
                         f"from its transcribe phase ({reused})")
        else:
            transcript_path = event_dir / "transcript.md"
    if not transcript_path.exists():
        raise InputError(f"no transcript.md found at {transcript_path} (event_slug={slug!r}) -- for a "
                          f"non-default event_slug, put the transcript at data/events/<slug>/transcript.md")

    event_val = inputs.get("event_json_path")
    event_path = resolve_repo_relative(event_val) if event_val else (event_dir / "event.json")
    if not event_path.exists():
        raise InputError(f"no event.json found at {event_path} (event_slug={slug!r})")

    return transcript_path, event_path, notes


def run_m3_run(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    live_requested = bool(payload.get("live", False))
    options = inputs.get("options") or {}
    notes = []
    try:
        transcript_path, event_path, resolve_notes = resolve_run_transcript_and_event(payload, inputs)
    except InputError as exc:
        return {"ok": False, "module": "m3", "phase": "run", "error": str(exc), "log": log, "notes": notes}
    notes.extend(resolve_notes)

    help_text = script_help(REPURPOSE_SCRIPT)
    cmd = [legacy.PY, str(REPURPOSE_SCRIPT), "--out", str(out_dir),
           "--event", str(event_path), "--transcript", str(transcript_path)]

    if live_requested:
        if "--live" in help_text:
            cmd.append("--live")
        else:
            notes.append("repurpose.py has no --live flag in its current --help -- cannot honor live:true")
    elif "--offline" in help_text:
        cmd.append("--offline")
    else:
        notes.append("repurpose.py does not support --offline yet -- omitted (its own no-flag default replays "
                      "sample_output, so this still runs zero LLM calls)")

    if options.get("clips"):
        if "--clips" in help_text:
            cmd.append("--clips")
        else:
            notes.append("repurpose.py does not support --clips yet -- inputs.options.clips ignored")
    if options.get("images"):
        if "--images" in help_text:
            cmd.append("--images")
        else:
            notes.append("repurpose.py does not support --images yet -- inputs.options.images ignored")

    log.append(f"[m3-run] live={live_requested} options={options} -- invoking repurpose.py")
    env = legacy.build_env(live_requested and legacy.openrouter_key_present())
    result = legacy.run_module(cmd, env, SUBPROCESS_TIMEOUT_S)
    log.extend(legacy.lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"] or not result["ok"]:
        error = (f"repurpose.py timed out after {result['elapsed']:.1f}s" if result["timed_out"]
                 else f"repurpose.py exited {result['returncode']}")
        return {"ok": False, "module": "m3", "phase": "run", "error": error, "log": log, "notes": notes}

    manifest = {}
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            notes.append("manifest.json present but not valid JSON")
    else:
        notes.append(f"repurpose.py exited 0 but did not write manifest.json at {manifest_path}")

    summary = summarize_m3_run(out_dir, manifest)

    artifacts = {}
    for rel in ("blog.md", "youtube.md", "infographic.md", "social.md", "extraction.json",
                "grounding_report.json", "manifest.json"):
        if (out_dir / rel).exists():
            artifacts[rel] = f"/artifacts/{run_id}/{rel}"
    for f in manifest.get("files", []):
        name = f.get("name")
        if name and name not in artifacts and (out_dir / name).exists():
            artifacts[name] = f"/artifacts/{run_id}/{name}"

    next_files = []
    for f in manifest.get("files", []):
        name = f.get("name")
        if not name:
            continue
        next_files.append({"name": name, "path": str(out_dir / name), "url": f"/artifacts/{run_id}/{name}",
                            "mime": guess_content_type(out_dir / name), "kind": f.get("kind")})

    receipts = [f"/artifacts/{run_id}/{rel}" for rel in
                ("receipts/m3_llm_calls.json", "receipts/m3_images.json", "receipts/m3_clips.json")
                if (out_dir / rel).exists()]

    lane = manifest.get("lane") or ("live" if live_requested else "offline")
    model = (manifest.get("models") or {}).get("text")

    return {"ok": True, "module": "m3", "phase": "run", "lane": lane, "run_id": run_id, "model": model,
            "summary": summary, "artifacts": artifacts, "receipts": receipts,
            "next": {"files": next_files}, "notes": notes, "log": log}


def run_m3_record(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    inputs = payload.get("inputs") or {}
    drive = inputs.get("drive")
    notes = []
    if not isinstance(drive, dict):
        return {"ok": False, "module": "m3", "phase": "record",
                "error": "inputs.drive is required (n8n's {folder_url, folder_id, files: [...]})",
                "log": log, "notes": notes}

    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "module": "m3", "phase": "record",
                "error": f"no manifest.json at {manifest_path} -- run the m3 'run' phase for this run_id first",
                "log": log, "notes": notes}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "module": "m3", "phase": "record",
                "error": f"manifest.json is not valid JSON: {exc}", "log": log, "notes": notes}

    drive_manifest = {"run_id": run_id, "recorded_at": datetime.now(timezone.utc).isoformat(), **drive}
    (out_dir / "drive_manifest.json").write_text(json.dumps(drive_manifest, indent=2), encoding="utf-8")

    manifest["shared_drive"] = drive
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    files = drive.get("files") or []
    return {"ok": True, "module": "m3", "phase": "record", "lane": manifest.get("lane", "offline"),
            "run_id": run_id, "model": None,
            "summary": {"files_recorded": len(files), "folder_url": drive.get("folder_url")},
            "artifacts": {"drive_manifest.json": f"/artifacts/{run_id}/drive_manifest.json",
                          "manifest.json": f"/artifacts/{run_id}/manifest.json"},
            "receipts": [], "next": {}, "notes": notes, "log": log}


def run_m3(payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    phase = payload.get("phase")
    if phase == "transcribe":
        return run_m3_transcribe(payload, run_id, out_dir, log)
    if phase == "run":
        return run_m3_run(payload, run_id, out_dir, log)
    if phase == "record":
        return run_m3_record(payload, run_id, out_dir, log)
    return {"ok": False, "module": "m3", "phase": phase,
            "error": f"invalid m3 phase {phase!r} (must be one of transcribe, run, record)", "log": log, "notes": []}


# --------------------------------------------------------------------------
# M4 -- no phases of its own; still reuses api/run.py's stage_m1/stage_m4
# directly (with the module-level timeout monkeypatch above already
# applied) -- unchanged by the M3 rewrite above, which only split M3 out of
# what used to be this same function.
def run_generic(module: str, payload: dict, run_id: str, out_dir: Path, log: list) -> dict:
    live_requested = bool(payload.get("live", False))
    try:
        m1_out = out_dir / "m1"
        legacy.stage_m1(m1_out, REPO_ROOT / "data" / "incoming" / "registrants.csv", False, log)
        result = legacy.stage_m4(out_dir, m1_out / "hubspot_ready.csv",
                                  REPO_ROOT / "data" / "fixtures" / "engagement.json",
                                  REPO_ROOT / "data" / "fixtures" / "segments.json", log)
    except legacy.ModuleFailure as fail:
        return {"ok": False, "module": module, "error": fail.error, "hint": fail.hint, "log": fail.log}

    summary = legacy.summarize_m4(result["stdout"])
    artifacts = {rel: f"/artifacts/{run_id}/{rel}" for rel, _kind in legacy.M4_ARTIFACT_SPECS if (out_dir / rel).exists()}
    return {
        "ok": True, "module": module, "phase": payload.get("phase"),
        "lane": "live" if live_requested else "offline", "run_id": run_id,
        "model": legacy.FAST_MODEL_CHAIN.split(",")[0] if live_requested else None,
        "summary": summary, "artifacts": artifacts, "receipts": [], "next": {}, "notes": [], "log": log,
    }


# --------------------------------------------------------------------------
# Dispatcher
def handle_run_request(payload: dict) -> dict:
    module = payload.get("module")
    if module not in ("m1", "m2", "m3", "m4"):
        return {"ok": False, "module": str(module),
                "error": f"invalid 'module': {module!r} (must be one of m1, m2, m3, m4)", "log": []}

    run_id = payload.get("run_id") or f"{module}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out_dir = OUT_API_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log = []
    start = time.perf_counter()

    if module == "m1":
        resp = run_m1(payload, run_id, out_dir, log)
    elif module == "m2":
        resp = run_m2(payload, run_id, out_dir, log)
    elif module == "m3":
        resp = run_m3(payload, run_id, out_dir, log)
    else:
        resp = run_generic(module, payload, run_id, out_dir, log)

    resp.setdefault("run_id", run_id)
    resp.setdefault("module", module)
    resp["seconds"] = round(time.perf_counter() - start, 2)
    resp.setdefault("notes", [])
    return resp


def build_health() -> dict:
    return {
        "ok": True,
        "modules": ["m1", "m2", "m3", "m4"],
        "live_available": legacy.openrouter_key_present(),
        "versions": {"python": sys.version.split()[0], "git_sha": GIT_SHA},
    }


# --------------------------------------------------------------------------
# Artifacts
def find_artifact(out_dir: Path, rel_path: str):
    if not out_dir.exists():
        return None
    out_resolved = out_dir.resolve()
    for base in (out_dir, out_dir / "m1", out_dir / "m2"):
        candidate = base / rel_path
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(out_resolved)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


# mimetypes.guess_type() leans on the host's system mime.types file for some
# extensions (absent/incomplete on python:3.12-slim, this service's Docker
# base) -- these are pinned explicitly so M3's clip/caption artifacts get a
# correct Content-Type on every deployment, not just wherever this runs.
EXTRA_CONTENT_TYPES = {".mp4": "video/mp4", ".srt": "application/x-subrip", ".vtt": "text/vtt"}


def guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_CONTENT_TYPES:
        return EXTRA_CONTENT_TYPES[ext]
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


# --------------------------------------------------------------------------
# Auth
def auth_ok(handler: "Handler") -> bool:
    if MODULE_API_TOKEN:
        return handler.headers.get("Authorization", "") == f"Bearer {MODULE_API_TOKEN}"
    return DEV_MODE


# --------------------------------------------------------------------------
# HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PostEventModuleAPI/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _log_line(self, status):
        print(f"{datetime.now(timezone.utc).isoformat()} {self.command} {self.path} -> {status}", flush=True)

    def log_message(self, format, *args):  # noqa: A002 -- replaced by _log_line, one line per request
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, build_health())
            self._log_line(200)
            return
        if parsed.path.startswith("/artifacts/"):
            if not auth_ok(self):
                self._json(401, {"ok": False, "error": "unauthorized"})
                self._log_line(401)
                return
            self._serve_artifact(parsed.path[len("/artifacts/"):])
            return
        self._json(404, {"ok": False, "error": f"not found: {parsed.path}"})
        self._log_line(404)

    def _serve_artifact(self, rest: str):
        rest = unquote(rest)
        parts = rest.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            self._json(400, {"ok": False, "error": "expected /artifacts/<run_id>/<path>"})
            self._log_line(400)
            return
        run_id, rel_path = parts
        if ".." in Path(run_id).parts or ".." in Path(rel_path).parts:
            self._json(400, {"ok": False, "error": "path must not contain '..'"})
            self._log_line(400)
            return
        target = find_artifact(OUT_API_ROOT / run_id, rel_path)
        if target is None:
            self._json(404, {"ok": False, "error": f"artifact not found: {run_id}/{rel_path}"})
            self._log_line(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", guess_content_type(target))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._log_line(200)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self._json(404, {"ok": False, "error": f"not found: {parsed.path}"})
            self._log_line(404)
            return
        if not auth_ok(self):
            self._json(401, {"ok": False, "error": "unauthorized"})
            self._log_line(401)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(200, {"ok": False, "error": f"malformed JSON body: {exc}"})
            self._log_line(200)
            return
        if not isinstance(payload, dict) or "module" not in payload:
            self._json(200, {"ok": False, "error": "body must be a JSON object with at least {'module': ...}"})
            self._log_line(200)
            return
        try:
            result = handle_run_request(payload)
        except Exception as exc:  # noqa: BLE001 -- top-level handler must never 500 silently
            self._json(200, {"ok": False, "error": f"unhandled {type(exc).__name__}: {exc}",
                              "log": traceback.format_exc().splitlines()[-20:]})
            self._log_line(200)
            return
        self._json(200, result)
        self._log_line(200)


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(host) to set
        # server_name -- a reverse-DNS lookup that can hang for many seconds
        # (or the full connect timeout) binding to 0.0.0.0 in a network-
        # sandboxed/offline environment. This service never uses
        # server_name/server_port for anything, so skip straight to
        # TCPServer's plain bind instead of HTTPServer's.
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def main():
    global DEV_MODE
    parser = argparse.ArgumentParser(description="Post-event module API service (stdlib only, no request timeouts).")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--dev", action="store_true",
                         help="Skip Bearer auth when MODULE_API_TOKEN is unset (local dev only).")
    args = parser.parse_args()
    DEV_MODE = args.dev

    if not MODULE_API_TOKEN and not DEV_MODE:
        print("[warn] MODULE_API_TOKEN is unset and --dev not passed -- every /run and /artifacts request "
              "will be rejected with 401 until one of those is set", file=sys.stderr)

    OUT_API_ROOT.mkdir(parents=True, exist_ok=True)
    server = Server((args.host, args.port), Handler)
    print(f"post-event module API listening on {args.host}:{args.port} "
          f"(dev={DEV_MODE}, token_set={bool(MODULE_API_TOKEN)}, git_sha={GIT_SHA})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
