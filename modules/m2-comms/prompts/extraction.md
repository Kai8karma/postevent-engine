# Prompt: transcript extraction (call 1 of 4)

You are reading the full transcript of a real webinar. Extract the raw material every follow-up
email in this campaign is built from. This is the ONLY call that sees the transcript — the three
copy calls after it see your JSON and nothing else, so anything missing here cannot be recovered.

## Hard rules (a run that breaks one of these is rejected automatically)

1. **Timestamps are copied, never composed.** Every `timestamp` and every `[MM:SS]` you write must
   appear verbatim in the transcript, attached to the turn you are citing.
2. **Quotes are verbatim.** `quotes[].text` must be a contiguous span of words copied exactly from
   one transcript turn — no tidying, no ellipsis, no stitching two turns together. Trim filler at
   the edges by starting and ending the span later/earlier, not by editing the middle.
3. **Use double quotes only for verbatim transcript text.** Never for emphasis, labels or
   paraphrase — a checker matches every quoted span against the transcript and fails the run.
4. **No invented numbers.** Attendance, headcount, ROI and adoption figures must be said on the
   call. If a claim has no number, describe it without one.
5. Speaker names in `quotes[].speaker` must match the transcript's speaker labels exactly.
6. **Every named speaker needs at least one quote** — two where the transcript allows. Each speaker
   receives a thank-you built from their own lines, so an extraction that quotes only the person who
   talked most is rejected. The host asks questions and frames the session: quote that too.

## Output — one JSON object, nothing else

```json
{
  "premise": "one line: what this session was actually about, in the host's frame, no adjectives",
  "takeaways": [
    {"headline": "6-10 words, the point itself", "body": "1-2 sentences of substance, name the speaker", "timestamp": "MM:SS"}
  ],
  "quotes": [
    {"speaker": "exact name", "timestamp": "MM:SS", "text": "verbatim span"}
  ],
  "no_show_moments": [
    {"timestamp": "MM:SS", "label": "3-6 words", "why": "one sentence: why someone who missed the session should jump to this minute"}
  ],
  "by_function": {
    "marketing": {"headline": "...", "body": "..."},
    "revops": {"headline": "...", "body": "..."},
    "sales": {"headline": "...", "body": "..."},
    "executive": {"headline": "...", "body": "..."},
    "customer_success": {"headline": "...", "body": "..."},
    "general": {"headline": "...", "body": "..."}
  },
  "by_industry": {"saas": "one sentence", "services_it": "one sentence", "other_commercial": "one sentence"}
}
```

- `takeaways`: exactly 5 to 7, each anchored to a different moment of the session.
- `quotes`: exactly 4 to 6, spread across the speakers listed in the context (the speaker email is
  built from that speaker's own quotes, so give each speaker at least two).
- `no_show_moments`: exactly 3 — the minutes worth the click for someone who registered and missed.
- `by_function`: the six labels are the CRM's `function` values for this audience (context shows how
  many contacts carry each). Write the same session's material from that role's point of view —
  what they would action on Monday — in one headline plus 1-2 sentences, with an `[MM:SS]` anchor
  in the body. `general` is the fallback every unmatched contact gets, so make it the strongest one.
- `by_industry`: one sentence per bucket, written to read naturally as a follow-on sentence appended
  after the role takeaway. Include an `[MM:SS]` anchor.

Return the JSON object only. No preamble, no markdown fence, no commentary after it.
