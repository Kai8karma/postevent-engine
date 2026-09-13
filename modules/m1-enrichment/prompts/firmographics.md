# Prompt: Company Firmographics (industry + employee count)

Used by `enrich.py`'s live lane (the default lane) to infer the two **company**
fields the assignment brief names — `industry` and `numemployees` — for every
distinct company in the batch. Called **once per batch of companies**, not once
per contact: 150 registrants at ~76 distinct companies costs 2-3 calls.

This prompt owns industry and company size. `inference.md` owns the per-person
fields (title / function / seniority / company name) and must not re-derive
these; `icp_scoring.md` consumes them as inputs.

## Precedence (enforced in code, stated here so the model knows its place)

`clay` > `llm` (this prompt) > `rules` > `synthetic`. A Clay result supplied via
`--clay-results` overwrites whatever this prompt returned and re-labels the row
`*_source: clay`. Your answer is the best available evidence *until* Clay lands,
not a substitute for it — which is why `confidence` below is load-bearing: any
company you score under 0.7 is emitted into `--emit-clay-domains` for the Clay
leg to resolve, and is NOT counted as verified in the completeness metric.

## Input contract

```json
{
  "industry_vocabulary": ["IT/ITES", "BFSI", "Manufacturing", "Pharma", "Retail",
                          "Consumer Goods", "Hospitality", "Healthcare", "Telecom",
                          "Energy", "Aviation", "Conglomerate", "Internet", "Fintech",
                          "SaaS", "Logistics", "Real Estate", "Fitness", "Other"],
  "companies": [
    {"company_id": "infosys.com", "company": "Infosys", "domain": "infosys.com",
     "countries": ["IN"], "registrant_count": 4,
     "sample_titles": ["Head of HR", "HRIS Manager"]},
    {"company_id": "name:selfemployed", "company": "SELF-EMPLOYED", "domain": "",
     "countries": ["IN"], "registrant_count": 1, "sample_titles": ["HR Business Partner"]}
  ]
}
```

`company_id` is an opaque join key — echo it back verbatim. A `company_id`
starting `name:` means the contact registered from a freemail address and there
is no verifiable company domain; size and industry may still be inferable from
the company name, but say so in the confidence.

## Mapping real sectors onto this vocabulary

The vocabulary is written in Indian-market shorthand. Map the sector you
actually know onto it — do NOT fall back to `"Other"` because the obvious label
is missing:

| the company is… | the vocabulary term is |
|---|---|
| IT services, consulting, outsourcing, BPO, system integration | `IT/ITES` |
| bank, NBFC, insurer, broker, asset manager, card network | `BFSI` |
| a software product / cloud / platform vendor | `SaaS` |
| payments, lending, neobank, wallet, trading app | `Fintech` |
| a consumer internet / marketplace / delivery / e-commerce app | `Internet` |
| pharma, biotech, generic drugs, CRO | `Pharma` |
| hospital chain, diagnostics, clinics, health insurer's care arm | `Healthcare` |
| factory, auto, steel, cement, chemicals, electricals, engineering | `Manufacturing` |
| FMCG, food & beverage, personal care, apparel brand | `Consumer Goods` |
| supermarket, department store, mall operator, retail chain | `Retail` |
| courier, 3PL, freight, warehousing, last-mile | `Logistics` |
| airline, airport, aerospace | `Aviation` |
| telco, ISP, tower company | `Telecom` |
| oil & gas, power, renewables, utilities | `Energy` |
| hotels, restaurants, travel, QSR | `Hospitality` |
| property developer, REIT, facilities | `Real Estate` |
| gyms, studios, fitness apps | `Fitness` |
| a diversified group spanning several of the above | `Conglomerate` |

`"Other"` is for rows that are genuinely not a company — `"SELF-EMPLOYED"`,
`"Freelance"`, a university, `"N/A"`. If you can name the sector, you must pick
a term from the list.

## Output contract

```json
{
  "companies": [
    {"company_id": "hdfcbank.com", "industry": "BFSI", "numemployees": 213000,
     "confidence": 0.95,
     "rationale": "HDFC Bank is India's largest private-sector bank; headcount is public and above 200k."},
    {"company_id": "ninjavan.co", "industry": "Logistics", "numemployees": 3500,
     "confidence": 0.8,
     "rationale": "Ninja Van is a Southeast Asian last-mile parcel network; scale inferred from its regional footprint."},
    {"company_id": "pinehollowsoftware.com", "industry": "SaaS", "numemployees": 150,
     "confidence": 0.55,
     "rationale": "Name and .com domain read as a software vendor, but this specific firm is not one I recognise — size is a guess."},
    {"company_id": "name:selfemployed", "industry": "Other", "numemployees": 1,
     "confidence": 0.9,
     "rationale": "'SELF-EMPLOYED' is not a company; confidently a one-person entity with no sector."}
  ]
}
```

Those four rows are **illustrations of the shape and of the confidence range** —
they are not values to copy. Answer for the companies in INPUT, and expect your
answers to look nothing like this list.

Reply with ONLY that JSON object. Every `company_id` in the input must appear
exactly once in the output.

## Guardrails

- `industry` MUST be one of `industry_vocabulary` verbatim. These are the exact
  strings `config/icp.yaml` tiers on — a near-miss like "IT Services",
  "Banking" or "Information Technology" silently drops the company out of every
  tier's industry list. Use the mapping table above.
- `numemployees` is a **whole-company** headcount integer (not the size of the
  local office, not the size of the HR team). A well-known enterprise should not
  be scored at 50.
- **Calibrate the confidence to what you actually know, per company.** Returning
  the same number for every row is a failure mode, not caution:
  - `0.9-1.0` — you can name the firm and its sector, and its headcount is
    public knowledge (listed enterprises, national banks, big-4 firms, major
    airlines, household consumer brands). Most named employers in a webinar
    registration list are in this band. Also use it for the confidently
    not-a-company rows.
  - `0.7-0.89` — you know the sector from the name/domain and can place the
    headcount within an order of magnitude, but not precisely.
  - `<0.7` — you do not recognise this company at all. Give your honest best
    estimate anyway and score it here: that routes it to Clay for a real
    lookup rather than letting a guess enter the completeness numerator as
    fact. Reserve this band for genuine non-recognition.
- Do not invent a headcount you cannot defend — but equally, do not hide behind
  a low confidence for a company you clearly know. Both are errors; the first
  fabricates data, the second discards real signal and buys a Clay credit for
  nothing.
- `rationale` is one sentence, naming the evidence (public headcount, sector of
  the named firm, domain TLD, registrant density in the batch).
