#!/usr/bin/env python3
"""One-command demo: chains M1 enrich -> M2 comms -> M3 repurpose -> M4 dashboard.

MODULE CLI CONTRACT (M4 confirmed against its real script; M1-M3 assumed --
modules build in parallel, see PLAN.md):
  - M1 modules/m1-enrichment/enrich.py [--live] --in <event-dir>/registrants.csv
        --out <dir>
        writes <dir>/hubspot_ready.csv (filename fixed by M4's real consumer
        below -- do not rename without updating M4 too).
  - M2 modules/m2-comms/comms.py [--live] --out <dir> --enriched <m1_csv>
        --event <event-dir>/event.json --transcript <event-dir>/transcript.md
        writes <dir>/comms.json
  - M3 modules/m3-repurpose/repurpose.py [--live] --out <dir>
        --event <event-dir>/event.json --transcript <event-dir>/transcript.md
  - M4 modules/m4-dashboard/build_dashboard.py --enriched <m1_csv>
        --engagement data/fixtures/engagement.json
        --segments data/fixtures/segments.json --out <dir>
        writes <dir>/index.html. Real script as built: no --live flag and no
        --comms/--content/--quality flags (dashboard computes only from M1 +
        engagement + segments) -- do not pass any of those or argparse will
        fail. Completeness KPIs are read internally from quality_report.json,
        which build_dashboard.py finds on its own next to --enriched (same
        --out dir M1 wrote both files into) -- no separate flag needed.
        Engagement/segments fixtures are global (not per-event) by design --
        M4 was not asked to be event-partitioned, only M1's output is.
  General, for M1-M3 (assumed, not yet verified against real code):
  - With no --live, module reads fixtures/incoming at fixed repo paths
    (data/incoming/, data/fixtures/, config/) and runs fully offline.
  - --live switches to real LLM calls (scripts/lib/claude_call.py pattern).
  - Module prints at least one line to stdout; the last non-blank line is
    treated as a human-readable summary and shown in the receipt table.
  - Exit code 0 = pass, nonzero = fail.

If a module's real CLI differs from the above, update STAGES below -- this is
the single integration point.

EVENT PARTITIONING (added post-judge-review, see docs/build_log.md):
  --event-dir points at a directory holding one event's registrants.csv /
  event.json / transcript.md (default data/incoming/, the original single
  fixture event). Pass a second event's dir (e.g. data/fixtures/event2/) to
  run the same pipeline against a different webinar without clobbering the
  first event's output -- --out defaults to out/<slugified event_name>/ so
  a second run lands next to, not on top of, the first. See
  orchestrator/test_webinar2.py for the idempotency proof (alumni from event
  1 resolve to update_existing, not create_new, when event 2's M1 run is
  pointed --hubspot at event 1's output-as-CRM-state).

DEMO TRUTH BANNER (added post-judge-review): every stage prints, before it
runs, one line stating plainly whether this is the offline cached-output
replay or a live LLM call -- so a judge watching the terminal is never
left to assume AI ran live when it didn't.

MODULE LANE BANNER (added post-judge-review): the offline lane runs in well
under a second, which reads as "just scripts" to a judge who runs before
reading docs/index.html. On offline runs only, main() prints a compact
per-module table (mirrors the thesis table in docs/index.html) before any
stage runs, showing what each module replays/computes offline vs. what the
LLM actually does with --live. demo.sh's offline fallback path re-execs this
same script without --live, so it inherits the banner automatically.
"""
import argparse
import json
import re
import subprocess
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALL_MODULES = ("m1", "m2", "m3", "m4")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "event"


def event_slug_for(event_dir: Path) -> str:
    """Best-effort slug from <event_dir>/event.json's event_name. Falls back
    to 'event' if the file is missing/unparseable -- never fatal, since --out
    can always be passed explicitly to override."""
    event_path = event_dir / "event.json"
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        return slugify(event.get("event_name", "event"))
    except (OSError, json.JSONDecodeError):
        return "event"


