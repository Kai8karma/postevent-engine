# Prompt: Missing-Field Inference (company / title / function / seniority)

Used by `enrich.py`'s live lane (the default lane) to resolve the **per-person**
fields the deterministic rule tables (company-peer majority vote, domain lookup,
keyword classifiers) could not resolve. Called **once per batch of ambiguous
rows**, not once per contact -- a 150-row registrant list should cost two or
three LLM calls, not 150.

Scope boundary: this prompt does **not** produce `industry` or `company_size`.
Those are company-level fields owned by `firmographics.md`, which runs next over
distinct companies rather than per contact (one call per ~40 companies instead of
the same company being re-guessed by every registrant from it). `icp_scoring.md`
then consumes all of it.

## When this prompt fires

Rows still carrying a generic title (`"Attendee"`) or an unresolved company
(`"Unknown"`) after the rule-based cascade (sibling-duplicate backfill ->
company-mode-title / domain-to-company lookup), plus rows whose company name is
non-ASCII-dominant and so structurally unreadable to the ASCII keyword tables.
Everything else stays on the deterministic path -- cheaper, reproducible, and
auditable in `icp_rationale`.

## Input contract

```json
{
  "event_context": {
    "host_company": "Darwinbox",
    "topic": "AI-native HCM platform (core HR, payroll, talent, Cortex AI agents)"
  },
  "rows": [
    {
      "row_id": "anton.chen@gmail.com",
      "firstname": "Anton",
      "lastname": "Chen",
      "email": "anton.chen@gmail.com",
      "email_domain": "gmail.com",
      "company": "",
      "jobtitle": "",
      "country": "FR",
      "peer_titles_at_company": []
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
      "jobtitle": {"value": "Unknown", "confidence": 0.1, "rationale": "no title given and no peer signal at this company"},
      "function": {"value": "general", "confidence": 0.3, "rationale": "no title to classify"},
      "seniority": {"value": "unknown", "confidence": 0.3, "rationale": "no title to classify"}
    }
  ]
}
```

Reply with ONLY that JSON object. Every `row_id` in the input must appear
exactly once in the output.

## Guardrails

- Never invent a company name that isn't grounded in the email domain, a peer
  registrant's company field, or the event context. `"Unknown"` is a valid,
  honest answer -- it is not a failure, and a fabricated employer is worse than
  a blank one for a CRM record an SDR will call from.
- Never invent a job title either. If the row has no title and no peer title at
  the same company, return `"Unknown"` -- `enrich.py` keeps its generic
  `"Attendee"` placeholder and, correctly, does **not** count that row as
  verified. Inferring "HR Manager" because the event was an HR webinar is
  exactly the fabrication the completeness metric exists to catch.
- Do infer a title when the email localpart, the peer titles at the same
  company, or an explicitly abbreviated/garbled title supports one (e.g.
  `"Sr. Mgr - People Ops"` -> `"Senior Manager, People Operations"`). Normalising
  a present-but-messy title is the highest-value thing this prompt does.
- `function` and `seniority` must come only from the classifier categories
  already defined in `enrich.py` (`marketing`, `revops`, `sales`, `executive`,
  `customer_success`, `general` / `c_suite`, `vp`, `head`, `director`,
  `manager`, `individual_contributor`, `intern`, `unknown`) -- do not invent new
  categories that downstream modules (M2's segment routing) don't expect.
- Do not return `industry` or `company_size`; they are ignored here. See
  `firmographics.md`.
- If uncertain, return a lower `confidence` rather than a confident-sounding
  guess. `enrich.py` folds this into the row's `confidence` column that SDRs see
  before acting on the record.
