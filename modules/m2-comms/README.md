# M2 — Post-Event Communications

Turns the recording, the transcript and M1's enriched contacts into three segment-specific emails
and one fully rendered message per recipient, ready for n8n to dispatch inside 24 hours of event
close. Live by default: nothing here replays cached copy unless you ask for it.

## Input

| file | what it carries |
|---|---|
| `data/incoming/event.json` | event name, date, `start_time_local` + `timezone` + `duration_min` (→ `event_close_ts`), recording URL, registration page, speakers |
| `data/incoming/transcript.md` | the real transcript, `[MM:SS] **Speaker:** text` turns |
| `data/incoming/speakers.json` | speaker bios and the inbox each speaker's mail goes to (`email`) |
| `data/fixtures/segments.json` | attendee / no-show / speaker address lists |
| `--enriched out/<run>/m1/hubspot_ready.csv` | M1's contacts: `firstname`, `jobtitle`, `company`, `industry`, `function`, `icp_tier`, `attendance_status`, `time_in_session_minutes`, `hubspot_contact_id`, `suppression_reason` |

Without `--enriched` M2 falls back to `segments.json`, which is neither deduped nor
suppression-filtered, and says so on stderr. Rows M1 flagged with a `suppression_reason` are
excluded from `recipients` and counted in `counts.suppressed`.

## AI role

Four calls, budget five (`MAX_LLM_CALLS`), one retry in reserve:

1. **Extraction** (the only call that sees the transcript) → `extraction.json`: 5–7 takeaways with
   `[MM:SS]` anchors, 4–6 verbatim quotes with speaker + timestamp, the 3 moments worth the click
   for a no-show, a one-line premise, and the personalisation matrix — one angle per CRM `function`
   (6) and one follow-on sentence per industry bucket (3).
2. **One call per segment** (attendee / no-show / speaker), each seeing only the extraction, each
   returning `subject_a`, `subject_b`, `preheader`, `body_md` and `takeaways[]`. The body is a
   template: `{{firstname}}`, `{{takeaway_headline}}`, `{{takeaway_body}}`, `{{recording_link}}`,
   `{{cta_link}}`, and for speakers the `{{snapshot_*}}` fields. Prompts live in `prompts/`.

**Grounding gate.** Every quoted span and every `[MM:SS]` in the extraction, the variants and the
rendered bodies is matched against the transcript (case- and punctuation-insensitive for quotes,
exact for timestamps). A quote that cannot be matched at extraction time is dropped and listed; a
miss anywhere in generated copy fails the run before a plan is written. Result: `grounding.json`.

**Personalisation.** `takeaway_headline` / `takeaway_body` resolve per contact from their M1
`function` (primary) plus an industry-bucket sentence (appended). The speaker email carries a
performance snapshot computed from M1 + segments — registrants, attendees, attendance rate, average
and median minutes in session, no-show count, top 5 accounts by attendee count — plus that
speaker's own quotes. A lint refuses any speaker email that refers to its own recipient in the
third person.

## Tools

- LLM: OpenRouter (`OPENROUTER_API_KEY` env or `~/.config/postevent/llm.env`; never printed),
  `OPENROUTER_MODEL` as a comma-separated chain. `LLM_BACKEND=openrouter|claude|auto`.
  `LLM_BATCH_DEADLINE_S` (default 120) is a per-call wall clock: a hung request is abandoned and
  the next model in the chain is tried. `LLM_MAX_TOKENS` (default 8000) also sets the reservation
  OpenRouter's affordability check bills against — lower it on a near-empty key. All models failing
  fails the run — stale copy is never shipped as live.
- n8n orchestration and HubSpot send/logging are downstream and are documented by their owners:
  [`docs/module-api.md` §M2](../../docs/module-api.md) (phases `generate` / `approve` / `log`, and
  the `dispatch_plan.json` contract) and
  [`orchestrator/n8n/railway/README.md`](../../orchestrator/n8n/railway/README.md).

## Output

- `dispatch_plan.json` — the contract in `docs/module-api.md` §M2: `run_id`, `event_slug`,
  `generated_at`, `lane`, `event_close_ts`, `variants` (3 × two subjects + preheader + body +
  takeaways; speaker also `snapshot`), `recipients[]` (one rendered message per mailable contact:
  subject alternating A/B by row index, `body_html`, `body_text`, UTM-tagged `links`, `utm`,
  `hubspot_contact_id`, `demo_redirect_to`), `approval`, `counts`.
- `emails/<segment>.md` — the draft a human reads: frontmatter + body with merge fields intact,
  plus the personalisation preview for the two bulk segments.
- `approval_gate.json` — `pending_human_approval`. This module never sends.
- `extraction.json`, `grounding.json`, `comms.json`, `receipts/m2_llm_calls.json`.

UTM on every link: `utm_source=webinar`, `utm_medium=email`, `utm_campaign=<event_slug>`,
`utm_content=<segment>-<a|b>` (`shared/utm.py`).

## Run

```bash
set -a; . ~/.config/postevent/llm.env; set +a          # never echo the key
export OPENROUTER_MODEL="nvidia/nemotron-3-super-120b-a12b:free"

python3 modules/m2-comms/comms.py --out out/w2/m2 --enriched out/w2/m1/hubspot_ready.csv
python3 modules/m2-comms/comms.py --out out/w2/m2-dry --live-dry-run   # prints the 4 prompts, no network
python3 modules/m2-comms/comms.py --out out/w2/m2-replay --offline     # replays sample_output/
python3 modules/m2-comms/test_plan_schema.py out/w2/m2                 # schema + grounding gate
```

A successful live run refreshes `sample_output/extraction.json`, `variants.json` and
`.fingerprint.json`, so a reviewer without keys can replay exactly what the model produced.
`--offline` refuses — with the sha mismatch printed — if that cache came from a different
transcript or event.

## Receipts

`receipts/m2_llm_calls.json` logs every HTTP attempt: `ts`, `backend`, `model`, `purpose`,
`prompt_chars`, `latency_ms`, `http_status`, `parse_ok`, `error`. The committed run
(`out/receipts/m2-live/m2_llm_calls.json`) ran all four calls on `google/gemini-2.5-flash-lite`,
a paid model, not the free chain the command above exports — roughly half a US cent for the run. It is written even when the run
fails, so the cost of a failed run is visible. `grounding.json` lists every check and its verdict;
`comms.json` carries the lane, model, call count, snapshot and the suppressed-contact trail.
