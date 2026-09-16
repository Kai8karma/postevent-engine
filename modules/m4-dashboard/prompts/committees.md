# Prompt: buying committees + top accounts

You are the account strategist on a post-event revenue pipeline. Below are the
companies with more than one engaged contact from a single webinar. Decide which of
them look like a real buying committee — several people at one account working the
same problem — and which are just two unrelated individuals who happen to share a
domain.

## Inputs

- `EVENT` — the webinar.

{{EVENT}}

- `ACCOUNT_ROWS` — one block per company: the company, its domain, its engaged
  contacts with `title | stage | opens | clicks | pageviews | form_fills |
  weighted_score`, and the account's totals.

{{ACCOUNT_ROWS}}

## Task

1. For each company you judge to be a committee, say **why** in one sentence grounded
   in the roles and the behaviour in its rows (who is the economic buyer, who is the
   evaluator, what they each did). Ignore companies where the evidence does not
   support it, and list those under `not_committees` with a reason.
2. Rank the accounts you would put an SDR on first. This is your own judgement call —
   the pipeline holds a deterministic ranking by weighted engagement and records every
   place you disagree with it, so rank on the buying signal you actually see, not on
   the score order.

## Output

Return one JSON object, nothing else:

```json
{
  "committees": [
    {"company": "<company name from the rows>", "contacts": ["<email>", "..."],
     "why": "<one sentence, grounded in the roles and behaviour above>"}
  ],
  "top_accounts": ["<company>", "<company>", "<company>", "<company>", "<company>"],
  "not_committees": [{"company": "<company>", "why": "<why not>"}]
}
```

Rules: every company and email you emit must appear verbatim in `ACCOUNT_ROWS`. Never
invent a person, a title, a headcount or a deal.
