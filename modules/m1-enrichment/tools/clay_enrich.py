#!/usr/bin/env python3
"""Standalone operator tool: enrich company domains via the official `clay`
CLI (Clay's managed "Enrich Company" function). Written to call Clay for
real, against whatever workspace the CLI is signed into.

NOT RUN IN THIS BUILD. No Clay call has ever been made here -- the evidence
is out/receipts/m1-live-slice-30/quality_report.json, whose `clay` block is
all zeros with `run_urls: {}`. Every firmographic value in this package came
from the LLM or the rule cascade, never from Clay.

Nothing invokes this file: not enrich.py, not api/server.py, not n8n. It is
also NOT how Clay would reach M1. M1's only Clay door is `--clay-results
PATH`, a `{domain: {industry, employee_count, country, source, run_url}}`
map (see clay_spec.md) -- and this tool does not write that shape. It writes
a run receipt (clay_enrich_results.json: routine id, run id, workspace,
credit balances, raw response) plus a flattened clay_enrich_summary.csv;
feeding M1 from it would mean reshaping those fields into the map above.

Credit discipline: hard-capped by --max (default 3). Prints the workspace
credit balance before and after so spend is auditable. Never runs without
an explicit domain list.

Usage:
  python3 modules/m1-enrichment/tools/clay_enrich.py clay.com hubspot.com n8n.io
  python3 modules/m1-enrichment/tools/clay_enrich.py --from-csv out/<slug>/m1/hubspot_companies.csv --max 3
  add --out DIR to choose where clay_enrich_results.json lands (default out/clay-enrich/)

Stdlib only. Requires `clay` on PATH or CLAY_BIN env pointing at the launcher.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENRICH_COMPANY = "function:t_0tk7xn4ZKDmpQ6jhJzX"
DEFAULT_BIN = os.environ.get("CLAY_BIN") or "clay"
ROOT = Path(__file__).resolve().parents[3]


def clay(bin_path, *args, timeout=120):
    cmd = [bin_path, *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"clay {' '.join(args[:2])} failed (exit {p.returncode}): {p.stderr.strip()[:400]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def credits(bin_path):
    try:
        return clay(bin_path, "credits").get("balance")
    except Exception as exc:  # credits call is informational only
        print(f"[warn] credits lookup failed: {exc}", file=sys.stderr)
        return None


def enrich_domains(domains, max_n, clay_bin=DEFAULT_BIN, poll=90):
    """Runs Clay's managed "Enrich Company" routine (`clay routines runs
    start`/`get`) for up to `max_n` distinct domains -- the same logic `main()`
    below drives from the CLI. Callers (e.g. enrich.py's --clay-max lane) get
    this in-process instead of shelling out to this script.

    Returns (results, meta):
      results -- {domain: {status, name, industry, employee_count, country,
                  annual_revenue}}
      meta    -- {workspace, domains, run_id, credits_before, credits_after,
                  credits_spent, final_status, raw}

    Raises RuntimeError/OSError/etc. on any `clay` CLI failure (missing
    binary, non-zero exit, bad auth) -- callers decide the fallback, this
    function never swallows a hard failure silently. Empty `domains` (after
    max_n cap) is a no-op: returns ({}, meta-with-nulls) without touching the
    `clay` CLI at all."""
    domains = list(dict.fromkeys(d.strip().lower() for d in domains if d.strip()))[:max_n]
    if not domains:
        return {}, {
            "workspace": None, "domains": [], "run_id": None,
            "credits_before": None, "credits_after": None, "credits_spent": None,
            "final_status": None, "raw": None,
        }

    who = clay(clay_bin, "whoami")
    before = credits(clay_bin)
    print(f"workspace={who.get('workspace', {}).get('id')} credits_before={before} domains={domains}")

    body = {"items": [{"id": d, "inputs": {"Company Identifier": d}} for d in domains]}
    started = clay(clay_bin, "routines", "runs", "start", ENRICH_COMPANY, "--input", json.dumps(body))
    run_id = started["routineRunId"]
    print(f"run started: {run_id} status={started.get('status')}")

    deadline = time.time() + poll
    result = None
    while time.time() < deadline:
        result = clay(clay_bin, "routines", "runs", "get", run_id)
        status = result.get("status")
        if status not in ("in_progress", "queued", "running", "pending"):
            break
        time.sleep(5)
    after = credits(clay_bin)

    results = {}
    for item in (result or {}).get("data", []) or []:
        payload = item.get("result") or {}
        ec = payload.get("Enrich Company") if isinstance(payload, dict) else None
        ec = ec if isinstance(ec, dict) else {}
        results[item.get("id")] = {
            "status": item.get("status"),
            "name": ec.get("name"),
            "industry": ec.get("industry"),
            "employee_count": ec.get("employee_count"),
            "country": ec.get("country"),
            "annual_revenue": ec.get("annual_revenue"),
        }

    meta = {
        "workspace": who.get("workspace"),
        "domains": domains,
        "run_id": run_id,
        "credits_before": before,
        "credits_after": after,
        "credits_spent": (before - after) if (before is not None and after is not None) else None,
        "final_status": result.get("status") if result else None,
        "raw": result,
    }
    return results, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*", help="company domains to enrich")
    ap.add_argument("--from-csv", help="CSV with a 'domain' column (e.g. M1 hubspot_companies.csv)")
    ap.add_argument("--max", type=int, default=3, help="hard cap on enrichments (credit guard)")
    ap.add_argument("--out", default=str(ROOT / "out" / "clay-enrich"))
    ap.add_argument("--poll", type=int, default=90, help="seconds to wait for results")
    args = ap.parse_args()

    domains = [d.strip().lower() for d in args.domains if d.strip()]
    if args.from_csv:
        with open(args.from_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("domain") or "").strip().lower()
                if d:
                    domains.append(d)
    domains = list(dict.fromkeys(domains))[: args.max]
    if not domains:
        print("no domains given — refusing to run (credit guard)", file=sys.stderr)
        sys.exit(2)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results, meta = enrich_domains(domains, len(domains), clay_bin=DEFAULT_BIN, poll=args.poll)

    receipt = {
        "routine": ENRICH_COMPANY,
        "routineRunId": meta["run_id"],
        "workspace": meta["workspace"],
        "domains": meta["domains"],
        "credits_before": meta["credits_before"],
        "credits_after": meta["credits_after"],
        "credits_spent": meta["credits_spent"],
        "final_status": meta["final_status"],
        "raw": meta["raw"],
    }
    (out / "clay_enrich_results.json").write_text(json.dumps(receipt, indent=2))

    # Flatten the useful fields for the control room / README.
    rows = [
        {"domain": domain, **fields}
        for domain, fields in results.items()
    ]
    with open(out / "clay_enrich_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "status", "name", "industry", "employee_count", "country", "annual_revenue"])
        w.writeheader()
        w.writerows(rows)

    print(f"final_status={receipt['final_status']} credits_after={meta['credits_after']} spent={receipt['credits_spent']}")
    for r in rows:
        print(f"  {r['domain']}: {r['status']} | {r['name']} | {r['industry']} | {r['employee_count']} | {r['country']}")
    print(f"wrote {out / 'clay_enrich_results.json'} and clay_enrich_summary.csv")
    sys.exit(0 if receipt["final_status"] not in (None, "failed", "error") else 1)


if __name__ == "__main__":
    main()
