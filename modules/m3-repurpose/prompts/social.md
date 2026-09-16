# Prompt: Social Post Package

## Inputs

- `EVENT_JSON` — event metadata (title, date, host, speakers, recording_url).

{{EVENT_JSON}}
- `EXTRACTION` — verified moments, insights, quotes and data points:

{{EXTRACTION}}

- `TRANSCRIPT` — the full webinar transcript.

{{TRANSCRIPT}}

## Task

Write **8 social posts** repurposing this webinar: 5 for LinkedIn, 3 for X. Every
post uses a different hook — no two may read like the same sentence rearranged.

## Output contract

Markdown, one `###` block per post, 8 blocks. The heading must be exactly
`### Post <N>` (`### Post 1` … `### Post 8`) — an automated parser matches that
literal text, so folding the platform into the heading reads as zero posts.

Each block then carries these fields, each on its own line, in this order:

- `**Platform:** LinkedIn` or `**Platform:** X`
- `**Hook style:** <one of>` `contrarian stat`, `story`, `listicle`, `quote card`,
  `question`, `hot take`, `data viz callout`, `speaker spotlight` — each used
  **exactly once** across the 8 posts.
- `**Segment:** Chapter <n> at [MM:SS]` — one timestamp copied from `EXTRACTION` (the moment or quote this post draws on).
- `**Post:**` then the copy. LinkedIn: 3–6 short paragraphs or a tight list. X: under
  280 characters. End the copy with the literal token `[link]` (the pipeline swaps it
  for a tracked recording link).

Requirements:
- Exactly 5 LinkedIn + 3 X, and every post carries its `**Platform:**` line.
- Every stat, quote or claim must come from `EXTRACTION` or `TRANSCRIPT`.
- The `quote card` post uses a verbatim `EXTRACTION.quotes` string with the right
  speaker.
- The `data viz callout` post references a comparison or before/after number, not a
  single flat stat.
- Do not centre the same speaker in more than 4 posts.

## Quotation-mark rule (enforced by an automated grounding check)

Copy a quote character-for-character from `EXTRACTION.quotes` and stop where that entry stops -- do not extend it with the words that came before or after it in the transcript, do not merge two entries, do not tidy the wording. Quotation marks are a claim that someone said those exact words. Use them only
around a verbatim `EXTRACTION.quotes` string, and name the speaker next to it.
Write hooks, paraphrases and rhetorical lines in plain prose without quotes.
