#!/usr/bin/env python3
"""Zero-cost, zero-network verification of log_dispatch.py's --dry-run path.

Monkeypatches log_dispatch.http_call to raise if it's ever called, then
feeds a fake dispatch_plan.json + dispatch_results.json through
build_receipt(dry_run=True) -- proves:
  1. status=="sent" + non-empty message_id -> lands in "logged" (not skipped).
  2. status=="failed" -> lands in "skipped", never "logged".
  3. status=="sent" with no message_id -> lands in "skipped", never "logged".
  4. zero network calls happen in dry-run (the monkeypatch would raise).

A second pass runs the real CLI end-to-end (subprocess, --dry-run, no
HUBSPOT_TOKEN in env) to prove the argparse/file-I/O wiring, not just the
importable function.

Run: python3 test_log_dispatch.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path("/Users/kiran/Desktop/Claude GOD/career/postevent-engine")
sys.path.insert(0, str(REPO_ROOT / "modules" / "m2-comms"))
import log_dispatch  # noqa: E402

PASS_COUNT, FAIL_COUNT = 0, 0


def check(label, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {label}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {label}  {detail}")


PLAN = {
    "run_id": "m2-test", "event_slug": "darwinbox-ai-in-hr-2026-08-13",
    "recipients": [
        {"email": "kai8karma+attendee@gmail.com", "hubspot_contact_id": "111", "segment": "attendee",
         "subject": "Thanks for joining", "body_text": "text", "body_html": "<p>html</p>"},
        {"email": "kai8karma+noshow@gmail.com", "hubspot_contact_id": "222", "segment": "no_show",
         "subject": "Sorry we missed you", "body_text": "text", "body_html": "<p>html</p>"},
        {"email": "kai8karma+speaker@gmail.com", "hubspot_contact_id": "333", "segment": "speaker",
         "subject": "Great talk", "body_text": "text", "body_html": "<p>html</p>"},
    ],
}
RESULTS = [
    {"email": "kai8karma+attendee@gmail.com", "segment": "attendee", "provider": "gmail",
     "message_id": "msg-1", "sent_to": "kai8karma+attendee@gmail.com",
     "sent_at": "2026-09-13T10:00:00Z", "status": "sent", "error": None},
    {"email": "kai8karma+noshow@gmail.com", "segment": "no_show", "provider": "gmail",
     "message_id": None, "sent_to": "kai8karma+noshow@gmail.com",
     "sent_at": "2026-09-13T10:00:01Z", "status": "sent", "error": None},
    {"email": "kai8karma+speaker@gmail.com", "segment": "speaker", "provider": "gmail",
     "message_id": "msg-3", "sent_to": "kai8karma+speaker@gmail.com",
     "sent_at": None, "status": "failed", "error": "bounced"},
]


def scenario_build_receipt_dry_run():
    print("=== scenario 1: build_receipt(dry_run=True) -- direct import, network patched to raise ===")
    with mock.patch.object(log_dispatch, "http_call", side_effect=AssertionError("network attempted in dry-run")):
        receipt = log_dispatch.build_receipt(PLAN, RESULTS, token=None, dry_run=True)

    logged_emails = {row["email"] for row in receipt["logged"]}
    skipped_emails = {row["email"] for row in receipt["skipped"]}
    check("sent+message_id (attendee) -> logged", "kai8karma+attendee@gmail.com" in logged_emails, receipt)
    check("logged row uses dry-run placeholder engagement_id",
          all(row["engagement_id"] == "(dry-run)" for row in receipt["logged"]), receipt["logged"])
    check("status=failed (speaker) -> skipped, not logged",
          "kai8karma+speaker@gmail.com" in skipped_emails and "kai8karma+speaker@gmail.com" not in logged_emails,
          receipt)
    check("missing message_id (no_show) -> skipped, not logged",
          "kai8karma+noshow@gmail.com" in skipped_emails and "kai8karma+noshow@gmail.com" not in logged_emails,
          receipt)
    check("exactly 1 logged, 2 skipped, 0 errors",
          len(receipt["logged"]) == 1 and len(receipt["skipped"]) == 2 and len(receipt["errors"]) == 0, receipt)
    check("portal_id is None in dry-run (no network call made to fetch it)", receipt["portal_id"] is None, receipt)


def scenario_cli_dry_run(tmp_out: Path):
    print("\n=== scenario 2: real CLI (subprocess) --dry-run, no HUBSPOT_TOKEN in env ===")
    plan_path, results_path, receipt_path = tmp_out / "plan.json", tmp_out / "results.json", tmp_out / "receipt.json"
    plan_path.write_text(json.dumps(PLAN), encoding="utf-8")
    results_path.write_text(json.dumps(RESULTS), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "modules" / "m2-comms" / "log_dispatch.py"),
         "--plan", str(plan_path), "--results", str(results_path), "--receipt", str(receipt_path), "--dry-run"],
        capture_output=True, text=True, timeout=15, env={"PATH": "/usr/bin:/bin"},
    )
    check("CLI exits 0", proc.returncode == 0, proc.stderr)
    check("receipt file written", receipt_path.exists(), proc.stdout)
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        check("CLI receipt: 1 logged, 2 skipped", len(receipt["logged"]) == 1 and len(receipt["skipped"]) == 2, receipt)


def main():
    scenario_build_receipt_dry_run()
    with tempfile.TemporaryDirectory() as td:
        scenario_cli_dry_run(Path(td))

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
