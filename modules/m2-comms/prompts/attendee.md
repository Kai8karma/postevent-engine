# Prompt: Attendee Thank-You Email

Used by `comms.py --live` to regenerate the attendee-segment email. Invoked once per event
(not once per contact) — output is a personalized *template* with token placeholders, which
`comms.py` then fills per contact. This keeps LLM calls to one per segment regardless of list size.

## Personalization variables (the model must preserve these tokens verbatim in its output)

- `{{first_name}}`, `{{company}}` — from the enriched contact record.
- `{{takeaway_headline}}` / `{{takeaway_body}}` — filled by the takeaway-injection contract below,
  keyed by the contact's `function`. Do not write generic copy into these two tokens — leave the
  tokens in place; `comms.py` substitutes them after generation.
- `{{recording_cta}}`, `{{recording_link}}`, `{{unsubscribe_link}}` — wired by `comms.py` with UTM
  params already attached. Never invent a URL.

## Takeaway-injection contract

Input to the model: the full transcript (`data/incoming/transcript.md`) plus the list of distinct
`function` values present in this event's attendee segment (e.g. `marketing`, `revops`, `sales`,
`executive`, `customer_success`, `general`) and the list of `industry` buckets present (`saas`,
`services_it`, `other_commercial` — comms.py derives these from M1's enriched `industry` column;
see `classify_industry()`).

For each function, extract exactly ONE takeaway that:
1. Is a real number or claim actually said in the transcript — quote-attributable to a named
   speaker with a `[MM:SS]` timestamp. No invented statistics.
2. Is the takeaway most relevant to that function's day-to-day (e.g. RevOps cares about
   multi-threading/attribution; Sales cares about reply-rate lift; Marketing cares about
   segmentation lift; Executives care about ROI multiples).
3. Fits in one sentence for `{{takeaway_headline}}` (bolded lead-in) and 1-2 sentences for
   `{{takeaway_body}}` (the supporting detail, with speaker name and timestamp).

Then, for each industry bucket, write exactly ONE additional sentence (`by_industry.<bucket>`) —
grounded the same way (transcript-attributable, `[MM:SS]` timestamp, no invented stats). Anchor
point: Daniel Kim's fourth-layer point at [26:15]–[26:52] — "the takeaways we pull out of the same
transcript literally change based on who's reading them" — applied per industry rather than per
company size. This sentence is never sent standalone: `comms.py`'s selection rule is role takeaway
primary, industry sentence appended second (see `resolve_takeaway()`), so write it to read naturally
as a follow-on sentence, not a second headline.

Output as JSON:
```json
{
  "by_function": {
    "marketing": {"headline": "...", "body": "..."},
    "revops": {"headline": "...", "body": "..."},
    "sales": {"headline": "...", "body": "..."},
    "executive": {"headline": "...", "body": "..."},
    "customer_success": {"headline": "...", "body": "..."},
    "general": {"headline": "...", "body": "..."}
  },
  "by_industry": {
    "saas": "one grounded, timestamped follow-on sentence",
    "services_it": "one grounded, timestamped follow-on sentence",
    "other_commercial": "one grounded, timestamped follow-on sentence"
  },
  "subject_a": "stat-led subject line (Daniel Kim's finding: stat-led subjects beat name-led by ~14pp open rate, see [18:15])",
  "subject_b": "speaker-name-led subject line, as the A/B counterpart"
}
```

## Guardrails

- Ground every claim in the transcript. If a function has no clearly relevant moment, fall back to
  the segmentation-lift stat (Sara Alvarez, [28:20]) rather than inventing one.
- Same rule for `by_industry`: if a bucket has no distinct transcript moment, fall back to the
  [26:15]–[26:52] fourth-layer point rather than inventing an industry-specific stat.
- Never fabricate attendance numbers, company names, or quotes not present in the transcript.
- Output must stay inside the approval gate — this prompt produces a draft for human review, not a
  send.
