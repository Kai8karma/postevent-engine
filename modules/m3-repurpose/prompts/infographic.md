# Prompt: Infographic Outline

## Inputs

- `EVENT_JSON` — event metadata (title, date, host, speakers).

{{EVENT_JSON}}
- `EXTRACTION` — verified moments, insights, quotes and data points:

{{EXTRACTION}}

- `TRANSCRIPT` — the full webinar transcript.

{{TRANSCRIPT}}

## Task

Produce a design-ready outline for a single-page infographic built entirely around
the numbers and claims this specific webinar produced. This is a spec for a designer
or an image model, not the finished graphic.

## Output contract

Markdown with exactly these three `##` sections, in this order.

### `## Headline Options`
- 3 candidate headlines, each under 12 words, each referencing the actual theme or
  numbers rather than being generic.

### `## Data Points`
- **6–8** data points drawn from `EXTRACTION.data_points` (and, where a number
  is thin, an `insights` entry expressed as a concrete count or duration).
- Each entry on its own line, in exactly this format:
  `**<number or short stat>** — <one line on what it measures> *<Speaker, Company [MM:SS]>*`
- Every number, speaker and timestamp must trace to `EXTRACTION`. Do not invent,
  re-round, or merge two stats into one.
- If you reference a point by ordinal in `## Layout`, use "Stat N" where N is within
  the number of points you actually listed here.

### `## Layout`
- Top-to-bottom (or panel-by-panel) visual flow: the hero element, what groups with
  what, which chart type fits each stat (bar, before/after pair, icon stat,
  timeline), and where the event branding and CTA sit.
- Name a 2–3 colour system so all the stats read as one family.
- Use no quotation marks around anything that is not a verbatim
  `EXTRACTION.quotes` string.
