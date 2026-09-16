# Prompt: narrated lifecycle movement (7 / 14 / 30 days)

You are writing the analyst note that sits on top of a post-event revenue dashboard.
Its readers are the demand-gen lead and the SDR pod that owns this event. Narrate what
the pipeline *did* across the three windows below — movement, not a metrics dump.

## Inputs

- `EVENT` — the webinar.

{{EVENT}}

- `WINDOWS` — stage transitions counted from each contact's stage history inside each
  window, ending at `as_of`. `source_mix` says where each transition came from:
  `seeded` = an engagement stream this pipeline wrote into the CRM for a synthetic
  registrant list; `hubspot_history` = the CRM's own property history.

{{WINDOWS}}

- `TRANSITIONS` — the individual moves inside the 30-day window:
  `email | company | from_stage -> to_stage | ts | source`.

{{TRANSITIONS}}

- `FUNNEL` — attendees, engaged contacts, and how many attendees are now MQL or later.

{{FUNNEL}}

## Task

Write 2–4 short paragraphs (plain text, no markdown headings, no bullet lists) that:

1. Say what moved in the last 7 days and how that compares with the 14- and 30-day
   windows — acceleration, plateau or stall, and which stage the movement concentrated in.
2. Name the accounts and stages actually carrying the movement, using the rows above.
3. Say plainly what has *not* moved: contacts with engagement whose stage never changed.
4. State the source mix in one clause where it matters, so nobody mistakes seeded
   movement for organic movement.

Then state the counts you narrated, so the pipeline can check them against its own
arithmetic and flag any disagreement.

## Output

Return one JSON object, nothing else:

```json
{
  "narrative": "<the 2-4 paragraphs, \n\n between them>",
  "counts": {"7d": <transitions you narrated>, "14d": <…>, "30d": <…>},
  "stalled_contacts": ["<email>", "..."]
}
```

Rules: every number in `narrative` must be one you can point to in the input. Every
email and company name must appear verbatim in the input. Never invent a customer,
a deal, a revenue figure or a date.
