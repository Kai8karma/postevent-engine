# HubSpot Push -- `push_to_hubspot.py`

Real CRM v3/v4 client for M1's upsert CSVs. Stdlib only, zero network for every write call unless a token is present and
`--dry-run` is absent -- owner routing's `GET /crm/v3/owners` is the one
read-only exception, see "Ownership" below.

**Run order**: (0) owner routing -- read `config/icp.yaml`'s `icp.owner_map`,
resolve each mapped email to a real `ownerId` via live `GET /crm/v3/owners`
(read-only, runs even under `--dry-run`) -> (1) ensure-properties (`icp_tier`,
`icp_rationale`, `confidence`, `attendance_status`, `needs_review`,
`event_tag` on Contacts; `event_tag` on Companies; 409 = success) -> (2)
sync companies -- search by `domain`, then `POST .../companies/batch/update`
for matches and `POST .../companies/batch/create` for misses (**not**
`batch/upsert` with `idProperty=domain`: HubSpot doesn't unique-index company
`domain` and that call 400s -- see below) -> (3) upsert contacts
(`idProperty=email`, `hubspot_owner_id` set from step 0's resolution when
available), optionally extended by `--include-speakers` (see below) -> (4)
associate, using the IDs steps 2-3 returned -> (5) `--log-emails` (optional,
and inert in this build -- see below) -> (6) `--verify` (optional).

**`--log-emails` cannot run in this build.** It reads `m2/sends_log.json`, and
nothing writes that file any more -- M2 writes `dispatch_plan.json` and
`comms.json` -- so `run_log_emails()` returns `status: "skipped"` on its
missing-file guard before the `approval_gate.json` gate is even reached. The
engagement logging that does run is `modules/m2-comms/log_dispatch.py` (the
module API's m2 `log` phase, over n8n's `dispatch_results.json`), and
sends.

**Company sync, not upsert**: `run_company_sync()` searches
`POST /crm/v3/objects/companies/search` on `domain` in chunks, then splits the
chunk into `batch/update` (id from the search hit) and `batch/create` (no hit).
Returns the same `(ok, fail, sample_body, responses)` shape as the contact
batch step, so the caller and `extract_id_map()` are unchanged.

**`--include-speakers`** (step 3 extra): upserts the `data/incoming/speakers.json`
contacts directly, joined to their address in `data/fixtures/segments.json`'s
`speakers` email list by `firstname.lastname` localpart (case-insensitive,
order-independent -- see `match_speaker_email()`). Role is marked the same way
`enrich.py::load_speakers()` already marks it for these same people:
`lifecyclestage=evangelist` (a standard property, reused rather than adding a
new custom one) plus a human-readable `role=speaker` note appended to the
`icp_rationale` custom property. Zero network in `--dry-run` -- reads only
the two local JSON fixtures.

**Note on the CSV already containing speakers**: `enrich.py::load_speakers()`
already folds speakers into `hubspot_contacts.csv` by default (no flag needed
on the M1 side) -- a fresh `--in` dir already has them. `--include-speakers`
is therefore a belt-and-suspenders path for `--in` dirs generated before that
fix, or for a bare speaker-only re-push; running it against a current M1
output upserts the same speakers twice (once via the CSV, once via this
flag) -- harmless, since HubSpot upsert is idempotent on `idProperty=email`.

**Batch salvage**: a HubSpot batch call is all-or-nothing -- one invalid
record 400s the whole request and every valid record in that chunk loses its
id too. `salvage_failed_chunk()` re-sends a rejected chunk one record at a
time so the valid rows still land; genuinely bad records are surfaced in the
run's rejected list, never silently dropped.

**Property gotcha**: HubSpot's company `industry` is a fixed enum (e.g.
`COMPUTER_SOFTWARE`, `FINANCIAL_SERVICES`,
`INFORMATION_TECHNOLOGY_AND_SERVICES`); M1's human-readable labels are mapped
through `HUBSPOT_INDUSTRY_ENUM` in `push_to_hubspot.py` before the write --
unmapped labels are dropped from the payload rather than 400-ing the whole
batch.

**Dry-run** (no token, zero network -- `network_calls_made=0` in the plan):
`python3 modules/m1-enrichment/push_to_hubspot.py --dry-run --verify`
`python3 modules/m1-enrichment/push_to_hubspot.py --dry-run --include-speakers`

**Live** (needs `HUBSPOT_TOKEN` env or `~/.config/postevent/hubspot.env`):
`python3 modules/m1-enrichment/push_to_hubspot.py --limit 10` (smoke), then
`python3 modules/m1-enrichment/push_to_hubspot.py --verify`

**Read-back**: `--verify` calls the same search API a judge/dashboard would
use -- `POST /crm/v3/objects/{contacts,companies}/search` filtered on
`event_tag EQ <tag>` -- or filter the portal UI's Contacts/Companies view on
`Event Tag`. `--event-tag` additionally sets a `postevent_event` contact
property so a portal shared across events stays filterable by event,
independent of the per-run `event_tag`.

**`--receipt PATH`**: writes a JSON receipt (`{portal_id, stages,
contacts_upserted, companies_upserted, associations, verify, ts}`) alongside
the stdout summary the script already prints.

**Ownership (`hubspot_owner_id`) -- real, resolved live**:
`hubspot_owner_email` on the CSVs (e.g. `sdr.india@darwinbox.com`, from
`config/icp.yaml`'s `icp.owners`) is a demo-synthetic routing label only --
it is never sent to HubSpot, and the region/bucket computation behind it is
unchanged. Actual portal ownership is a separate, real mapping:
`config/icp.yaml`'s `icp.owner_map` section maps each routing bucket
(`INDIA`/`SEA`/`MENA`/`NA`/`UKEU`) to a real portal owner **email**;
`push_to_hubspot.py` resolves that email to a real `ownerId` via a live
`GET /crm/v3/owners` call (`resolve_owner_ids()`) and sets
`hubspot_owner_id` on the contact upsert. This owners GET is read-only and
explicitly runs even under `--dry-run` (so the dry-run plan shows real
resolution, not a placeholder) -- every write step still stays at zero
network under `--dry-run`. A row whose bucket has no `owner_map` entry, or
whose mapped email has no matching live owner, has `hubspot_owner_id`
omitted and counted -- never invented.

`config/icp.yaml` notes the sandbox portal (247135551) currently has exactly
one owner and maps all five routing buckets to it for this demo; a real
portal would map each bucket to its own SDR's owner id.

**Sandbox caveat**: `--verify`'s email count is an unfiltered first-page
sample (`GET /crm/v3/objects/emails?limit=100`), not a filtered total -- the
Email engagement object has no `event_tag` property, only an association
back to its tagged contact. Contacts/Companies counts are exact totals.
