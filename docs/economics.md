# Unit Economics — Per Event

Every number below is a stated estimate, not a measured production bill (no
Clay/HubSpot account exists yet to meter against). Assumptions are listed so a
reviewer can swap in their own rates and recompute in thirty seconds.

## Assumptions

| assumption | value |
|---|---|
| LLM pricing | Claude Sonnet — $3.00 / MTok input, $15.00 / MTok output |
| Token estimate per module | given (M1 ~40k, M2 ~15k, M3 ~60k, M4 ~5k) — 120k tok/event total |
| Input/output split per module | assumed per task shape (enrichment reads a lot, writes little; content generation is the reverse) — see table |
| Clay credit price | ~$0.05/credit (blended self-serve tier, Explorer $149/2,000cr → Pro $349/10,000cr) |
| Clay credits per contact | 3 (person + company + email/title waterfall), M1 only |
| Registrants per event | 150 (this fixture's actual row count) |
| Manual hourly rate | $40/h (given) |
| Manual hours per task | enrichment 3h, emails 2h, repurposing 6h, reporting 2h (given) — 13h/event total |

## LLM cost by module

| module | tokens | split (in/out) | cost |
|---|---|---|---|
| M1 Enrichment | 40k | 26.0k / 14.0k | $0.288 |
| M2 Comms | 15k | 5.25k / 9.75k | $0.162 |
| M3 Repurposing | 60k | 18.0k / 42.0k | $0.684 |
| M4 Dashboard | 5k | 3.5k / 1.5k | $0.033 |
| **Total** | **120k** | — | **$1.17** |

## Clay cost (M1 only)

150 registrants × 3 credits × $0.05/credit = **$22.50/event**.

This is the real cost story: Clay is ~19x the entire LLM spend. If a reviewer
wants to cut cost further, the lever is enrichment-provider selection and
caching (dedupe against HubSpot *before* calling Clay, so already-known
contacts never re-spend a credit) — not swapping models.

## Full per-module comparison

| module | manual task | manual $ (hrs × $40) | AI $ (LLM + Clay) | multiple |
|---|---|---|---|---|
| M1 — Enrichment | 3h | $120.00 | $22.79 ($0.29 LLM + $22.50 Clay) | 5.3x |
| M2 — Comms | 2h | $80.00 | $0.16 | ~493x |
| M3 — Repurposing | 6h | $240.00 | $0.68 | ~351x |
| M4 — Dashboard/reporting | 2h | $80.00 | $0.03 | ~2,390x |
| **Total** | **13h** | **$520.00** | **$23.67** | **~22x** |

The multiple swings wildly by module and that's not a trick of the accounting
— it's the real shape of the cost. M1 is bottlenecked by a paid data provider
(Clay), so its multiple is modest. M2/M3/M4 are pure LLM-generation tasks
against near-zero marginal token cost, so their multiples look absurd on paper
— that's genuinely how it works once the pipeline exists: the marginal cost of
generating one more blog draft or one more email variant is fractions of a
cent, while the manual version is always a fixed number of human-hours.

## Fully-loaded (with human review time)

The build ships with a judgment gate — a human approves the three email
variants before HubSpot sends anything (see [architecture.md](architecture.md)).
That review time is real cost and belongs in an honest total. Estimated at
~25 minutes/event across all four modules (skim ICP-tier overrides on
flagged rows, approve sends, glance at content drafts, check the dashboard
narrative once):

- Infra only: $520.00 / $23.67 ≈ **22x**
- Infra + ~25min human review ($16.67 @ $40/h): $520.00 / $40.34 ≈ **12.9x**

Both numbers are legitimate depending on what's being asked — "what does the
AI cost" vs. "what does the AI-assisted workflow cost end to end."

## At scale (illustrative, 20 events/month)

| | manual | AI (infra only) | AI (fully loaded) |
|---|---|---|---|
| per event | $520 | $23.67 | $40.34 |
| per month (20 events) | $10,400 | $473 | $807 |

At "high volume of webinars" (the assignment's own framing of the client),
this compounds fast — the Clay line becomes the one worth negotiating volume
pricing on, not the LLM line.
