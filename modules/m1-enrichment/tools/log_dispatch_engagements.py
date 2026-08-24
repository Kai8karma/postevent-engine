#!/usr/bin/env python3
"""Log the n8n M2 dispatch-demo's REAL sends as HubSpot Email engagements.

Exists because push_to_hubspot.py --log-emails logs the M2 module's
sends_log.json batch plan -- but the first real dispatch (2026-08-24,
n8n workflow rxJnu5ZATDFLJToD, execution 4) went out through the demo
lane with its own subjects/bodies. Logging the batch plan as SENT would
misrepresent what actually dispatched; this script logs exactly the three
emails Gmail actually sent, with their Gmail message ids, and nothing else.

Input: a dispatch receipt JSON (list of {segment, override_to,
intended_recipient, subject, sla_hours_after_close, gmail_message_id,
sent_at}) captured from the n8n execution's "Demo Send Receipt" node.

Contact association: intended_recipient looked up live in HubSpot by email.
Missing contact (the .example speaker) => engagement skipped and counted,
never invented. Token comes from the same env var push_to_hubspot.py uses,
falling back to ~/.config/postevent/hubspot.env. Stdlib only.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent))
from push_to_hubspot import ASSOC_EMAIL_CONTACT_TYPE_ID  # noqa: E402

TOKEN_ENV = "HUBSPOT_TOKEN"  # same contract as push_to_hubspot.py
HUBSPOT_ENV_FILE = Path.home() / ".config" / "postevent" / "hubspot.env"
BASE = "https://api.hubapi.com"


def load_token() -> str:
    tok = os.environ.get(TOKEN_ENV, "")
    if tok:
        return tok
    if HUBSPOT_ENV_FILE.exists():
        for line in HUBSPOT_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(TOKEN_ENV + "="):
                return line.partition("=")[2].strip().strip("'\"")
    raise SystemExit(f"no {TOKEN_ENV} in env or {HUBSPOT_ENV_FILE}")


def api(token: str, method: str, path: str, payload: dict = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def find_contact_id(token: str, email: str):
    res = api(token, "POST", "/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email"], "limit": 1})
    results = res.get("results", [])
    return results[0]["id"] if results else None


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: log_dispatch_engagements.py <dispatch_receipt.json>")
    receipt = json.loads(Path(sys.argv[1]).read_text())
    token = load_token()
    logged, skipped = [], []
    for send in receipt["sends"]:
        contact_id = find_contact_id(token, send["intended_recipient"])
        if not contact_id:
            skipped.append({**send, "skip_reason": "no contact for intended_recipient"})
            print(f"skip {send['segment']}: no contact for {send['intended_recipient']}", file=sys.stderr)
            continue
        body = {
            "properties": {
                "hs_timestamp": send["sent_at"],
                "hs_email_direction": "EMAIL",
                "hs_email_status": "SENT",
                "hs_email_subject": send["subject"],
                "hs_email_text": send["body_text"],
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": ASSOC_EMAIL_CONTACT_TYPE_ID}],
            }],
        }
        created = api(token, "POST", "/crm/v3/objects/emails", body)
        logged.append({"segment": send["segment"], "engagement_id": created.get("id"),
                       "contact_id": contact_id, "gmail_message_id": send["gmail_message_id"]})
        print(f"logged {send['segment']}: engagement {created.get('id')} -> contact {contact_id}")
    out = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "source": receipt.get("source", ""),
        "logged": logged, "skipped": skipped,
    }
    out_path = Path(sys.argv[1]).with_name("engagement_log_receipt.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"receipt -> {out_path} ({len(logged)} logged, {len(skipped)} skipped)")


if __name__ == "__main__":
    main()
