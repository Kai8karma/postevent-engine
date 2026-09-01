#!/usr/bin/env python3
"""Assemble the static Vercel site for the judge control room.

Layout produced under out/vercel-stage/postevent-engine/ (deploy that dir):
  /                      web/index.html -- the console app a reviewer drives
  /control-room/         docs/index.html (the written build report) + docs/*.md
  /modules/<m>/README.md module READMEs the control room links to
  /dashboard/            M4 dashboard (live-proof build if present, else sample-run)
  /api/narrative.js      M4 narrative serverless function (+ vercel.json)
  /demo/sample-run/      every M1-M4 output of the offline lane, browsable
  /demo/live-proof/      every output of the live LLM lane (receipt), browsable
  /demo/clay-live-proof/ Clay live enrichment receipt
Each demo directory gets a generated index.html so a judge can click through
without a directory-listing server. Stdlib only.

Usage: python3 orchestrator/stage_vercel.py   (then: cd out/vercel-stage/postevent-engine && vercel deploy --prod)
"""
import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "out" / "vercel-stage" / "postevent-engine"
DEMO_DIRS = ["sample-run", "live-proof", "clay-live-proof"]
SKIP_NAMES = {".DS_Store", "__pycache__"}
TEXT_EXT = {".md", ".csv", ".txt", ".log", ".json", ".yaml", ".yml", ".sh"}


def copy_tree(src: Path, dst: Path):
    for p in src.rglob("*"):
        if any(part in SKIP_NAMES for part in p.parts):
            continue
        rel = p.relative_to(src)
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
        else:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)


def write_listing(d: Path, title_root: Path):
    """index.html for d listing subdirs + files (skips dirs that ship their own index.html, e.g. m4/)."""
    if (d / "index.html").exists():
        return
    rel = d.relative_to(title_root)
    rows = []
    for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name)):
        if child.name in SKIP_NAMES or child.name == "index.html":
            continue
        name = child.name + ("/" if child.is_dir() else "")
        size = "" if child.is_dir() else f"{child.stat().st_size:,} B"
        rows.append(f'<tr><td><a href="{html.escape(name)}">{html.escape(name)}</a></td><td>{size}</td></tr>')
    up = '<a href="../">../</a>' if rel.parts else '<a href="/">control room</a>'
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(str(rel) or 'demo')} — Post-Event Engine</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{{font:15px/1.5 -apple-system,Segoe UI,Inter,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#171b26;background:#f5f6f9}}
h1{{font-size:18px;margin:0 0 6px}}p{{color:#5b6472;margin:0 0 18px}}table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #dde1e8}}
td{{padding:8px 12px;border-top:1px solid #eef0f5}}td:last-child{{text-align:right;color:#5b6472;font-variant-numeric:tabular-nums}}a{{color:#2f5fd8;text-decoration:none}}a:hover{{text-decoration:underline}}
</style></head><body><h1>Post-Event Engine · {html.escape(str(rel) or 'demo')}</h1><p>{up} · generated outputs, served as-is (no server-side logic)</p>
<table>{''.join(rows)}</table></body></html>"""
    (d / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    docs = ROOT / "docs"
    console = ROOT / "web" / "index.html"
    if not console.exists():
        print("error: web/index.html (the console app) is missing", file=sys.stderr)
        return 1
    shutil.copy2(console, STAGE / "index.html")          # root = the app
    control = STAGE / "control-room"
    control.mkdir()
    shutil.copy2(docs / "index.html", control / "index.html")  # the written report
    for md in docs.glob("*.md"):
        if md.name == "submission_email.md":  # recruiter-addressed draft, not a judge page
            continue
        shutil.copy2(md, control / md.name)
        shutil.copy2(md, STAGE / md.name)    # keep the old flat paths alive; links already shipped
    for m in ("m1-enrichment", "m2-comms", "m3-repurpose", "m4-dashboard"):
        mdir = STAGE / "modules" / m
        mdir.mkdir(parents=True, exist_ok=True)
        for doc in (ROOT / "modules" / m).glob("*.md"):
            shutil.copy2(doc, mdir / doc.name)
    # M4 hosted dashboard + narrative function
    live_m4 = ROOT / "out" / "live-proof" / "m4" / "index.html"
    sample_m4 = ROOT / "out" / "sample-run" / "m4" / "index.html"
    dash_src = live_m4 if live_m4.exists() else sample_m4
    if not dash_src.exists():
        print("error: no built M4 dashboard (run orchestrator/run_pipeline.py first)", file=sys.stderr)
        return 1
    (STAGE / "dashboard").mkdir()
    shutil.copy2(dash_src, STAGE / "dashboard" / "index.html")
    (STAGE / "api").mkdir()
    shutil.copy2(ROOT / "modules" / "m4-dashboard" / "api" / "narrative.js", STAGE / "api" / "narrative.js")
    # The Python module runner, plus everything it shells out to. api/run.py
    # resolves the repo root by walking up for modules/m1-enrichment/enrich.py,
    # so these three trees must sit beside it in the deployment.
    shutil.copy2(ROOT / "api" / "run.py", STAGE / "api" / "run.py")
    if (ROOT / "requirements.txt").exists():
        shutil.copy2(ROOT / "requirements.txt", STAGE / "requirements.txt")
    for tree in ("modules", "config", "data", "shared"):
        src = ROOT / tree
        if src.exists():
            copy_tree(src, STAGE / tree)
    vercel_cfg = json.loads((ROOT / "modules" / "m4-dashboard" / "vercel.json").read_text())
    vercel_cfg.setdefault("functions", {})["api/run.py"] = {"maxDuration": 60}
    vercel_cfg["headers"] = [{
        "source": "/(.*)\\.(md|csv|txt|log|sh|yaml|yml)",
        "headers": [{"key": "Content-Type", "value": "text/plain; charset=utf-8"}],
    }]
    (STAGE / "vercel.json").write_text(json.dumps(vercel_cfg, indent=2))
    # n8n workflow JSONs, served so n8n's "Import from URL" can pull them directly
    n8n_dst = STAGE / "n8n"
    n8n_dst.mkdir(exist_ok=True)
    for lane, src in (("cloud-master.json", ROOT / "orchestrator" / "n8n" / "cloud" / "master.json"),
                      ("local-demo-master.json", ROOT / "orchestrator" / "n8n" / "local-demo" / "master.json")):
        if src.exists():
            shutil.copy2(src, n8n_dst / lane)

    # Browsable module outputs
    staged = []
    for name in DEMO_DIRS:
        src = ROOT / "out" / name
        if not src.exists():
            continue
        dst = STAGE / "demo" / name
        copy_tree(src, dst)
        staged.append(name)
    demo_root = STAGE / "demo"
    if demo_root.exists():
        for d in [demo_root, *sorted(p for p in demo_root.rglob("*") if p.is_dir())]:
            write_listing(d, demo_root)
    n_files = sum(1 for p in STAGE.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file())
    print(f"staged {STAGE.relative_to(ROOT)}: {n_files} files, {size/1024:.0f} KB; dashboard from {dash_src.relative_to(ROOT)}; demo dirs: {staged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
