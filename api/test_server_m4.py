#!/usr/bin/env python3
"""Zero-cost verification of api/server.py's M4 surface (seed/sync/analyze/render,
GET /narrative/<run_id>, GET /dashboard/<run_id>/).

Boots the real server (subprocess, MODULE_API_TOKEN=t) on a free port and hits it
over real HTTP with urllib (stdlib), same style as test_server_m3.py. Offline only:
every /run call passes live:false, so server.py appends --offline to the dashboard
CLI and no request leaves the machine.

WHAT THIS DOES AND DOES NOT COVER: the dashboard CLI under test is a fixed fake
written by this file into a temp dir and wired in with M4_DASHBOARD_PY -- so the
file contents (counts, lane, narrative text) are known constants and the
assertions below are exact. That means this file verifies server.py's own M4
plumbing -- flag building, summary-read-from-files, artifacts/receipts maps, the
narrative/dashboard routes and their auth -- and NOT modules/m4-dashboard/
dashboard.py, which has its own tests. The fake CLI's file shapes are the ones
docs/module-api.md's M4 table specifies.

Scenarios:
  1. POST /run m4 seed/sync/analyze/render, live:false, one shared run_id --
     asserts ok:true and the exact summary keys/values docs/module-api.md's M4
     section promises, plus the artifacts/receipts maps.
  2. GET /narrative/<run_id> (no refresh) -- asserts the rules-lane narrative,
     source, model and validator_disagreements come back from analysis.json;
     then ?refresh=1&live=0 re-runs analyze offline and still answers.
  3. GET /dashboard/<run_id>/ -- asserts window.NARRATIVE_ENDPOINT is injected
     before the page's first <script>.
  4. Bad bearer token -- 401 on /run and on /narrative.
  5. PUBLIC_DASHBOARD -- /dashboard/<run>/ and /narrative/<run> with no token:
     401 against the default server, 200 against a second server booted with
     PUBLIC_DASHBOARD=1.

Run: python3 api/test_server_m4.py

NOTE: this is a plain script, not unittest.TestCase. "python3 -m unittest <this file>" runs none of it: it reports "Ran 0 tests" and "NO TESTS RAN", exiting 5 on Python 3.12+ and 0 on older interpreters. Either way nothing here is executed. Run it directly, as the Run line above shows.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t"

PASS_COUNT, FAIL_COUNT = 0, 0

# The fake dashboard CLI: same argument surface as modules/m4-dashboard/
# dashboard.py (phase positional, --out, --event, --event-tag, --offline,
# --live-dry-run, --budget), writing the files docs/module-api.md's M4 table
# names with constant contents. Zero network, zero LLM calls.
STUB_CLI = '''#!/usr/bin/env python3
"""Fixed-output dashboard CLI used by api/test_server_m4.py. Writes the M4
contract's files with constant contents; never touches the network."""
import argparse
import json
import sys
from pathlib import Path

