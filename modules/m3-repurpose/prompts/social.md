# Prompt: Social Post Package

## Inputs

- `{{TRANSCRIPT}}` — full timestamped webinar transcript.
- `{{EVENT_JSON}}` — event metadata (title, date, host, speakers, recording_url).
- `{{EXTRACTION}}` — JSON from the extraction pass (key_moments, quotes, data_points).

## Task

Produce 8 social posts repurposing this webinar: 5 for LinkedIn, 3 for X.
Each post must use a **different hook style** — no two posts should read
like the same sentence chopped up differently.

## Output contract

Markdown, one `###` block per post, 8 blocks total, each containing these
fields in order:

- `Platform:` LinkedIn or X
- `Hook style:` one of exactly these 8, each used **exactly once** across
  the set: `contrarian stat`, `story`, `listicle`, `quote card`, `question`,
  `hot take`, `data viz callout`, `speaker spotlight`
- `Segment:` which part of the transcript this draws from (e.g. "Segment 1
  — Speed-to-Lead [08:10–24:00]" or a specific timestamp range) — must be
  traceable to real transcript content, not invented
- `Post:` the actual post copy (LinkedIn: 3–6 short paragraphs or a tight
  list; X: under 280 characters), ending with a placeholder link token
  `[link]` pointing at the recording

Requirements:
- Distribution must be exactly 5 LinkedIn + 3 X.
- Every stat, quote, or claim used in a post must come from `{{EXTRACTION}}`
  or `{{TRANSCRIPT}}` — never invented.
- The `quote card` post must use a verbatim quote with correct speaker
  attribution.
- The `data viz callout` post must reference a specific before/after or
  comparison number (not a single flat stat).
- Vary which speaker each post centers — do not center the same speaker in
  more than 3 of the 8 posts.
