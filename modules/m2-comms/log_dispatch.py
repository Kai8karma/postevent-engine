#!/usr/bin/env python3
"""log_dispatch.py -- logs n8n's real dispatch_results.json sends as HubSpot
Email engagements, associated to the recipient contact. See docs/module-api.md
(M2 phase table + dispatch_plan.json / dispatch_results.json schemas) -- this
script is built against those schemas, not against comms.py's current output.

For every dispatch_results.json row with status=="sent" and a non-empty
message_id: resolve the contact (plan's hubspot_contact_id, else search-or-
create by email), then create ONE HubSpot email engagement associated to
that contact. Rows with status=="failed" or no message_id are never logged
-- they land in the receipt's "skipped" list. Idempotent: a message_id
already logged for its contact is found and reused, never duplicated.

Two lanes:
  - --dry-run: zero network calls (no token needed). Every row that would be
    logged gets a "(dry-run)" placeholder contact_id/engagement_id instead
    of a real HubSpot call -- proves the plan/results wiring without
    touching the portal.
  - live (default, requires HUBSPOT_TOKEN or ~/.config/postevent/hubspot.env):
    real HubSpot CRM v3 calls. Attempts to create a custom `postevent_message_id`
    text property on the `emails` object once (409 already-exists counts as
    success); if the portal rejects custom properties on that object, falls
    back to prefixing "<provider>:<message_id>" onto hs_email_text instead,
    and the receipt says so via a stderr note.

Association uses HUBSPOT_DEFINED type 198 (email -> contact), passed inline
in the same POST that creates the engagement -- the same pattern already
proven against this portal by modules/m1-enrichment/push_to_hubspot.py
(see its ASSOC_EMAIL_CONTACT_TYPE_ID), rather than a separate v4 association
call.

Stdlib only (argparse, json, os, sys, urllib.request, urllib.error,
datetime, pathlib) -- no LLM calls, ever.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HUBSPOT_ENV_PATH = Path.home() / ".config" / "postevent" / "hubspot.env"
API_BASE = "https://api.hubapi.com"
ASSOC_EMAIL_CONTACT_TYPE_ID = 198  # HUBSPOT_DEFINED email->contact, see push_to_hubspot.py
RATE_LIMIT_SLEEP = 0.15


class HubspotLogError(RuntimeError):
    """A HubSpot call this script cannot recover from for one row -- caught
    per-row in build_receipt() and written to the receipt's 'errors' list,
    never raised out of main()."""


# --------------------------------------------------------------------------
# token / HTTP (same shape as push_to_hubspot.py's http_call -- independent
# copy, per this repo's per-module HubSpot-client convention)
def resolve_token():
    token = os.environ.get("HUBSPOT_TOKEN")
    if token:
        return token.strip()
    if HUBSPOT_ENV_PATH.exists():
        for line in HUBSPOT_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "HUBSPOT_TOKEN":
                return value.strip().strip('"').strip("'")
    return None


def http_call(method: str, path: str, token: str, body, timeout: int = 30, _retried: bool = False):
    """Returns (status_code_or_None, parsed_json_or_None, error_text)."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode("utf-8", "replace")
        if e.code == 429 and not _retried:
            try:
                retry_after = float(e.headers.get("Retry-After", "1"))
            except (TypeError, ValueError):
                retry_after = 1.0
            time.sleep(retry_after)
            return http_call(method, path, token, body, timeout=timeout, _retried=True)
        return e.code, None, text[:300]
    except urllib.error.URLError as e:
        return None, None, str(e)[:300]


def fetch_portal_id(token):
    status, parsed, _err = http_call("GET", "/account-info/v3/details", token, None)
    return parsed.get("portalId") if status == 200 and isinstance(parsed, dict) else None


