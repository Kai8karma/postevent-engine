# Prompt: YouTube Package

## Inputs

- `EVENT_JSON` — event metadata (title, date, host, speakers, duration).

{{EVENT_JSON}}
- `CHAPTERS` — the recording's three real chapter files and their absolute
  start/end marks. The first marker of each chapter is a real chapter boundary:

{{CHAPTERS}}

- `VALID_TIMESTAMPS` — the ONLY timestamps that exist. Every chapter marker must
  be copied from this list, exactly:

{{VALID_TIMESTAMPS}}

- `EXTRACTION` — verified moments, insights, quotes and data points:

{{EXTRACTION}}

- `TRANSCRIPT` — the full webinar transcript.

{{TRANSCRIPT}}

## Task

Produce the YouTube upload package: chapter markers, description, thumbnail brief.

## Output contract

Markdown with exactly these three `##` sections, in this order.

### `## Chapters`
- A flat list, one per line, `MM:SS Chapter Title`. No bullets, no bold.
- First entry **must** be `00:00`.
- Include one marker at each real chapter-file boundary from `CHAPTERS`, then
  add markers for topic shifts inside each chapter, using `EXTRACTION.moments`
  and `.insights` timestamps to find them. 8–14 markers total, at least 3.
- Every timestamp must be copied from `VALID_TIMESTAMPS`. Do not invent, round,
  or interpolate — an unlisted timestamp fails an automated check.
- Titles under 8 words, specific to what is said at that mark.

### `## Description`
- 130–170 words.
- Names the event, both speakers with their titles and companies, and the host.
- States 2–3 concrete takeaways, each traceable to a real data point or insight.
- Ends with a one-line keyword list and the recording date.
- Use no quotation marks in this section unless the string inside them is copied
  verbatim from `EXTRACTION.quotes` — quoted text is checked against the transcript.

### `## Thumbnail Brief`
- **Composition**: who/what is in frame, framing, logo placement.
- **Text overlay**: exact headline text (under 6 words) plus one secondary stat
  callout, using a real number from `EXTRACTION.data_points`.
- **Colors**: a specific palette (hex or named) tied to the host's brand.
- Concrete enough for a designer or an image model to execute without follow-up.
