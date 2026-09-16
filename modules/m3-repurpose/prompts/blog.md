# Prompt: Blog Draft

## Inputs

- `EVENT_JSON` — event metadata (title, date, host, speakers, recording_url).

{{EVENT_JSON}}
- `EXTRACTION` — verified extraction: moments, insights, quotes (already checked
  verbatim against the transcript), data points.

{{EXTRACTION}}

- `TRANSCRIPT` — the full webinar transcript.

{{TRANSCRIPT}}

## Task

Write a publish-ready recap for the host company's own blog audience (HR and people
leaders). Ground every claim in the transcript and the extraction — invent nothing.

## Output contract

- Markdown, **800–1200 words** (title and headers count). This is a hard gate: a
  draft outside that range is rejected automatically. Aim for ~1,000 words.
- One `#` H1 title — specific and concrete, not "Webinar Recap".
- One short italic byline naming the event, host, date and both speakers.
- 4–6 `##` H2 sections following the session's actual arc.
- **Exactly 3 pull-quotes**, each a markdown blockquote (`>`) containing a quote
  copied character-for-character from `EXTRACTION.quotes`, followed on the next
  line by `— Speaker Name, Title, Company` (title and company from `EVENT_JSON`).
- At least 3 data points from `EXTRACTION.data_points` woven into the prose
  (not only inside the pull-quotes).
- Close with a short CTA paragraph linking the full recording using
  `event.recording_url` from `EVENT_JSON` as the link target.
- No invented case studies, no "in today's fast-paced world" filler.

## Quotation-mark rule (enforced by an automated grounding check)

Copy a quote character-for-character from `EXTRACTION.quotes` and stop where that entry stops -- do not extend it with the words that came before or after it in the transcript, do not merge two entries, do not tidy the wording. Quotation marks are read as a claim that someone said those exact words. Use them
ONLY around a verbatim string from `EXTRACTION.quotes`. Write every paraphrase,
rhetorical aside, or hypothetical in plain prose or italics — never in quotes.