# --------------------------------------------------------------------------
# emails.postevent_message_id custom property (attempted once per run)
def ensure_message_id_property(token) -> bool:
    group = "emailinformation"
    status, parsed, _err = http_call("GET", "/crm/v3/properties/emails/hs_email_subject", token, None)
    if status == 200 and isinstance(parsed, dict) and parsed.get("groupName"):
        group = parsed["groupName"]
    body = {"name": "postevent_message_id", "label": "Post-Event Message ID",
            "type": "string", "fieldType": "text", "groupName": group}
    status, _parsed, err = http_call("POST", "/crm/v3/properties/emails", token, body)
    if status in (200, 201, 409):
        return True
    print(f"[warn] custom property postevent_message_id not available on emails (status={status} {err}) "
          "-- falling back to embedding provider:message_id in hs_email_text", file=sys.stderr)
    return False


# --------------------------------------------------------------------------
# contacts.postevent_event custom property -- same create-once/409-tolerant/
# graceful-fallback shape as ensure_message_id_property() above.
# modules/m1-enrichment/push_to_hubspot.py defines this exact property
# (name/label/groupName) when it runs its own ensure-properties step; this
# portal was purged and may not have had that step run against it yet, so
# this script can't assume it exists -- it ensures it for itself instead of
# depending on the other module having run first.
def ensure_event_property(token) -> bool:
    body = {"name": "postevent_event", "label": "Post-Event Event Slug",
            "type": "string", "fieldType": "text", "groupName": "contactinformation"}
    status, _parsed, err = http_call("POST", "/crm/v3/properties/contacts", token, body)
    if status in (200, 201, 409):
        return True
    print(f"[warn] custom property postevent_event not available on contacts (status={status} {err}) "
          "-- creating contacts without it", file=sys.stderr)
    return False


