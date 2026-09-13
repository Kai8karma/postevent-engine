# Prompt: speaker thank-you (one call per event, not per speaker)

Write the thank-you email that goes to each person who spoke. The context block carries the event
facts, the extraction from call 1 (including `quotes_by_speaker`), and `performance_snapshot` —
real numbers computed from the CRM, not by you. One template serves every speaker, so it must never
name a speaker: the recipient is always "you".

## Merge fields — copy these tokens verbatim into `body_md`

- `{{firstname}}` — the recipient speaker's first name.
- `{{snapshot_registrants}}`, `{{snapshot_attendees}}`, `{{snapshot_attendance_rate}}`,
  `{{snapshot_avg_minutes}}`, `{{snapshot_median_minutes}}`, `{{snapshot_no_shows}}`,
  `{{snapshot_top_accounts}}` — the performance snapshot. Use at least four of them, each exactly
  once, in a short labelled list. Never write the numbers yourself; the context values are there so
  you can phrase the labels, not so you can inline them.
- `{{snapshot_your_quotes}}` — required. Renders as a blockquote list of that speaker's own quotes
  with timestamps. Put it on its own line and introduce it in the second person ("the lines people
  are already pulling out of your session").
- `{{recording_link}}`, `{{cta_link}}` — already UTM-tagged.

No other `{{...}}` token is fillable; using one fails the run.

## Rules

- Under 250 words. Thank, report, hand over the assets, ask for the reshare. No flattery inflation.
- **Never name the recipient or attribute anything to them in the third person** — no "Q made the
  point", no "Sudi walked us through". Everything they did is "you". This is enforced by a lint.
- Do not inline any quote text: their quotes arrive through `{{snapshot_your_quotes}}`.
- Any `[MM:SS]` you write must be copied exactly from the extraction. Use double quotes only for a
  verbatim quote from `quotes[]`.
- Do not invent attendance, watch-time or pipeline numbers; the snapshot tokens are the only numbers
  allowed in this email.
- Subject lines: `subject_a` leads with the performance numbers, `subject_b` leads with the thank
  you. Both under 60 characters, different from each other, and neither may contain a speaker name.
- `preheader`: under 90 characters.

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