NARRATIVE = ("Lifecycle movement over 30 days: 2 contacts advanced, 1 within the first week. "
             "Composed by the deterministic lane; no model was called.")


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Fixed-output M4 dashboard CLI (test double).")
    parser.add_argument("phase", choices=["seed", "sync", "analyze", "render", "all"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--event", default=None)
    parser.add_argument("--event-tag", default="test-event")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--live-dry-run", action="store_true")
    parser.add_argument("--budget", type=int, default=None)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lane = "offline" if args.offline else "live"
    phases = ["seed", "sync", "analyze", "render"] if args.phase == "all" else [args.phase]

    for phase in phases:
        if phase == "seed":
            write(out / "receipts" / "m4_seed.json",
                  {"event_slug": args.event_tag, "lane": lane, "method": "properties",
                   "contacts_matched": 3, "events_written": 0, "lifecycle_updates": 2, "errors": []})
        elif phase == "sync":
            write(out / "snapshot.json",
                  {"contacts": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                   "companies": [{"id": "c1"}, {"id": "c2"}],
                   "email_engagements": [{"id": "e%d" % i} for i in range(4)],
                   "events": [{"id": "ev%d" % i} for i in range(5)],
                   "method": "properties", "lane": lane})
            write(out / "receipts" / "m4_hubspot_sync.json",
                  {"event_slug": args.event_tag, "lane": lane,
                   "endpoints": ["/crm/v3/objects/contacts/search"],
                   "totals": {"contacts": 3, "companies": 2, "email_engagements": 4,
                              "events": 5, "lifecycle_history_rows": 2, "requests": 0}})
        elif phase == "analyze":
            # Rules-lane shape: `llm` empty, the narrative at the top level,
            # every count in `deterministic` -- what dashboard.py writes with
            # --offline.
            write(out / "analysis.json",
                  {"event_slug": args.event_tag, "generated_at": "2026-09-16T00:00:00+00:00",
                   "lane": "rules" if args.offline else "live", "model": None,
                   "narrative_source": "rules", "movement_narrative": NARRATIVE,
                   "deterministic": {"mql_rate": 0.4, "attendee_to_mql": 0.4,
                                     "movement": {"7d": 1, "14d": 2, "30d": 2},
                                     "top_accounts": [{"company": "One"}, {"company": "Two"}],
                                     "top_contacts": [{"email": "first@example.test"}],
                                     "committees": [{"company": "One", "contacts": 2}],
                                     "anomalies": [{"contact": "first@example.test"},
                                                   {"contact": "second@example.test"}]},
                   "llm": {}, "notes": []})
            write(out / "receipts" / "m4_llm_calls.json", {"lane": lane, "calls": [], "budget": args.budget})
        elif phase == "render":
            write(out / "dashboard_data.json",
                  {"lane": lane, "kpis": {"mql_rate": 0.4, "committees": 1, "anomalies": 2},
                   "top_accounts": [{"company": "One"}, {"company": "Two"}],
                   "narrative": {"text": NARRATIVE, "source": "rules", "model": None}})
            (out / "index.html").write_text(
                "<!doctype html><html><head><title>M4</title></head><body>"
                "<div id=\\"root\\">dashboard</div>\\n<script>var BUILT_IN = 1;</script>\\n"
                "</body></html>", encoding="utf-8")
        print("M4 %s (%s): wrote fixed test output -> %s" % (phase, lane, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def check(label, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {label}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {label}  {detail}")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, method="GET", token=None, body=None, timeout=60):
    """Returns (status, headers_dict, raw_bytes). Never raises on non-2xx --
    server.py's /run contract always answers 200 with ok:false; only auth
    failures and bad paths use real HTTP error codes."""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def wait_for_server(base_url: str, timeout=20):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            status, _, _ = http(f"{base_url}/health")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not come up within {timeout}s: {last_exc}")


def run_phase(base_url: str, run_id: str, phase: str):
    status, _, raw = http(f"{base_url}/run", method="POST", token=TOKEN,
                           body={"module": "m4", "phase": phase, "run_id": run_id, "live": False,
                                 "inputs": {"event_slug": "test-event", "options": {"budget": 3}}})
    return status, json.loads(raw)


def scenario_phases(base_url: str, run_id: str):
    print("=== scenario 1: POST /run m4 seed -> sync -> analyze -> render (live:false) ===")
    status, resp = run_phase(base_url, run_id, "seed")
    check("seed HTTP 200", status == 200, status)
    check("seed ok:true", resp.get("ok") is True, resp.get("error") or resp)
    summary = resp.get("summary", {})
    check("seed summary keys", set(summary) == {"method", "contacts_matched", "events_written",
                                                 "lifecycle_updates", "errors"}, summary)
    check("seed summary.method read from the receipt", summary.get("method") == "properties", summary)
    check("seed summary.contacts_matched == 3", summary.get("contacts_matched") == 3, summary)
    check("seed summary.lifecycle_updates == 2", summary.get("lifecycle_updates") == 2, summary)
    check("seed lane == offline", resp.get("lane") == "offline", resp.get("lane"))
    check("seed receipt listed", "/artifacts/%s/receipts/m4_seed.json" % run_id in resp.get("receipts", []),
          resp.get("receipts"))

    status, resp = run_phase(base_url, run_id, "sync")
    check("sync HTTP 200", status == 200, status)
    check("sync ok:true", resp.get("ok") is True, resp.get("error") or resp)
    summary = resp.get("summary", {})
    check("sync summary keys", set(summary) == {"contacts", "companies", "engagements", "events",
                                                 "lifecycle_changes", "method"}, summary)
    check("sync summary.contacts == 3", summary.get("contacts") == 3, summary)
    check("sync summary.companies == 2", summary.get("companies") == 2, summary)
    check("sync summary.engagements == 4", summary.get("engagements") == 4, summary)
    check("sync summary.events == 5", summary.get("events") == 5, summary)
    check("sync summary.lifecycle_changes == 2", summary.get("lifecycle_changes") == 2, summary)
    check("sync summary.method == properties", summary.get("method") == "properties", summary)
    check("sync artifacts include snapshot.json",
          resp.get("artifacts", {}).get("snapshot.json") == f"/artifacts/{run_id}/snapshot.json",
          resp.get("artifacts"))

    status, resp = run_phase(base_url, run_id, "analyze")
    check("analyze HTTP 200", status == 200, status)
    check("analyze ok:true", resp.get("ok") is True, resp.get("error") or resp)
    summary = resp.get("summary", {})
    check("analyze summary keys", set(summary) == {"anomalies", "scored", "committees", "model", "lane"}, summary)
    check("analyze summary.anomalies == 2 (deterministic rows, llm block empty)",
          summary.get("anomalies") == 2, summary)
    check("analyze summary.committees == 1", summary.get("committees") == 1, summary)
    check("analyze summary.scored is null in the rules lane (no interest_scores in the file)",
          summary.get("scored") is None, summary)
    check("analyze notes say why scored is null",
          any("scored" in note for note in resp.get("notes", [])), resp.get("notes"))
    check("analyze summary.lane == rules (read from analysis.json, not the request)",
          summary.get("lane") == "rules", summary)
    check("analyze summary.model is null in the rules lane", summary.get("model") is None, summary)
    check("analyze receipt listed",
          f"/artifacts/{run_id}/receipts/m4_llm_calls.json" in resp.get("receipts", []), resp.get("receipts"))

    status, resp = run_phase(base_url, run_id, "render")
    check("render HTTP 200", status == 200, status)
    check("render ok:true", resp.get("ok") is True, resp.get("error") or resp)
    summary = resp.get("summary", {})
    check("render summary keys", set(summary) == {"mql_rate", "top_accounts", "narrative_source"}, summary)
    check("render summary.mql_rate == 0.4", summary.get("mql_rate") == 0.4, summary)
    check("render summary.top_accounts == 2", summary.get("top_accounts") == 2, summary)
    check("render summary.narrative_source == rules", summary.get("narrative_source") == "rules", summary)
    check("render artifacts include index.html",
          resp.get("artifacts", {}).get("index.html") == f"/artifacts/{run_id}/index.html",
          resp.get("artifacts"))
    check("render next.dashboard_url points at this run",
          resp.get("next", {}).get("dashboard_url") == f"/dashboard/{run_id}/", resp.get("next"))

    status, _, raw = http(f"{base_url}/artifacts/{run_id}/snapshot.json", token=TOKEN)
    check("GET /artifacts/<run>/snapshot.json resolves inside m4/", status == 200, status)
    check("artifact body is the snapshot the CLI wrote",
          json.loads(raw).get("method") == "properties", raw[:120])

    status, resp = run_phase(base_url, run_id, "bogus")
    check("invalid phase -> ok:false", resp.get("ok") is False, resp)
    check("invalid phase names the valid ones", "seed" in (resp.get("error") or ""), resp.get("error"))


def scenario_narrative(base_url: str, run_id: str):
    print("\n=== scenario 2: GET /narrative/<run_id> (no refresh, then ?refresh=1&live=0) ===")
    status, _, raw = http(f"{base_url}/narrative/{run_id}", token=TOKEN)
    resp = json.loads(raw)
    check("HTTP 200", status == 200, status)
    check("ok:true", resp.get("ok") is True, resp)
    for key in ("narrative", "source", "generated_at", "model", "validator_disagreements"):
        check(f"response has {key!r}", key in resp, resp)
    check("narrative is the rules-lane text from analysis.json",
          isinstance(resp.get("narrative"), str) and "Lifecycle movement" in resp["narrative"],
          resp.get("narrative"))
    check("source == 'rules'", resp.get("source") == "rules", resp.get("source"))
    check("model is null in the rules lane", resp.get("model") is None, resp.get("model"))
    check("validator_disagreements == []", resp.get("validator_disagreements") == [], resp)
    check("refreshed:false without ?refresh=1", resp.get("refreshed") is False, resp)

    status, _, raw = http(f"{base_url}/narrative/{run_id}?refresh=1&live=0", token=TOKEN)
    resp = json.loads(raw)
    check("refresh=1&live=0 HTTP 200", status == 200, status)
    check("refresh=1 ok:true", resp.get("ok") is True, resp)
    check("refresh=1 reports refreshed:true", resp.get("refreshed") is True, resp)
    check("refresh=1 still the rules-lane narrative",
          isinstance(resp.get("narrative"), str) and "Lifecycle movement" in resp["narrative"],
          resp.get("narrative"))

    status, _, raw = http(f"{base_url}/narrative/m4-no-such-run", token=TOKEN)
    check("unknown run_id -> 404", status == 404, status)
    check("404 body carries ok:false", json.loads(raw).get("ok") is False, raw[:120])


def scenario_dashboard(base_url: str, run_id: str):
    print("\n=== scenario 3: GET /dashboard/<run_id>/ injects window.NARRATIVE_ENDPOINT ===")
    status, headers, raw = http(f"{base_url}/dashboard/{run_id}/", token=TOKEN)
    html = raw.decode("utf-8")
    check("HTTP 200", status == 200, status)
    check("Content-Type: text/html; charset=utf-8",
          headers.get("Content-Type") == "text/html; charset=utf-8", headers.get("Content-Type"))
    expected = f'window.NARRATIVE_ENDPOINT = "/narrative/{run_id}"'
    check("NARRATIVE_ENDPOINT injected with this run's path", expected in html, html[:200])
    check("injected before the page's first <script>",
          html.index(expected) < html.index("var BUILT_IN"), html[:400])
    check("page's own body survived", 'id="root"' in html, html[:200])

    status, _, _ = http(f"{base_url}/dashboard/{run_id}", token=TOKEN)
    check("no-trailing-slash form also serves it", status == 200, status)

    status, raw_headers, raw = http(f"{base_url}/dashboard/m4-no-such-run/", token=TOKEN)
    check("unknown run_id -> 404", status == 404, status)


def scenario_bad_token(base_url: str, run_id: str):
    print("\n=== scenario 4: wrong bearer token -> 401 ===")
    status, _, raw = http(f"{base_url}/run", method="POST", token="wrong-token",
                           body={"module": "m4", "phase": "sync", "inputs": {}})
    check("POST /run 401", status == 401, status)
    check("ok:false on 401", json.loads(raw).get("ok") is False, raw[:120])
    status, _, _ = http(f"{base_url}/narrative/{run_id}", token="wrong-token")
    check("GET /narrative 401", status == 401, status)


def scenario_public_dashboard_off(base_url: str, run_id: str):
    print("\n=== scenario 5a: PUBLIC_DASHBOARD unset -> unauthenticated reads are 401 ===")
    status, _, _ = http(f"{base_url}/dashboard/{run_id}/")
    check("GET /dashboard/<run>/ without a token -> 401", status == 401, status)
    status, _, _ = http(f"{base_url}/narrative/{run_id}")
    check("GET /narrative/<run> without a token -> 401", status == 401, status)


def scenario_public_dashboard_on(run_id: str):
    print("\n=== scenario 5b: PUBLIC_DASHBOARD=1 -> the same two reads are 200 ===")
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["MODULE_API_TOKEN"] = TOKEN
    env["PUBLIC_DASHBOARD"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "api" / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_server(base_url)
        status, _, raw = http(f"{base_url}/dashboard/{run_id}/")
        check("GET /dashboard/<run>/ without a token -> 200", status == 200, status)
        check("still injects NARRATIVE_ENDPOINT",
              f'window.NARRATIVE_ENDPOINT = "/narrative/{run_id}"' in raw.decode("utf-8"), raw[:200])
        status, _, raw = http(f"{base_url}/narrative/{run_id}")
        check("GET /narrative/<run> without a token -> 200", status == 200, status)
        check("narrative body still ok:true", json.loads(raw).get("ok") is True, raw[:200])
        status, _, _ = http(f"{base_url}/run", method="POST",
                             body={"module": "m4", "phase": "sync", "inputs": {}})
        check("POST /run still 401 without a token (PUBLIC_DASHBOARD opens reads only)", status == 401, status)
        status, _, _ = http(f"{base_url}/artifacts/{run_id}/snapshot.json")
        check("GET /artifacts still 401 without a token", status == 401, status)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if FAIL_COUNT and proc.stdout:
            print("\n--- public-dashboard server stdout/stderr ---")
            print(proc.stdout.read())


def main():
    stub_dir = Path(tempfile.mkdtemp(prefix="m4-dashboard-stub-"))
    stub_path = stub_dir / "dashboard.py"
    stub_path.write_text(STUB_CLI, encoding="utf-8")
    print(f"dashboard CLI under test: {stub_path} (fixed-output test double, "
          f"not modules/m4-dashboard/dashboard.py)")

    run_id = f"m4-servertest-{os.getpid()}"
    out_dir = REPO_ROOT / "out" / "api" / run_id
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["MODULE_API_TOKEN"] = TOKEN
    env["M4_DASHBOARD_PY"] = str(stub_path)
    env.pop("PUBLIC_DASHBOARD", None)
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "api" / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_server(base_url)
        scenario_phases(base_url, run_id)
        scenario_narrative(base_url, run_id)
        scenario_dashboard(base_url, run_id)
        scenario_bad_token(base_url, run_id)
        scenario_public_dashboard_off(base_url, run_id)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        server_output = proc.stdout.read() if proc.stdout else ""

    # 5b boots its own server (PUBLIC_DASHBOARD is read from the environment at
    # request time, so it takes a second process to prove both halves) -- run it
    # after the first server is down but before the run dir is removed.
    try:
        scenario_public_dashboard_on(run_id)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(stub_dir, ignore_errors=True)

    if FAIL_COUNT and server_output:
        print("\n--- main server stdout/stderr (for debugging the failures above) ---")
        print(server_output)

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