# --------------------------------------------------------------------------
# contact resolution
def search_contact_by_email(token, email):
    body = {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email"], "limit": 1}
    status, parsed, err = http_call("POST", "/crm/v3/objects/contacts/search", token, body)
    if status in (200, 201):
        results = (parsed or {}).get("results") or []
        return results[0]["id"] if results else None
    raise HubspotLogError(f"contact search failed for {email}: {status} {err}")


def create_contact(token, email, event_slug, event_prop_available, firstname=None):
    props = {"email": email}
    if event_prop_available:
        props["postevent_event"] = event_slug or ""
    if firstname:
        props["firstname"] = firstname
    status, parsed, err = http_call("POST", "/crm/v3/objects/contacts", token, {"properties": props})
    if status in (200, 201):
        return parsed["id"]
    raise HubspotLogError(f"contact create failed for {email}: {status} {err}")


def resolve_contact_id(token, recipient, event_slug, event_prop_available):
    existing = recipient.get("hubspot_contact_id")
    if existing:
        return existing
    email = recipient["email"]
    found = search_contact_by_email(token, email)
    if found:
        return found
    return create_contact(token, email, event_slug, event_prop_available, recipient.get("firstname"))


# --------------------------------------------------------------------------
# engagement create (idempotent on message_id)
def find_existing_engagement(token, contact_id, message_id, custom_prop_available, subject, sent_at):
    if custom_prop_available:
        filters = [{"propertyName": "postevent_message_id", "operator": "EQ", "value": message_id}]
    else:
        filters = [{"propertyName": "hs_email_subject", "operator": "EQ", "value": subject or ""},
                   {"propertyName": "hs_timestamp", "operator": "EQ", "value": sent_at or ""}]
    body = {"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1}
    status, parsed, err = http_call("POST", "/crm/v3/objects/emails/search", token, body)
    if status in (200, 201):
        results = (parsed or {}).get("results") or []
        return results[0]["id"] if results else None
    raise HubspotLogError(f"engagement idempotency search failed: {status} {err}")


def create_engagement(token, contact_id, recipient, row, message_id, custom_prop_available):
    subject = recipient.get("subject", "")
    body_text = recipient.get("body_text", "")
    if not custom_prop_available:
        body_text = f"{row.get('provider', '')}:{message_id}\n{body_text}"
    props = {
        "hs_timestamp": row.get("sent_at") or datetime.now(timezone.utc).isoformat(),
        "hs_email_direction": "EMAIL",
        "hs_email_status": "SENT",
        "hs_email_subject": subject,
        "hs_email_text": body_text,
        "hs_email_html": recipient.get("body_html", ""),
    }
    if custom_prop_available:
        props["postevent_message_id"] = message_id
    body = {"properties": props,
            "associations": [{"to": {"id": contact_id},
                               "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                          "associationTypeId": ASSOC_EMAIL_CONTACT_TYPE_ID}]}]}
    status, parsed, err = http_call("POST", "/crm/v3/objects/emails", token, body)
    if status in (200, 201):
        return parsed["id"]
    raise HubspotLogError(f"engagement create failed: {status} {err}")


def log_one(token, event_slug, recipient, row, custom_prop_available, event_prop_available, dry_run):
    email = row["email"]
    provider = row.get("provider")
    sent_to = row.get("sent_to", email)
    if dry_run:
        return {"email": email, "contact_id": recipient.get("hubspot_contact_id") or "(dry-run)",
                "engagement_id": "(dry-run)", "sent_to": sent_to, "provider": provider}
    contact_id = resolve_contact_id(token, recipient, event_slug, event_prop_available)
    time.sleep(RATE_LIMIT_SLEEP)
    message_id = row["message_id"]
    existing_id = find_existing_engagement(token, contact_id, message_id, custom_prop_available,
                                            recipient.get("subject"), row.get("sent_at"))
    time.sleep(RATE_LIMIT_SLEEP)
    engagement_id = existing_id or create_engagement(token, contact_id, recipient, row, message_id,
                                                      custom_prop_available)
    if not existing_id:
        time.sleep(RATE_LIMIT_SLEEP)
    return {"email": email, "contact_id": contact_id, "engagement_id": engagement_id,
            "sent_to": sent_to, "provider": provider}


# --------------------------------------------------------------------------
# receipt (pure function, no file I/O -- importable/testable directly)
def build_receipt(plan: dict, results: list, token, dry_run: bool) -> dict:
    event_slug = plan.get("event_slug", "")
    recipients_by_email = {r["email"]: r for r in plan.get("recipients", []) if r.get("email")}

    custom_prop_available = False if dry_run else ensure_message_id_property(token)
    event_prop_available = False if dry_run else ensure_event_property(token)

    logged, skipped, errors = [], [], []
    for row in results:
        email = row.get("email")
        status = row.get("status")
        message_id = row.get("message_id")
        if status != "sent" or not message_id:
            skipped.append({"email": email, "status": status, "message_id": message_id,
                             "reason": "status=failed" if status == "failed" else "missing message_id"})
            continue
        recipient = recipients_by_email.get(email)
        if recipient is None:
            errors.append({"email": email, "error": f"recipient not found in dispatch_plan.json for {email}"})
            continue
        try:
            logged.append(log_one(token, event_slug, recipient, row, custom_prop_available,
                                   event_prop_available, dry_run))
        except HubspotLogError as exc:
            errors.append({"email": email, "error": str(exc)})

    return {
        "portal_id": None if dry_run else fetch_portal_id(token),
        "ts": datetime.now(timezone.utc).isoformat(),
        "logged": logged, "skipped": skipped, "errors": errors,
    }


# --------------------------------------------------------------------------
def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Log n8n's dispatch_results.json sends as HubSpot email engagements.")
    ap.add_argument("--plan", required=True, help="dispatch_plan.json path")
    ap.add_argument("--results", required=True, help="dispatch_results.json path")
    ap.add_argument("--receipt", required=True, help="where to write the receipt JSON")
    ap.add_argument("--dry-run", action="store_true", help="zero network calls -- placeholder ids only")
    args = ap.parse_args()

    plan = load_json(Path(args.plan))
    results = load_json(Path(args.results))

    token = None
    if not args.dry_run:
        token = resolve_token()
        if not token:
            print(f"error: no HubSpot token found -- set HUBSPOT_TOKEN or write {HUBSPOT_ENV_PATH} "
                  "(KEY=VALUE lines, HUBSPOT_TOKEN=...). Use --dry-run to preview without one.", file=sys.stderr)
            sys.exit(1)

    receipt = build_receipt(plan, results, token, args.dry_run)

    receipt_path = Path(args.receipt)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"receipt written: {receipt_path} logged={len(receipt['logged'])} "
          f"skipped={len(receipt['skipped'])} errors={len(receipt['errors'])}")


if __name__ == "__main__":
    main()
