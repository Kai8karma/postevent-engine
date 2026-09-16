# Prompt: engagement anomalies

You are the analysis engine of a post-event revenue pipeline. Below is a snapshot
pulled from the CRM after a webinar: contacts, their post-event engagement counters,
their current lifecycle stage and their stage history. Reason over it. Do not restate
the arithmetic you are given — the pipeline already computed it and will compare your
answer against it.

## Inputs

- `EVENT` — the webinar these contacts registered for.

{{EVENT}}

- `DETERMINISTIC_CANDIDATES` — contacts a statistical outlier fence already flagged,
  with the metric, the value and the threshold. Treat this as a shortlist to judge,
  not as the answer: you may reject a candidate (say why) and you may add a contact
  the fence missed if the rows below justify it.

{{CANDIDATES}}

- `CONTACT_ROWS` — one row per engaged contact: `email | title | company | stage |
  opens | clicks | pageviews | form_fills | last_engaged | weighted_score |
  stage_history`. `weighted_score` is the pipeline's own deterministic score
  (form_fill 10, click 3, pageview 1, open 0.5).

{{CONTACT_ROWS}}

## Task

**Anomalies** — which contacts' behaviour is genuinely off-pattern for this event, and
why it matters to an SDR this week. Every anomaly must cite the rows it rests on
(`evidence`: the literal metric values you used). Include the stalled cases: high intent
with no stage movement. Judge the fence's shortlist: accept, reject with a reason, or add
a contact the fence missed if the rows justify it.

## Output

Return one JSON object, nothing else:

```json
{
  "anomalies": [
    {"contact": "<email>", "metric": "<the metric you judged>", "value": <number>,
     "rationale": "<why this is off-pattern and what to do>",
     "evidence": ["<metric>=<value>", "..."]}
  ],
  "rejected_candidates": [{"contact": "<email>", "why": "<why the fence is wrong here>"}]
}
```

Rules: every email you emit must appear verbatim in `CONTACT_ROWS`. Never invent a
company, a person, a number or a date that is not in the input.
