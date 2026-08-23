# Prompt: Speaker Thank-You Email

Used by `comms.py --live` to regenerate the speaker-segment email template. Speakers are named
individuals (`data/incoming/speakers.json`, `event.json`), not a bulk list — this prompt is invoked
once per event, then `comms.py` fills the same template for each of the (typically 2-4) speakers.

## Personalization variables

- `{{first_name}}`, `{{title}}`, `{{company}}` — from `event.json` speakers list.
- `{{attendance_count}}`, `{{registered_count}}`, `{{avg_watch_minutes}}`, `{{duration_min}}`,
  `{{watch_pct}}` — computed by `comms.py` from `data/fixtures/segments.json` and
  `data/incoming/registrants.csv`. The model must NOT invent these; leave the tokens in place.
- `{{top_quote}}` — one verbatim, timestamp-attributable quote said BY this specific speaker in the
  transcript, chosen for being the most quotable/resharable line they said (per Sara Alvarez's
  advice at [19:06] that a speaker thank-you should include "a top quote of theirs that resonated").
- `{{internal_or_external_note}}` — one sentence whose TONE differs by whether the speaker is
  internal (host company) or external, per the segmentation rule stated live on the call at
  [30:52]: internal speakers get a recognition/metrics-loop note; external speakers get a
  relationship-maintenance note (invite-back, resharing ask).

## Takeaway-injection contract

For `{{top_quote}}`: search the transcript for lines spoken by the named speaker, rank by how
standalone/shareable the line is (a claim or observation that reads well with zero surrounding
context — per Daniel Kim's point at [43:22] that standalone quote posts outperform), and return
exactly one with its `[MM:SS]` timestamp for auditability. Do not paraphrase — quote verbatim.

Output as JSON:
```json
{
  "by_speaker": {
    "<speaker name>": {"top_quote": "...", "timestamp": "MM:SS", "internal_or_external_note": "..."}
  },
  "subject_a": "performance-numbers-led subject line",
  "subject_b": "personal-thank-you-led subject line"
}
```

## Guardrails

- `{{top_quote}}` must be traceable to an exact transcript line. If no strong standalone line
  exists for a speaker, say so rather than inventing one — fall back to their highest-signal
  numeric claim instead.
- Never fabricate attendance or watch-time figures; those are computed, not generated.
- Draft only, subject to human approval before send.
