# HubSpot Push — `push_to_hubspot.py`

Real CRM v3/v4 client for M1's upsert CSVs + M2's `sends_log.json`. Stdlib only, zero network unless a token is present and `--dry-run` is absent.

**Scopes**: `crm.objects.contacts.read/write`, `crm.objects.companies.read/write`, `crm.schemas.contacts.read/write`, `crm.schemas.companies.read/write`.

**Run order**: (1) ensure-properties (`icp_tier`, `icp_rationale`, `confidence`, `attendance_status`, `needs_review`, `event_tag` on Contacts; `event_tag` on Companies; 409 = success) → (2) upsert companies — search-by-`domain` then batch/update + batch/create, because HubSpot does not unique-index company `domain` (batch/upsert 400s on it; learned on the live sandbox push) → (3) upsert contacts (idProperty `email`) → (4) associate, using the IDs the two upserts returned → (5) `--log-emails` (optional, gated on `m2/approval_gate.json`, `--force-log` to override) → (6) `--verify` (optional).

**Property gotcha**: HubSpot's company `industry` is a fixed enum (e.g. `COMPUTER_SOFTWARE`, `FINANCIAL_SERVICES`, `INFORMATION_TECHNOLOGY_AND_SERVICES`); M1's human-readable labels are mapped through `HUBSPOT_INDUSTRY_ENUM` in `push_to_hubspot.py` before the write — unmapped labels are dropped from the payload rather than 400-ing the whole batch.

**Dry-run** (no token, zero network — `network_calls_made=0` in the plan):
`python3 modules/m1-enrichment/push_to_hubspot.py --dry-run --log-emails --verify`

**Live** (needs `HUBSPOT_TOKEN` env or `~/.config/postevent/hubspot.env`):
`python3 modules/m1-enrichment/push_to_hubspot.py --limit 10` (smoke), then `python3 modules/m1-enrichment/push_to_hubspot.py --log-emails --verify`

**Read-back**: `--verify` calls the same search API a judge/dashboard would use — `POST /crm/v3/objects/{contacts,companies}/search` filtered on `event_tag EQ <tag>` — or filter the portal UI's Contacts/Companies view on `Event Tag`. M4's dashboard itself reads M1's local files, not HubSpot.

**Not pushed**: `hubspot_owner_email`/`hubspot_owner_id` — stays CSV-only (sandbox has no owners matching the CSV's SDR emails).

**Sandbox caveat**: `--verify`'s email count is an unfiltered first-page sample (`GET /crm/v3/objects/emails?limit=100`), not a filtered total — the Email engagement object has no `event_tag` property, only an association back to its tagged contact. Contacts/Companies counts are exact totals.
