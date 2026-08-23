# Prompt: Blog Draft

## Inputs

- `{{TRANSCRIPT}}` — full timestamped webinar transcript.
- `{{EVENT_JSON}}` — event metadata (title, date, host, speakers, recording_url).
- `{{EXTRACTION}}` — JSON from the extraction pass (key_moments, quotes, data_points).

## Task

Write a publish-ready blog post recapping the webinar for the host company's
own blog audience (marketing/RevOps practitioners). Ground every claim in the
transcript and extraction data — do not invent statistics, quotes, or
speaker attributions.

## Output contract

- Markdown, **800–1200 words** (title and headers count toward the total).
- One `#` H1 title — specific and stat-forward, not generic ("Webinar Recap").
- One short italic byline line naming the event, host, date, and speakers.
- 4–6 `##` H2 sections following the session's actual arc (do not invent
  structure not supported by the transcript).
- **Exactly 3 pull-quotes**, each a markdown blockquote (`>`), each with a
  verbatim quote pulled from `{{EXTRACTION}}.quotes`, followed on the next
  line by `— Speaker Name, Title, Company`. Prefer covering 3 different
  speakers if the extraction data supports it.
- At least 3 concrete data points from `{{EXTRACTION}}.data_points` woven
  into prose (not just left in the pull-quotes).
- Close with a short CTA paragraph pointing to the full recording, using
  `event.recording_url` from `{{EVENT_JSON}}` as the link target.
- No invented case studies, no filler transitions ("In today's fast-paced
  world..."). Every sentence should trace to something actually said.
