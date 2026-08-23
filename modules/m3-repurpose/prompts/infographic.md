# Prompt: Infographic Outline

## Inputs

- `{{TRANSCRIPT}}` — full timestamped webinar transcript.
- `{{EVENT_JSON}}` — event metadata (title, date, host).
- `{{EXTRACTION}}` — JSON from the extraction pass (key_moments, quotes, data_points).

## Task

Produce a design-ready outline for a single-page infographic built entirely
around the numbers this specific webinar produced. This is a spec for a
designer (or downstream image-generation step), not the finished graphic.

## Output contract

Markdown with exactly these three `##` sections, in this order:

### `## Headline Options`
- 3 candidate headlines for the infographic, each under 12 words, each
  referencing the numbers/theme rather than being generic.

### `## Data Points`
- **Exactly 6** data points, selected from `{{EXTRACTION}}.data_points`.
- Prefer the 6 most surprising or citable — favor specific multipliers and
  percentages over vague claims, and favor spread across speakers over
  repeating one speaker's stats.
- Each entry format: `**<number>** — <one-line what-it-measures> *<Speaker, Company [MM:SS]>*`
- Every number and attribution must trace to `{{EXTRACTION}}` — do not
  invent, round beyond what was said, or merge two different stats into one.

### `## Layout`
- A top-to-bottom (or panel-by-panel) description of visual flow: what's the
  hero element, what's grouped together, what chart type (if any) fits each
  data point (bar, before/after pair, icon stat, timeline), and where the
  event branding and CTA sit.
- Name a simple color system (2–3 colors max) so all 6 stats read as one
  family rather than six unrelated graphics.
