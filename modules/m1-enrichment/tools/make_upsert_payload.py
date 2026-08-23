#!/usr/bin/env python3
"""Generates hubspot_upsert_payload.json -- a sample HubSpot CRM v3 batch
upsert payload built from the real M1 pipeline output (not hand-written), so
it can never drift from what enrich.py actually produces the way clay_spec.md
used to (same discipline as clay_spec.md's header).

Reads (in order, first 5 data rows of each):
  out/<event-slug>/m1/hubspot_companies.csv
  out/<event-slug>/m1/hubspot_contacts.csv
<event-slug> is computed the same way orchestrator/run_pipeline.py computes
its default --out dir (slugified data/incoming/event.json event_name).

Run: `python3 orchestrator/run_pipeline.py` first (writes the default --out
dir this script reads), then `python3 modules/m1-enrichment/tools/make_upsert_payload.py`.

Stdlib only (csv, json, re, pathlib).
"""
import csv
import json
import re
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_DIR.parent.parent
EVENT_JSON = REPO_ROOT / "data" / "incoming" / "event.json"
SAMPLE_SIZE = 5

# HubSpot custom contact properties that don't exist natively -- must be
# created (Settings -> Properties -> Contact) before a real batch/upsert call
# against these payloads would succeed. Mirrors clay_spec.md's "two object
# types" section.
CUSTOM_CONTACT_PROPERTIES = ["icp_tier", "icp_rationale", "confidence", "attendance_status", "needs_review"]


def slugify(name: str) -> str:
    """Identical to orchestrator/run_pipeline.py::slugify -- do not let the
    two drift, or this script will look in the wrong --out dir."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "event"


def event_slug() -> str:
    try:
        event = json.loads(EVENT_JSON.read_text(encoding="utf-8"))
        return slugify(event.get("event_name", "event"))
    except (OSError, json.JSONDecodeError):
        return "event"


def read_csv_rows(path: Path, limit: int) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit]


def company_upsert_input(row: dict) -> dict:
    domain = row["domain"]
    return {
        "idProperty": "domain",
        "id": domain,
        "properties": dict(row),
    }


def contact_upsert_input(row: dict) -> dict:
    email = row["email"]
    return {
        "idProperty": "email",
        "id": email,
        "properties": dict(row),
    }


def association_input(contact_row: dict) -> dict:
    """Contact -> Company association object, HUBSPOT_DEFINED / typeId 1
    (Contact-to-Company, primary). `from`/`to` use email/domain here as
    human-readable placeholders for the real HubSpot record IDs -- those IDs
    only exist after the companies_batch and contacts_batch upserts above
    have actually run, so a live import resolves them from those two
    responses before calling the real associations batch/create endpoint.
    company_domain is blank for freemail contacts (no Company object to
    associate to) -- such rows are skipped here, same rule as
    build_company_rows() in enrich.py."""
    return {
        "from": {"id": contact_row["email"], "id_source": "email (placeholder -- real call uses the contact ID returned by contacts_batch upsert)"},
        "to": {"id": contact_row["company_domain"], "id_source": "domain (placeholder -- real call uses the company ID returned by companies_batch upsert)"},
        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
    }


def main():
    slug = event_slug()
    m1_dir = REPO_ROOT / "out" / slug / "m1"
    companies_csv = m1_dir / "hubspot_companies.csv"
    contacts_csv = m1_dir / "hubspot_contacts.csv"

    for label, p in (("hubspot_companies.csv", companies_csv), ("hubspot_contacts.csv", contacts_csv)):
        if not p.exists():
            print(
                f"error: {label} not found at {p}\n"
                "run `python3 orchestrator/run_pipeline.py` first to generate M1 output.",
                file=sys.stderr,
            )
            sys.exit(1)

    company_rows = read_csv_rows(companies_csv, SAMPLE_SIZE)
    contact_rows = read_csv_rows(contacts_csv, SAMPLE_SIZE)
    # Only associate contacts that actually have a resolvable company_domain
    # (freemail/unresolved-domain contacts have no Company object -- same
    # exclusion rule as enrich.py::build_company_rows()).
    assoc_rows = [r for r in contact_rows if r.get("company_domain")][:SAMPLE_SIZE]

    payload = {
        "_generated_from": f"out/{slug}/m1/hubspot_companies.csv + hubspot_contacts.csv (first {SAMPLE_SIZE} rows each), by modules/m1-enrichment/tools/make_upsert_payload.py",
        "companies_batch": {
            "endpoint": "POST /crm/v3/objects/companies/batch/upsert",
            "inputs": [company_upsert_input(r) for r in company_rows],
        },
        "contacts_batch": {
            "endpoint": "POST /crm/v3/objects/contacts/batch/upsert",
            "inputs": [contact_upsert_input(r) for r in contact_rows],
        },
        "associations_batch": {
            "endpoint": "POST /crm/v4/associations/contacts/companies/batch/create",
            "inputs": [association_input(r) for r in assoc_rows],
        },
        "notes": [
            "Import order: companies_batch, then contacts_batch, then associations_batch -- "
            "associations need the HubSpot record IDs returned by the first two calls "
            "(this sample uses domain/email as human-readable placeholders for those IDs).",
            "Custom contact properties that must exist in HubSpot (Settings -> Properties) "
            "before contacts_batch will succeed: " + ", ".join(CUSTOM_CONTACT_PROPERTIES) + ". "
            "event_tag is not a contact/company property in this pipeline -- it is M3's "
            "per-asset campaign tag (modules/m3-repurpose/repurpose.py::event_tag()); if a "
            "HubSpot campaign rollup property is wanted on Contact, create it separately.",
            "companies_batch matches on `domain`; contacts_batch matches on `email` -- both "
            "per clay_spec.md's two-object-type section. hubspot_owner_email and "
            "hubspot_contact_id are import-time lookup/join columns only, never persisted "
            "as literal HubSpot properties (see modules/m1-enrichment/README.md).",
        ],
    }

    out_path = MODULE_DIR / "hubspot_upsert_payload.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path} (companies={len(company_rows)}, contacts={len(contact_rows)}, associations={len(assoc_rows)}, from out/{slug}/m1/)")


if __name__ == "__main__":
    main()
