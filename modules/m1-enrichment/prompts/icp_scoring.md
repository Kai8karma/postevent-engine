# Prompt: ICP Tier Scoring + Rationale

Used by `enrich.py`'s live lane (the default lane) to assign the ICP tier that
**ships** in `hubspot_ready.csv`. This is the brief's "score ICP fit" step, and
the model does the scoring — not a commentary track over a rule table.

The deterministic `icp_tier()` function in `enrich.py` (exact title-list ×
company-size-range × industry-set match against `config/icp.yaml`) still runs on
every row, but its role is now **validator**, not author:

- model tier within one level of the rules tier → model tier ships, `icp_source: llm`
- model tier more than one level away (e.g. `tier1` vs `tier3`) → model tier
  still ships, but the row is flagged `needs_review` with reason
  `icp_disagreement` so a human adjudicates before an SDR acts on it
- model returned nothing usable for a row → rules tier ships, `icp_source: rules`

Lifecycle stage is **not** inferred here. It is derived from the final tier by
`enrich.py::lifecycle_target()` — policy, not judgement (see that docstring).

Called once per batch of rows, same batching discipline as `inference.md`, and
always **after** `firmographics.md` so `industry` and `company_size` are the
enriched values rather than the rule-table placeholders.

## Input contract

```json
{
  "icp_config": {
    "tier1": {"titles": ["CHRO", "VP People", "Head of HR", "HRIS Lead"], "company_size": [500, 1000000], "industries": ["IT/ITES", "BFSI", "Manufacturing"]},
    "tier2": {"titles": ["HR Business Partner", "Payroll Manager", "CIO"], "company_size": [200, 1000000], "industries": ["IT/ITES", "BFSI", "Manufacturing", "Retail"]},
    "tier3": {"titles": ["*"], "company_size": [1, 200], "industries": ["*"]}
  },
  "rows": [
    {
      "row_id": "meera.rao@infosys.com",
      "jobtitle": "Head of HR Shared Services",
      "company": "Infosys",
      "industry": "IT/ITES",
      "company_size": 340000,
      "company_size_source": "llm",
      "seniority": "head",
      "country": "IN",
      "rule_engine_tier": "tier2",
      "rule_engine_rationale": "tier2 -- title 'Head of HR Shared Services' in tier titles; company_size=340000 in [200, 1000000]; industry=IT/ITES in [...]"
    }
  ]
}
```

## Output contract

```json
{
  "rows": [
    {
      "row_id": "meera.rao@infosys.com",
      "icp_tier": "tier1",
      "icp_rationale": "Head of HR Shared Services at Infosys (IT/ITES, ~340k employees) owns exactly the HR-operations scope this platform replaces, and the title is a tier1 'Head of Shared Services' variant the literal title list misses.",
      "icp_confidence": 0.85
    }
  ]
}
```

Reply with ONLY that JSON object. Every `row_id` in the input must appear
exactly once in the output.

## Guardrails

- `icp_tier` must be one of `tier1`, `tier2`, `tier3`, `unqualified`. No new
  tiers, no nulls, no "tier1/tier2".
- Score the **buying role**, not the string. The rule engine can only do exact
  title matching, which is why it hands you the row: "Associate Director –
  People Operations" is the tier1 "Director HR" buyer; "Student", "Consultant"
  with no employer, and a vendor's own sales rep are not, whatever the company
  size says.
- Company size and industry are **inputs** here, not outputs. If they look wrong,
  say so in the rationale and score the tier you can defend from the title — do
  not silently substitute your own headcount (that is `firmographics.md`'s job,
  and its number is what the CSV reports).
- When `company_size_source` is `rules` or `synthetic`, the size is a placeholder,
  not evidence. Weight title and industry, and keep `icp_confidence` ≤ 0.6.
- `icp_rationale` is ONE sentence and must cite at least two of
  title / company size / industry. It ships in the CSV an SDR reads before the
  first call — "good fit" is not a rationale.
- `icp_confidence` is calibrated: ≥0.85 = the title and firmographics both
  clearly place the buyer; 0.6-0.84 = one signal is inferred; <0.6 = the row is
  genuinely ambiguous and should be reviewed.
- Disagreeing with the rule engine is expected and wanted — the title lists are
  literal and finite. Disagreeing by more than one tier gets the row flagged for
  a human, so reserve that for cases you can defend in the rationale.
