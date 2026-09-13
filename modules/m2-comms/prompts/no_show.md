# Prompt: no-show catch-up (one call per event, not per contact)

Write the email for people who registered and did not attend. The context block carries the event
facts and the extraction from call 1 — takeaways, verbatim quotes, the premise, and
`no_show_moments`: the three minutes of the recording worth jumping to. You do not have the
transcript; everything you claim must come from that extraction.

The output is a template. `comms.py` renders it once per contact by substituting the merge fields.

## Merge fields — copy these tokens verbatim into `body_md`

- `{{firstname}}` — recipient's first name.
- `{{takeaway_headline}}` and `{{takeaway_body}}` — required, exactly once each; resolved per
  contact from their CRM `function` and industry bucket. Frame them as the one thing they missed
  that matters for their role.
- `{{recording_link}}` — the on-demand recording, already UTM-tagged.
- `{{cta_link}}` — the CTA URL, already UTM-tagged. This is the primary action in this email.

No other `{{...}}` token is fillable; using one fails the run.

## Rules

- Under 220 words. No guilt, no "we missed you" filler, no re-pitching the whole agenda.
- Lead with the fact that the recording is available, then give the three `no_show_moments` as a
  timestamped list so the recipient can jump straight in — each entry carries its `[MM:SS]`.
- Every `[MM:SS]` and every quoted span must be copied exactly from the extraction. Use double
  quotes only for a verbatim quote from `quotes[]`.
- Do not claim anything about why they missed it, and do not state how long ago the session ran —
  the send time is not known when this copy is written.
- Subject lines: `subject_a` leads with what they missed, `subject_b` leads with the recording
  being ready. Both under 60 characters, different from each other, no emoji.
- `preheader`: under 90 characters, adds information rather than repeating the subject.

## Output — one JSON object, nothing else

```json
{
  "subject_a": "...",
  "subject_b": "...",
  "preheader": "...",
  "body_md": "markdown with the merge fields in place",
  "takeaways": ["3-5 short strings, each ending with its [MM:SS] anchor"]
}
```
