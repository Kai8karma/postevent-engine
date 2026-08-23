# Prompt: No-Show Catch-Up Email

Used by `comms.py --live` to regenerate the no-show-segment email template (one call per event,
same one-call-per-segment economics as `attendee.md`).

## Personalization variables

- `{{first_name}}`, `{{company}}` — from the enriched contact record.
- `{{takeaway_headline}}` / `{{takeaway_body}}` — same takeaway-injection contract as
  `attendee.md`, keyed by `function`. Leave tokens in place for `comms.py` to fill.
- `{{recording_cta_timestamped}}`, `{{recording_link}}`, `{{unsubscribe_link}}` — wired by
  `comms.py`, UTM-tagged. Never invent a URL or timestamp not derivable from the transcript.
- `{{function_relevant_segment}}` — a short phrase naming the transcript segment most relevant to
  the recipient's function (e.g. "the segmentation numbers" for marketing, "the attribution math"
  for RevOps), used in a "reply for the short version" CTA.
- `{{event_time_since_close}}` — filled by `comms.py` from `event.json` date + send time; the model
  should reference it as proof of speed-to-lead discipline, not restate a number itself.

## Takeaway-injection contract

Same rules as `attendee.md`: one grounded, speaker-attributed, timestamped takeaway per function,
plus one grounded, timestamped follow-on sentence per industry bucket (`saas`, `services_it`,
`other_commercial` — see `attendee.md`'s `by_industry` contract, same [26:15]–[26:52] anchor). The
selection rule is unchanged across segments: role takeaway is primary, industry sentence is
appended second by `comms.py` (`resolve_takeaway()`) — write the industry sentence to read as a
follow-on, not a competing headline. No-show copy additionally must acknowledge, in the model's
tone guidance (not as a literal token), Daniel Kim's finding that no-show follow-up out-clicks
attendee follow-up in 6 of 8 recent webinars ([15:44]) — the copy should feel like a genuinely
useful digest, not an apology.

Output as JSON (same shape as `attendee.md`):
```json
{
  "by_function": { "...": {"headline": "...", "body": "..."} },
  "by_industry": { "saas": "...", "services_it": "...", "other_commercial": "..." },
  "subject_a": "...",
  "subject_b": "..."
}
```

## Guardrails

- Recording link + timestamp CTA only — no hard sales ask in the first two sentences.
- Ground every claim in the transcript; no invented stats — same fallback as `attendee.md` if a
  function or industry bucket has no distinct transcript moment.
- Draft only. Nothing here is sent without a human clearing `approval_gate.json`.
