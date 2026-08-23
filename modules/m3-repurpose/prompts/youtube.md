# Prompt: YouTube Package

## Inputs

- `{{TRANSCRIPT}}` — full timestamped webinar transcript.
- `{{EVENT_JSON}}` — event metadata (title, date, host, speakers, duration).
- `{{EXTRACTION}}` — JSON from the extraction pass (key_moments, quotes, data_points).

## Task

Produce a YouTube upload package for the recorded webinar: chapter markers,
a description, and a thumbnail brief for the design/image-gen step downstream.

## Output contract

Markdown with exactly these three `##` sections, in this order:

### `## Chapters`
- A flat list, one per line, `MM:SS Chapter Title`.
- First entry **must** be `00:00`.
- 10–16 chapters total — more granular than the transcript's own segment
  headers; use `{{EXTRACTION}}.key_moments` timestamps to find natural
  sub-breaks inside each segment, not just the four top-level segments.
- Every timestamp must actually appear in `{{TRANSCRIPT}}`. Do not invent
  or round timestamps.
- Titles are short (under 8 words), specific to what's said at that mark —
  not generic ("Discussion continues").

### `## Description`
- 130–170 words.
- Names the event, both guest speakers and their companies, and the host.
- States 2–3 concrete takeaways a viewer gets from watching, each traceable
  to a real data point or quote.
- Ends with a one-line topic/keyword list and the recording date.

### `## Thumbnail Brief`
- **Composition**: who/what is in frame, framing, logo placement.
- **Text overlay**: exact headline text (under 6 words) plus any secondary
  stat callout text, pulled from a real number in `{{EXTRACTION}}`.
- **Colors**: a specific palette (hex or named), tied to the host's brand
  where `{{EVENT_JSON}}` gives enough to infer one, otherwise a clean B2B
  default (navy + one accent).
- This brief is a spec for a designer or an image-generation tool — be
  concrete enough that no follow-up question is needed.
