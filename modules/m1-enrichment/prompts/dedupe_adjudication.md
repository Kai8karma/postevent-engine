# Prompt: Gray-Zone Dedupe Adjudication (fuzzy-match score 0.65-0.80)

Used by `enrich.py --live` for the exact band a fixed threshold cannot
resolve: pairs whose `difflib.SequenceMatcher` composite score (see
`composite_score()` in enrich.py) lands in `[GRAY_ZONE_LOW, DEDUPE_THRESHOLD)`
-- currently `[0.65, 0.80)`. Below `DEDUPE_THRESHOLD` the rule engine already
merges automatically; above `GRAY_ZONE_LOW` the two records are too
dissimilar for even a qualitative read to help. The gray zone in between is
where two humans reviewing the same two records would genuinely disagree --
a fixed number can't reason about that, a person or a model can.

Outside this band the rule engine stays fully authoritative, `--live` or
not: `>=0.80` always merges, `<0.65` never does. This prompt is only ever
called with pairs already filtered into the band by `dedupe_within_batch()`
/ `dedupe_against_hubspot()`, and is hard-capped at `GRAY_ZONE_MAX_PAIRS`
(20) pairs per run -- a credit/review-load guard, not a quality signal.
Overflow pairs are logged as skipped and left on the rule engine's default
(no merge). Called **once per run**, every gray-zone pair combined into a
single batch -- same batching discipline as `inference.md` / `icp_scoring.md`.

Two kinds of pair arrive in the same batch:
- `within_batch` -- two rows in this event's own registrant export share a
  normalized (first, last) name but scored below threshold (e.g. a company
  or email-localpart mismatch inside an otherwise-matching name pair).
- `hubspot` -- a new registrant row scored against the closest *existing*
  HubSpot contact, but not closely enough for the rule engine to treat it as
  the same person.

## The asymmetry (read before answering)

- A **false merge** (deciding two different people are the same person)
  silently destroys a real, distinct lead -- their record disappears into
  someone else's, and nothing downstream ever surfaces the loss. This is
  the more severe failure and effectively unrecoverable once M2/M3/M4 run on
  top of it (a merged-away contact never gets their own follow-up).
- A **false split** (failing to merge two records that were actually the
  same person) at worst double-touches one person -- two emails instead of
  one. Annoying, but fully recoverable: a human reviewer or a later merge
  pass can fix it without losing any data.

Because the two failure modes are not equally costly, do not merge on a
"probably." Require the same kind of concrete, nameable evidence
`icp_scoring.md` requires for a tier disagreement (a specific shared detail
-- email localpart pattern, company, a near-identical name spelling that
reads as one person written two ways), not a vibe or the raw score alone.
When genuinely torn between two readings, answer `no_merge`.

## Input contract

```json
{
  "pairs": [
    {
      "pair_id": "wb:tomas.ivanov@everlinetech.com|t.ivanov@everlinetech.com",
      "kind": "within_batch",
      "score": 0.74,
      "context": "two rows in the same registrant export share a normalized (first, last) name",
      "a": {"role": "candidate_primary", "firstname": "Tomas", "lastname": "Ivanov", "email": "tomas.ivanov@everlinetech.com", "company": "Everline Technologies", "jobtitle": "RevOps Manager"},
      "b": {"role": "candidate_duplicate", "firstname": "Tomas", "lastname": "Ivanov", "email": "t.ivanov@everlinetech.com", "company": "Everline Technologies", "jobtitle": ""}
    },
    {
      "pair_id": "hs:nora.garcia@wavecrestit.com|100019",
      "kind": "hubspot",
      "score": 0.789,
      "context": "new registrant row scored against the closest existing HubSpot contact",
      "a": {"role": "existing_hubspot_contact", "firstname": "Nora", "lastname": "Weber", "email": "nora.weber@wavecrestit.com", "company": "Wavecrest IT Services", "jobtitle": "Growth Manager"},
      "b": {"role": "new_registrant", "firstname": "Nora", "lastname": "Garcia", "email": "nora.garcia@wavecrestit.com", "company": "Wavecrest IT Services", "jobtitle": ""}
    }
  ]
}
```

## Output contract

```json
{
  "pairs": [
    {"pair_id": "wb:tomas.ivanov@everlinetech.com|t.ivanov@everlinetech.com", "decision": "merge", "confidence": 0.85, "rationale": "same name, same company, and the email localparts are the same person written two ways (first.last vs f.last) -- a classic export duplicate, not two colleagues."},
    {"pair_id": "hs:nora.garcia@wavecrestit.com|100019", "decision": "no_merge", "confidence": 0.55, "rationale": "same first name and company, but the last names disagree (Garcia vs Weber) with no other field to break the tie -- could be a name change, but could just as easily be two different people at the same employer; not enough evidence to overwrite an existing HubSpot record."}
  ]
}
```

## Guardrails

- `decision` must be exactly `"merge"` or `"no_merge"` -- no third option,
  no partial merges. Anything else is treated as `"no_merge"` by the caller.
- `rationale` must name the specific evidence considered (which fields
  agreed/disagreed, and why that is or isn't enough) -- never a restatement
  of the score or a vague "seems like the same person."
- This prompt's decision is authoritative **only for the pair it was given**,
  and only because that pair already fell inside the gray band -- it never
  runs on, and can never override, a pair the rule engine already resolved
  at `>=DEDUPE_THRESHOLD` or ruled out at `<GRAY_ZONE_LOW`.
- A `merge` decision on a `within_batch` pair drops the `b` record and
  folds any of its non-blank fields into `a` (same effect as the rule
  engine's own within-batch merge). A `merge` decision on a `hubspot` pair
  treats the registrant row as an update to the named existing contact,
  never a fresh `create_new`.
- A parse failure or an unavailable backend degrades every pair in the
  batch to `no_merge` (the rule engine's existing default for anything
  under threshold) with a warning -- this prompt never blocks the run, and
  can only ever *decline* to merge on failure, never merge silently.
