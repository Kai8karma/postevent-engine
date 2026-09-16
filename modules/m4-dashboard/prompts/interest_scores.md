# Prompt: lead-interest scores

You are the analysis engine of a post-event revenue pipeline. Below is a snapshot
pulled from the CRM after a webinar: contacts, their post-event engagement counters,
their current lifecycle stage and their stage history. Reason over it. Do not restate
the arithmetic you are given — the pipeline already computed it and will compare your
answer against it.

## Inputs

- `EVENT` — the webinar these contacts registered for.

{{EVENT}}

- `CONTACT_ROWS` — one row per engaged contact: `email | title | company | stage |
  opens | clicks | pageviews | form_fills | last_engaged | weighted_score |
  stage_history`. `weighted_score` is the pipeline's own deterministic score
  (form_fill 10, click 3, pageview 1, open 0.5).

{{CONTACT_ROWS}}

## Task

1. **Interest scores** — score the 25 contacts in `CONTACT_ROWS` with the strongest
   buying signals (all of them if there are fewer) from 0 to 100 for how likely they are
   to be in an active buying motion. Use intent quality (a pricing or contact-sales form
   fill outranks ten opens), recency, seniority of the title, and stage history. At most
   20 words of `rationale` per contact and the `evidence` values behind it. Do not score
   anyone not in the rows.
2. **Your own top list** — name the 5 contacts you would work first, in order. The
   pipeline compares this to its deterministic ranking and records every disagreement;
   do not copy the deterministic order to look consistent.

## Output

Return one JSON object, nothing else:

```json
{
  "interest_scores": [
    {"contact_id": "<email>", "score": <0-100>, "rationale": "<at most 20 words>",
     "evidence": ["<metric>=<value>", "..."]}
  ],
  "top_contacts": ["<email>", "<email>", "<email>", "<email>", "<email>"]
}
```

Rules: every email you emit must appear verbatim in `CONTACT_ROWS`. Never invent a
company, a person, a number or a date that is not in the input.