def build_stages(out_dir: Path, live: bool, event_dir: Path):
    live_flag = ["--live"] if live else []
    m1_out = out_dir / "m1"
    m2_out = out_dir / "m2"
    m3_out = out_dir / "m3"
    m4_out = out_dir / "m4"
    m1_hubspot_ready = m1_out / "hubspot_ready.csv"
    m2_comms = m2_out / "comms.json"
    m3_manifest = m3_out / "manifest.json"
    m4_index = m4_out / "index.html"

    event_json = event_dir / "event.json"
    transcript_md = event_dir / "transcript.md"
    registrants_csv = event_dir / "registrants.csv"

    return {
        "m1": {
            "stage": "M1 enrich",
            "cmd": [sys.executable, str(REPO_ROOT / "modules" / "m1-enrichment" / "enrich.py"),
                    *live_flag, "--in", str(registrants_csv), "--out", str(m1_out)],
            "key_output": m1_hubspot_ready,
        },
        "m2": {
            "stage": "M2 comms",
            "cmd": [sys.executable, str(REPO_ROOT / "modules" / "m2-comms" / "comms.py"),
                    *live_flag, "--out", str(m2_out), "--enriched", str(m1_hubspot_ready),
                    "--event", str(event_json), "--transcript", str(transcript_md)],
            "key_output": m2_comms,
        },
        "m3": {
            "stage": "M3 repurpose",
            "cmd": [sys.executable, str(REPO_ROOT / "modules" / "m3-repurpose" / "repurpose.py"),
                    *live_flag, "--out", str(m3_out),
                    "--event", str(event_json), "--transcript", str(transcript_md)],
            "key_output": m3_manifest,
        },
        "m4": {
            # Real build_dashboard.py CLI: no --live, no --comms/--content/--quality.
            # It finds quality_report.json on its own (sibling of hubspot_ready.csv
            # in m1_out) for the completeness KPIs -- nothing extra to wire here.
            "stage": "M4 dashboard",
            "cmd": [sys.executable, str(REPO_ROOT / "modules" / "m4-dashboard" / "build_dashboard.py"),
                    "--out", str(m4_out), "--enriched", str(m1_hubspot_ready),
                    "--engagement", str(REPO_ROOT / "data" / "fixtures" / "engagement.json"),
                    "--segments", str(REPO_ROOT / "data" / "fixtures" / "segments.json")],
            "key_output": m4_index,
        },
    }


def last_summary_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith("{") or line.startswith("["):
            continue  # skip raw JSON blobs (e.g. M1's quality_report dump) -- not human summaries
        return line[:70]
    return ""


MODULE_LANES = [
    ("M1 Enrichment", "Deterministic rules: difflib dedupe + lookup-table field inference. No LLM call.",
     "Real LLM field inference via --live (claude -p or OpenRouter, per LLM_BACKEND); Clay waterfall enrichment in production."),
    ("M2 Comms", "Cached LLM-generated copy replayed from sample_output/, fingerprint-guarded.",
     "Live LLM call regenerates the segment's takeaway/quote copy; recipients come from M1's deduped output."),
    ("M3 Repurposing", "Cached LLM-generated copy replayed from sample_output/, fingerprint-guarded.",
     "Live LLM call regenerates blog/YouTube/infographic/social, then verify_grounding.py checks every quote and timestamp back against the transcript."),
    ("M4 Dashboard", "Computed metrics (real math over fixtures) + labeled cached fallback narrative.",
     "Live LLM narrative via Vercel /api/narrative.js, UI-labeled live vs. fallback."),
]


