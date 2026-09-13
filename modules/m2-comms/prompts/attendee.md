# Prompt: attendee thank-you (one call per event, not per contact)

Write the thank-you email for people who attended live. The context block carries the event facts
and the extraction from call 1 — the takeaways, verbatim quotes and premise. You do not have the
transcript; everything you claim must come from that extraction.

The output is a template. `comms.py` renders it once per contact by substituting the merge fields,
so per-recipient copy lives in the merge fields, not in your prose.

## Merge fields — copy these tokens verbatim into `body_md`

- `{{firstname}}` — recipient's first name.
- `{{takeaway_headline}}` and `{{takeaway_body}}` — required, exactly once each. These are resolved
  per contact from their CRM `function` and industry bucket. Write the sentence around them so they
  read as the personalised takeaway: `**{{takeaway_headline}}** {{takeaway_body}}`. Do not write
  your own text inside them and do not repeat that takeaway elsewhere in the body.
- `{{recording_link}}` — the recording URL, already UTM-tagged. Use it as a markdown link target.
- `{{cta_link}}` — the call-to-action URL, already UTM-tagged.

No other `{{...}}` token is fillable; using one fails the run.

## Rules

- Under 250 words of markdown. Short paragraphs, one bulleted block of 2-3 takeaways, one clear CTA.
- Every `[MM:SS]` and every quoted span must be copied exactly from the extraction — a checker
  matches both against the transcript. Use double quotes only for a verbatim quote from `quotes[]`.
- Attribute a quote to the speaker who said it, with its `[MM:SS]`.
- No invented statistics, no attendance numbers, no promises about future events.
- Do not sign off with an invented person's name; close as the host company's team.
- Subject lines: `subject_a` leads with the substance of the session, `subject_b` leads with a
  speaker or a quote. Both under 60 characters, different from each other, no emoji, no "RE:".
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
