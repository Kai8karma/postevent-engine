# Prompt: ICP Tier Scoring + Rationale

Used by `enrich.py --live` as a qualitative check on top of the deterministic
`icp_tier()` function in `enrich.py` (exact title-list + company-size-range +
industry-set match against `config/icp.yaml`). The deterministic function is
authoritative for the tier assignment shipped in `hubspot_ready.csv` --
this prompt's job is to produce a second, human-readable rationale for the
rows a reviewer is most likely to question (borderline sizes, `industry:
"Other"`, or `unqualified`), and to flag disagreement for a human to
resolve rather than silently overriding the rule engine.

Called once per batch of flagged rows, same batching discipline as
`inference.md`.

## Input contract

```json
{
  "icp_config": {
    "tier1": {"titles": ["VP Marketing", "Head of Demand Gen", "Director Marketing Ops", "CMO"], "company_size": [200, 5000], "industries": ["SaaS", "Fintech", "IT Services"]},
    "tier2": {"titles": ["Marketing Manager", "Growth Manager", "RevOps Manager"], "company_size": [50, 200], "industries": ["SaaS", "Fintech", "IT Services", "Ecommerce"]},
    "tier3": {"titles": ["*"], "company_size": [1, 50], "industries": ["*"]}
  },
  "rows": [
    {
      "row_id": "elena.kapoor@pinehollowsoftware.com",
      "jobtitle": "RevOps Manager",
      "company": "Pinehollow Software",
      "industry": "SaaS",
      "company_size": 43,
      "rule_engine_tier": "tier3",
      "rule_engine_rationale": "tier3 -- catch-all title 'RevOps Manager'; company_size=43 in [1, 50]; industry=SaaS (wildcard)."
    }
  ]
}
```

## Output contract

```json
{
  "rows": [
    {
      "row_id": "elena.kapoor@pinehollowsoftware.com",
      "llm_tier": "tier3",
      "agrees_with_rule_engine": true,
      "rationale": "RevOps Manager at a 43-person SaaS company is a real buyer for a RevOps tool, but company_size falls short of tier2's 50-person floor -- rule engine's tier3 call is correct, not a borderline miss.",
      "confidence": 0.85
    }
  ]
}
```

## Guardrails

- `llm_tier` must be one of `tier1`, `tier2`, `tier3`, `unqualified` --
  the same closed set the rule engine uses. No new tiers.
- If `agrees_with_rule_engine` is `false`, `rationale` must name the exact
  criterion the model weighs differently (title, size, or industry) --
  never a vague "seems like a better fit."
- This prompt never changes what ships in `hubspot_ready.csv`.
  Disagreements are logged for a human reviewer (the same judgment-gate
  principle as M2's send-approval step) -- no blind auto-override of the
  deterministic tier.
- Do not use this prompt to re-derive company_size or industry -- those are
  inputs here, not outputs. If they look wrong, that's `inference.md`'s job.