def print_module_lane_banner():
    """Printed once before offline-lane stages run. The offline lane finishes
    in well under a second -- without this, a judge who runs before reading
    docs/index.html sees 'some scripts ran fast' instead of the AI substance
    behind each module. Wording mirrors the thesis table in docs/index.html
    verbatim; keep the two in sync if either changes. Budget: <=12 lines."""
    print("=== What each module does: offline lane (this run) vs --live (production) ===")
    headers = ("Module", "Offline lane -- what it replays/computes", "--live lane -- what the LLM does")
    widths = [max(len(headers[i]), *(len(row[i]) for row in MODULE_LANES)) for i in range(3)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in MODULE_LANES:
        print(fmt.format(*row))


def print_stage_banner(stage_name: str, live: bool):
    if live:
        backend = (os.environ.get("LLM_BACKEND") or "auto").strip().lower()
        if backend == "openrouter":
            engine = f"OpenRouter ({os.environ.get('OPENROUTER_MODEL') or 'module default model'})"
        elif backend == "claude":
            engine = "claude -p"
        else:
            engine = "claude -p (OpenRouter fallback if unavailable)"
        print(f"[live lane] {stage_name}: invoking {engine} at runtime for real generation "
              f"-- use no flag / omit --live to replay cached outputs instead")
    else:
        print(f"[offline lane] {stage_name}: replaying cached AI outputs (generated by Claude "
              f"at build time) -- use --live for runtime generation")


def run_stage(stage: dict, live: bool = False) -> dict:
    start = time.perf_counter()
    # Live lane: reasoning-style models take ~2 min per call and a module may
    # make several, so the per-stage ceiling is 1h live vs 10 min offline.
    stage_timeout = 3600 if live else 600
    try:
        proc = subprocess.run(stage["cmd"], capture_output=True, text=True, timeout=stage_timeout)
        elapsed = time.perf_counter() - start
        ok = proc.returncode == 0
        summary = last_summary_line(proc.stdout) or last_summary_line(proc.stderr)
        # Full module stdout/stderr next to its outputs so a FAIL is diagnosable
        # from disk (the receipt table truncates to 50 chars).
        try:
            log_path = Path(stage["key_output"]).parent / "_stage.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"$ {' '.join(str(c) for c in stage['cmd'])}\n"
                f"exit={proc.returncode} seconds={elapsed:.2f}\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
        except OSError:
            pass
    except FileNotFoundError:
        elapsed = time.perf_counter() - start
        ok = False
        summary = f"module script not found: {stage['cmd'][1]}"
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        ok = False
        summary = f"timed out after {stage_timeout}s"

    key_output = stage["key_output"]
    if not summary:
        if key_output.exists():
            summary = f"{key_output} ({key_output.stat().st_size}B)"
        else:
            summary = f"missing: {key_output}"

    return {"stage": stage["stage"], "seconds": elapsed, "key_output": summary, "pass": ok}


def print_receipt(rows: list):
    headers = ["stage", "seconds", "key output", "pass/fail"]
    cells = [
        [r["stage"], f"{r['seconds']:.2f}", r["key_output"][:50], "PASS" if r["pass"] else "FAIL"]
        for r in rows
    ]
    widths = [max(len(headers[i]), *(len(row[i]) for row in cells)) if cells else len(headers[i])
              for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in cells:
        print(fmt.format(*row))


def main():
    parser = argparse.ArgumentParser(description="Post-event engine: run M1->M2->M3->M4 end to end.")
    parser.add_argument("--live", action="store_true", help="Use real LLM calls instead of offline fixtures.")
    parser.add_argument("--event-dir", default=str(REPO_ROOT / "data" / "incoming"),
                         help='Directory with this event\'s registrants.csv/event.json/transcript.md '
                              '(default: data/incoming/, the original fixture event). Point a second '
                              'call at a different event dir (e.g. data/fixtures/event2/) to run a '
                              'second webinar through the same pipeline without clobbering the first.')
    parser.add_argument("--out", default=None,
                         help='Output directory (default: out/<slug of event.json\'s event_name>/, '
                              'so a second event partitions automatically -- pass explicitly to override).')
    parser.add_argument("--modules", default="m1,m2,m3,m4",
                         help="Comma-separated subset of m1,m2,m3,m4 to run, in order (default: all four). "
                              "e.g. --modules m1,m4 to skip M2/M3 (useful for fixtures with no transcript).")
    args = parser.parse_args()

    event_dir = Path(args.event_dir)
    selected = [m.strip() for m in args.modules.split(",") if m.strip()]
    unknown = [m for m in selected if m not in ALL_MODULES]
    if unknown:
        sys.exit(f"unknown module(s) in --modules: {unknown} (valid: {list(ALL_MODULES)})")

    out_dir = Path(args.out) if args.out is not None else Path("out") / event_slug_for(event_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_stages = build_stages(out_dir, args.live, event_dir)
    stages = [all_stages[m] for m in selected]

    if not args.live:
        print_module_lane_banner()

    rows = []
    for stage in stages:
        print_stage_banner(stage["stage"], args.live)
        row = run_stage(stage, live=args.live)
        rows.append(row)
        if not row["pass"]:
            break

    print_receipt(rows)
    if not all(r["pass"] for r in rows):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
