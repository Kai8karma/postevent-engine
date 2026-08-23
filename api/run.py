#!/usr/bin/env python3
"""Vercel Python serverless function: POST/GET /api/run.

Runs the real M1-M4 module scripts (modules/m1-enrichment/enrich.py,
modules/m2-comms/comms.py, modules/m3-repurpose/repurpose.py,
modules/m4-dashboard/build_dashboard.py) as subprocesses against either the
caller's own registrant CSV / transcript or the bundled fixture, and returns
their real output files as artifacts. No module logic is reimplemented here.

See api/vercel-api-notes.md for the deploy contract (routes, maxDuration,
which repo directories must ship next to this file) and the exact request/
response JSON shape this implements.

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
from http.server import BaseHTTPRequestHandler
from pathlib import Path

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
    "current OpenRouter load -- or see the pre-computed full live run "
    "committed at out/live-proof/."
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
# Input materialisation
def materialize_event(tmp_dir: Path, registrants_csv, transcript_md, event_name):
    """Writes this request's event inputs into tmp_dir, using the caller's
    values where supplied and the bundled fixture (data/incoming/) otherwise.
    Returns (registrants_path, event_path, transcript_path, custom)."""
    incoming = REPO_ROOT / "data" / "incoming"
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

    return reg_path, ev_path, tr_path, custom


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
    llm_batches = 0
    llm_rows_patched = 0
    live_report_path = out_dir / "live_inference_report.json"
    if live_report_path.exists():
        lr = json.loads(live_report_path.read_text(encoding="utf-8"))
        llm_batches = lr.get("inference_batches", 0) + lr.get("icp_batches", 0)
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
        "llm_batches": llm_batches, "llm_rows_patched": llm_rows_patched,
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
    }


def m2_artifact_specs(out_dir: Path):
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
    """M4's numbers are parsed from build_dashboard.py's own summary print
    lines (not recomputed here) -- the KPI data itself only exists embedded
    in index.html's JSON blob, and the script's stdout is the script's own
    authoritative restatement of it."""
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
        # build_dashboard.py has no --live path; the dashboard's live
        # narrative call (modules/m4-dashboard/api/narrative.js) happens
        # client-side, in the reviewer's browser, after index.html loads --
        # this endpoint only ever runs the offline Python builder, which
        # always embeds the cached fallback narrative into the HTML.
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
            "offline, or see the pre-computed full live run at out/live-proof/m1/",
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
            "or see the pre-computed full live run at out/live-proof/m2/", log,
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
            "or see the pre-computed full live run at out/live-proof/m3/", log,
        )
    if not result["ok"]:
        raise ModuleFailure("m3", f"m3 exited {result['returncode']}", "check the log for the module's own error message", log)
    return result


def stage_m4(out_dir: Path, m1_ready_csv: Path, log: list):
    cmd = [
        PY, str(REPO_ROOT / "modules" / "m4-dashboard" / "build_dashboard.py"),
        "--out", str(out_dir),
        "--enriched", str(m1_ready_csv),
        "--engagement", str(REPO_ROOT / "data" / "fixtures" / "engagement.json"),
        "--segments", str(REPO_ROOT / "data" / "fixtures" / "segments.json"),
    ]
    timeout_s = OFFLINE_TIMEOUT_S
    log.append(f"[m4] offline (no --live flag exists on build_dashboard.py) -- invoking build_dashboard.py (timeout {timeout_s}s)")
    result = run_module(cmd, build_env(False), timeout_s)
    log.extend(lines_for_log(result["stdout"], result["stderr"]))
    if result["timed_out"]:
        raise ModuleFailure("m4", f"m4 timed out after {result['elapsed']:.1f}s (limit {timeout_s}s)", "unexpected -- m4 is pure computation, no LLM call; check for a script hang", log)
    if not result["ok"]:
        raise ModuleFailure("m4", f"m4 exited {result['returncode']}", "check the log for the module's own error message", log)
    return result


class ModuleFailure(Exception):
    def __init__(self, module, error, hint, log):
        super().__init__(error)
        self.module = module
        self.error = error
        self.hint = hint
        self.log = log


# --------------------------------------------------------------------------
# Top-level request handling
def handle_run(payload: dict) -> dict:
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

    key_present = openrouter_key_present()
    notes = []
    if live_requested and not key_present:
        notes.append("live:true was requested but no OPENROUTER_API_KEY is present on this deployment "
                      "-- ran the deterministic offline lane and said so.")
        live_requested = False

    tmp_dir = Path(tempfile.mkdtemp(prefix="postevent-run-", dir=tempfile.gettempdir()))
    try:
        reg_path, ev_path, tr_path, custom_inputs = materialize_event(tmp_dir, registrants_csv, transcript_md, event_name)
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
                m4_result = stage_m4(m4_out, m1_out / "hubspot_ready.csv", log)
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
        try:
            if module == "m1":
                stage_m1(out_dir, reg_path, live_requested, log)
                summary = summarize_m1(out_dir)
                artifacts = collect_artifacts(out_dir, M1_ARTIFACT_SPECS)
            elif module == "m2":
                stage_m2(out_dir, None, ev_path, tr_path, live_requested, log)
                summary = summarize_m2(out_dir)
                artifacts = collect_artifacts(out_dir, m2_artifact_specs(out_dir))
                if custom_inputs and not live_requested:
                    notes.append("offline lane replays cached sample copy fingerprinted to the bundled fixture "
                                  "-- it does not regenerate from your transcript. Pass live:true to see real "
                                  "generation from your data.")
            elif module == "m3":
                stage_m3(out_dir, ev_path, tr_path, live_requested, log)
                summary = summarize_m3(out_dir)
                artifacts = collect_artifacts(out_dir, M3_ARTIFACT_SPECS)
                if custom_inputs and not live_requested:
                    notes.append("offline lane replays cached sample copy fingerprinted to the bundled fixture "
                                  "-- it does not regenerate from your transcript. Pass live:true to see real "
                                  "generation from your data.")
            elif module == "m4":
                # M4 has no --live flag (pure computation over M1 output + fixtures);
                # it needs an M1 run first, so run M1 offline here to feed it.
                m1_out = out_root / "m1"
                stage_m1(m1_out, reg_path, False, log)
                result = stage_m4(out_dir, m1_out / "hubspot_ready.csv", log)
                summary = summarize_m4(result["stdout"])
                artifacts = collect_artifacts(out_dir, M4_ARTIFACT_SPECS)
                if live_requested:
                    notes.append("build_dashboard.py has no --live flag -- it is pure computation over M1's "
                                  "output plus engagement/segments fixtures. Ran M1 offline to feed it (M1 "
                                  "itself has no LLM dependency for the fields M4 reads). The dashboard's live "
                                  "narrative call is client-side (modules/m4-dashboard/api/narrative.js), not "
                                  "invoked by this endpoint.")
                    model = None
        except ModuleFailure as fail:
            return {"ok": False, "module": fail.module, "error": fail.error, "hint": fail.hint, "log": fail.log}

        return {"ok": True, "module": module, "lane": "live" if live_requested else "offline",
                "seconds": round(time.perf_counter() - start, 2), "model": model,
                "summary": summary, "artifacts": artifacts, "log": log, "notes": notes}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_get_response() -> dict:
    return {
        "ok": True,
        "service": "post-event engine module runner",
        "live_available": openrouter_key_present() if REPO_ROOT is not None else False,
        "model_chain": FAST_MODEL_CHAIN.split(","),
        "modules": ["m1", "m2", "m3", "m4", "chain"],
    }


# --------------------------------------------------------------------------
# HTTP handler (Vercel Python runtime contract: module-level `handler`)
class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send_json(200, build_get_response())

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
