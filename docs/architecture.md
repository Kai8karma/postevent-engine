# Architecture

Two diagrams. The first is the data DAG: what feeds what, module by module, ending
at this control room. The second is the honest two-lane story — the lane that
actually runs today (local, offline, stdlib Python) next to the lane it swaps
into on a real account (n8n + Clay + HubSpot + Vercel), same DAG underneath.

Both are reproduced in `docs/index.html` as `<pre class="mermaid">` source (portable —
paste into any Mermaid renderer or Claude artifact) and as hand-drawn inline SVG
(what actually renders in that page, zero dependencies).

## (a) End-to-end data flow

```mermaid
flowchart TD
    subgraph IN1G["for M1"]
        IN1A["registrants.csv<br/>150 rows · messy"]
        IN1B["hubspot_existing.json<br/>+ config/icp.yaml"]
    end

    subgraph IN2G["for M2"]
        IN2A["transcript.md<br/>+ speakers/event.json"]
        IN2B["segments.json<br/>attendee · no-show · speaker"]
    end

    IN3["transcript.md<br/>same source, full ~8k-word transcript<br/>(for M3)"]

    M1["M1 — Lead List Enrichment<br/>fuzzy dedupe → infer fields<br/>→ ICP score + rationale → route"]
    M2["M2 — Post-Event Comms<br/>segment attendee/no-show/speaker<br/>→ draft 3 variants × 2 subjects"]
    M3["M3 — Content Repurposing<br/>extract quotes/moments/data<br/>→ blog/YT/infographic/social"]

    GATE{{"human approval gate<br/>no blind send"}}

    O1["enriched.json<br/>contact+company completeness ≥90%"]
    O2["comms.json<br/>sent + UTM-logged to HubSpot"]
    O3["manifest.json<br/>blog + YT pack + infographic + 8 posts"]

    M4["M4 — Lead Intelligence Dashboard<br/>attendee→MQL funnel · top accounts + buying committee<br/>7/14/30-day lifecycle movement · AI narrative summary"]

    FIN["Judge Control Room<br/>docs/index.html"]

    IN1A --> M1
    IN1B --> M1
    IN2A --> M2
    IN2B --> M2
    IN3 --> M3

    M1 -- "enriched contacts (dedup'd)" --> M2
    M1 --> O1
    M2 --> GATE --> O2
    M3 --> O3

    O1 --> M4
    O2 --> M4
    O3 --> M4
    ENG["+ engagement.json<br/>opens · clicks · pageviews · fills"] --> M4

    M4 --> FIN
```

Reading it: M1 is the only module that touches HubSpot's existing contact
records — everything downstream trusts its dedup, not the raw registrant list.
M2 personalizes off M1's enriched output, not the messy CSV. M3 is the one
module with zero dependency on M1/M2 — it works straight off the transcript,
which is why it's the one guaranteed to produce something even if enrichment
or comms fail. M4 is the sink: it can't run meaningfully until the other three
have produced at least one artifact each, plus engagement data that only exists
once the emails have actually gone out and been opened.

## (b) Demo lane vs. production lane

```mermaid
flowchart LR
    subgraph DEMO["DEMO LANE — runs today, fully offline"]
        direction TB
        d0["data/incoming/ + data/fixtures/<br/>flat files, checked into the repo"]
        d1["orchestrator/run_pipeline.py<br/>subprocess chain → pass/fail receipt"]
        d2["enrich.py · comms.py · repurpose.py ·<br/>build_dashboard.py — stdlib only"]
        d3["claude -p via each module's own<br/>call_llm()/call_claude_live() helper<br/>(only with --live)"]
        d4["docs/index.html<br/>opened directly in a browser"]
        d5["orchestrator/n8n/local-demo/*.json<br/>n8n import (Docker) — mirrors this DAG, no account"]
        d0 --> d1 --> d2
        d2 -. "--live" .-> d3
        d2 --> d4
        d4 --> d5
    end

    subgraph PROD["PRODUCTION LANE — swap-in path"]
        direction TB
        p0["Webinar platform webhook<br/>Zoom · GoTo · ON24"]
        p1["n8n workflow — cloud lane<br/>orchestrator/n8n/cloud/*.json — real HTTP Request nodes"]
        p2a["Clay table<br/>waterfall enrichment"]
        p2b["HubSpot API<br/>contacts, companies, dedupe"]
        p3["Claude API<br/>classification + generation at every node"]
        pg{{"human approval node<br/>before any send"}}
        p5["HubSpot send + tracking<br/>UTM, opens, clicks, lifecycle stage"]
        p6["Vercel serverless fn<br/>/api/narrative.js reads HubSpot live"]
        p7["Hosted dashboard URL<br/>same M4 UI, live data"]
        p0 --> p1
        p1 --> p2a --> p3
        p1 --> p2b --> p3
        p3 --> pg --> p5 --> p6 --> p7
    end

    d4 -. "same DAG, same prompts/ — only the transport changes" .-> p1
```

The two lanes share every prompt in every module's `prompts/` dir, the same
four-module boundary, and the same human-approval gate before any send. The
only thing that changes crossing from left to right is *where the bytes live*
— flat files become HubSpot/Clay records, a Python subprocess chain becomes an
n8n workflow, and the dashboard goes from "open a file" to "hit a URL that
recomputes on load." Nothing about the reasoning changes; that's the point of
building offline-first — the swap is a transport change, not a rewrite.

n8n itself is two-lane too: `orchestrator/n8n/local-demo/*.json` imports into
a local n8n (Docker, no account) and mirrors the exact same stdlib DAG the demo
lane runs — it's a second way to watch the same logic execute, not a
different pipeline. `orchestrator/n8n/cloud/*.json` is the production lane —
real HTTP Request nodes hitting Clay, the HubSpot API, and the Claude API,
gated behind actual credentials.

There is no shared workspace-level chokepoint script for LLM calls in this
repo. Each module — M1's `enrich.py`, M2's `comms.py`, M3's `repurpose.py` —
owns its own `call_llm()` / `call_claude_live()` helper that shells out to
`claude -p` directly. All of them strip `USER` from the subprocess
environment before the call, since `claude -p` 401s against keychain auth
otherwise — that quirk is per-module, not centralized.
