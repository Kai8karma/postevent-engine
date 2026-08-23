# Prompt: Missing-Field Inference (title / function / seniority / industry / company_size)

Used by `enrich.py --live` as a second opinion on rows where the deterministic
rule tables (company-peer majority vote, domain lookup, keyword classifiers)
could not resolve a field with confidence. Called **once per batch of
ambiguous rows**, not once per contact -- a 150-row registrant list should
cost one LLM call, not 150.

## When this prompt fires

Only for rows still missing `jobtitle` and/or `company` after the rule-based
cascade (sibling-duplicate backfill -> company-mode-title / domain-to-company
lookup) has run, and for rows whose `industry` classified as `"Other"` where
a qualitative read of the company name might place it more precisely.
Everything else stays on the deterministic path -- cheaper, reproducible,
and auditable in `icp_rationale`.

## Input contract

```json
{
  "event_context": {
    "host_company": "ACME Revenue Cloud",
    "topic": "Post-event follow-up strategy: speed-to-lead, segmentation, content repurposing ROI, and attribution for B2B revenue teams"
  },
  "rows": [
    {
      "row_id": "anton.chen@gmail.com",
      "firstname": "Anton",
      "lastname": "Chen",
      "email": "anton.chen@gmail.com",
      "company": "",
      "jobtitle": "Marketing Intern",
      "country": "FR"
    }
  ]
}
```

## Output contract

```json
{
  "rows": [
    {
      "row_id": "anton.chen@gmail.com",
      "company": {"value": "Unknown", "confidence": 0.2, "rationale": "freemail address, no company signal in registrant or peer data"},
      "jobtitle": {"value": "Marketing Intern", "confidence": 1.0, "rationale": "already present, not inferred"},
      "industry": {"value": "Unknown", "confidence": 0.2, "rationale": "no resolvable company"},
      "function": {"value": "marketing", "confidence": 0.8, "rationale": "title contains 'Marketing'"},
      "seniority": {"value": "intern", "confidence": 0.9, "rationale": "title contains 'Intern'"},
      "company_size": {"value": null, "confidence": 0.0, "rationale": "cannot size an unknown company -- leave for Clay firmographic lookup in production"}
    }
  ]
}
```

## Guardrails

- Never invent a company name that isn't grounded in the email domain, a
  peer registrant's company field, or the event context. `"Unknown"` is a
  valid, honest answer -- it is not a failure.
- `company_size` must be a number the model can defend from real signal
  (e.g. "this domain's registrant density in the batch suggests a small
  team") or `null`. Do not guess a number just to fill the field -- that is
  `enrich.py`'s job in offline mode (`synthetic_company_size`, clearly
  labeled as a synthetic placeholder), not this prompt's.
- `function` and `seniority` must come only from the classifier categories
  already defined in `enrich.py` (`marketing`, `revops`, `sales`, `executive`,
  `customer_success`, `general` / `c_suite`, `vp`, `head`, `director`,
  `manager`, `individual_contributor`, `intern`, `unknown`) -- do not invent
  new categories that downstream modules (M2's segment routing) don't expect.
- If uncertain, return a lower `confidence` rather than a confident-sounding
  guess. `enrich.py` multiplies this into the row's final `confidence` column
  that SDRs see before acting on the record.
