# Unit Economics — superseded (v1)

This file estimated per-event LLM/Clay cost and a manual-hours comparison
for the v1 submission (rejected 2026-09-12 for not following the case
study). It priced against Claude Sonnet token counts that were never
metered per call, against a placeholder fixture — none of it carries over
to v2's Darwinbox build, and no v2 economics analysis has been redone.

The brief's four module tables don't ask for a cost model, so v2 dropped
this content from the top-level narrative. What v2 does record per call —
model, purpose, prompt size, latency, HTTP status, parse result — is in each
run's `*_llm_calls.json` receipt (`out/receipts/m1-live-slice-30/m1_llm_calls.json`,
`out/receipts/m2-live/m2_llm_calls.json`,
`out/receipts/m3-live/receipts/m3_llm_calls.json`,
`out/receipts/m4-live-portal/receipts/m4_llm_calls.json`). Those name the
model actually used; cite them, not this page. No v2 cost figure has been
metered or published.
