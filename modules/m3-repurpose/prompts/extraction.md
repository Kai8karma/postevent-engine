# Prompt: Extraction Pass

You are stage 1 of a content-repurposing pipeline for a recorded webinar. Read the
full transcript and pull out the raw material every downstream channel needs. Do
not write marketing copy here — this is structured extraction only.

## Inputs

- `EVENT_JSON` — event metadata (title, date, host, speakers, recording files).

{{EVENT_JSON}}
- `CHAPTERS` — the recording is three separate video files. Timestamps in the
  transcript are absolute across all three; this table says which chapter each
  absolute timestamp falls in:

{{CHAPTERS}}

- `VALID_TIMESTAMPS` — the ONLY timestamps that exist. Every timestamp you
  emit must be copied from this list, exactly:

{{VALID_TIMESTAMPS}}

- `TRANSCRIPT` — the full transcript, one turn per line, `[MM:SS] **Speaker:** text`.

{{TRANSCRIPT}}

## Task

Extract four things:

1. **Moments** — self-contained stretches of the conversation that would stand
   alone as a 30–60 second video clip: a concrete story, a decision, a warning, a
   sharp answer. Give each a `start` and an `end` from the valid-timestamp list,
   with `end` 30–60 seconds after `start` and both inside the SAME chapter (never
   straddle a chapter boundary). Score each with `clip_worthiness` 0–1: 1.0 = a
   complete, quotable thought that needs no setup; 0.3 = interesting but only in
   context.
2. **Insights** — what a viewer should actually take away, in your own words, each
   anchored to the timestamp where it is argued, with a one-line `so_what`.
3. **Quotes** — verbatim lines worth reusing as pull-quotes or quote cards.
   Copy a CONTIGUOUS span character-for-character from a single transcript line,
   including filler words ("um", "you know", repeats) if they fall inside the span.
   You may start and end mid-sentence to find a clean span, but you may not change,
   reorder, delete, or "tidy" a single word inside it, and you may not use "..." to
   join two pieces. A quote that is not a literal substring of the transcript is
   dropped by an automated checker and wasted.
4. **Data points** — every number, count, duration, percentage or multiplier that is
   actually said, with enough context to stand alone.

## Output contract

Return **only** a single JSON object, no prose before or after:

```json
{
  "moments": [
    {"start": "MM:SS", "end": "MM:SS", "title": "short label", "speaker": "Name",
     "why": "one sentence on why this stands alone", "clip_worthiness": 0.0}
  ],
  "insights": [
    {"insight": "one sentence in your own words", "speaker": "Name",
     "timestamp": "MM:SS", "so_what": "why a practitioner should care"}
  ],
  "quotes": [
    {"quote": "verbatim contiguous span", "speaker": "Name", "timestamp": "MM:SS", "topic": "short label"}
  ],
  "data_points": [
    {"value": "1,800 employees", "what": "one line on what it measures",
     "speaker": "Name", "timestamp": "MM:SS"}
  ]
}
```

Requirements:
- At least 6 moments (spread across all three chapters), 5 insights, 8 quotes, 6 data points.
- Every `speaker` must be one of the names in `EVENT_JSON.speakers`.
- Every timestamp must be copied from the valid-timestamp list above.
- Never invent a number, a quote, or an attribution. If the webinar only produced a
  handful of numbers, use the ones it produced — do not pad with estimates.
