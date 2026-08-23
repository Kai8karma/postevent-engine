# Loom walkthrough scripts

Five 90-second walkthroughs, one per module plus the control room. Word-for-word
narration, timestamped roughly every 20 seconds, with what to show on screen at
each mark. Read it straight — no hype, no filler adjectives.

---

## 1. Control room + one-command offline run

**0:00 — screen: `docs/index.html`, hero + thesis box**
"This is the control room for the post-event engine — one page, four modules,
each independently demoable. The thesis box up top is honest about what's
actually AI at each step versus what's deterministic — I'd rather show you the
real shape of that than round it up."

**0:20 — screen: scroll to the modules grid**
"Four modules: lead enrichment, post-event comms, content repurposing, and a
lead-intelligence dashboard. Each one solves a distinct piece of the assignment
and runs on its own."

**0:40 — screen: terminal, run `python3 orchestrator/run_pipeline.py`**
"One command chains all four. Watch the banner before each stage — it tells
you plainly whether that stage is replaying cached AI output or about to call
a live model. This run is offline: zero network, zero keys, and it still
finishes in about a second because it's real code doing real work on fixture
data, not a spinner."

**1:00 — screen: the receipt table, then open `out/<slug>/m4/index.html`**
"That's the per-stage receipt — pass, seconds, key output. And this is where
it lands: a live dashboard built from what the pipeline just produced."

**1:20 — screen: back to `docs/index.html`, scroll to "Swap to your real event"**
"Swapping this onto a real webinar is two file edits, no code changes. That's
the whole pitch — this isn't a demo you'd throw away, it's the actual engine."

---

## 2. M1 enrichment — dedupe, ICP rationale, HubSpot export, Clay receipt

**0:00 — screen: `data/incoming/registrants.csv` open in an editor**
"This is a real mess — a 150-row Zoom export with duplicate rows, inconsistent
casing, free-mail addresses, and missing titles and company data. This is
what a registrant list actually looks like."

**0:20 — screen: terminal, run `enrich.py`, then `dedupe_report.json`**
"Enrichment runs a fuzzy dedupe against the existing HubSpot contacts using
difflib at an 0.80 threshold — no external library needed — and logs exactly
which rows it matched and why."

**0:40 — screen: `hubspot_ready.csv`, scroll to the ICP tier and rationale columns**
"Every row gets an ICP tier and a written rationale — not just a score. A
reviewer can see why a contact landed in tier one instead of trusting a black
box."

**1:00 — screen: `hubspot_companies.csv`, `hubspot_contacts.csv`, `quality_report.json`**
"Output is a proper three-file HubSpot export — companies, contacts, and this
combined analyst view — with completeness tracked separately from the rows
that genuinely need human review, so the ninety-percent bar can't quietly
launder unresolved data."

**1:20 — screen: `docs/index.html`, Live receipts section, Clay table**
"And this enrichment step isn't just a spec — Clay's managed company-enrichment
routine already ran live against three real domains, run ID and credit spend
both shown here. Production points that same call at every real domain in the
list."

---

## 3. M2 comms — segment variants, personalization, approval gate, UTMs

**0:00 — screen: `data/fixtures/segments.json`**
"Three segments come out of the event: attendees, no-shows, and speakers. Each
needs a different email, not a broadcast."

**0:20 — screen: terminal, run `comms.py`, then `emails/` directory**
"Comms generates all three variants, each with two subject-line options, drafted
against the real transcript and the enriched contact list."

**0:40 — screen: open one attendee email, highlight a personalized line**
"The copy is personalized by role and by industry where the data supports it
— a VP of Ops gets a different takeaway line than an IC in marketing at the
same company size."

**1:00 — screen: `approval_gate.json`**
"Nothing sends blind. This gate holds every email until a human flips
`approved` to true — that's a hard stop, not a suggestion."

**1:20 — screen: `sends_log.json`, highlight the `utm_source` / `utm_medium` / `utm_campaign` fields**
"Every link is UTM-tagged before it ever reaches HubSpot, so the dashboard
downstream can trace engagement back to the exact email and segment that
drove it."

---

## 4. M3 repurposing pack + rendered visuals

**0:00 — screen: `data/incoming/transcript.md`, scroll through it briefly**
"One input: the full webinar transcript, about eight thousand words across
three speakers."

**0:20 — screen: terminal, run `repurpose.py`, then `manifest.json`**
"One run produces the entire repurposing pack — blog draft, YouTube pack,
infographic outline, and social posts — all tagged to this event."

**0:40 — screen: `blog.md`, then `social.md`**
"The blog draft is eight hundred to twelve hundred words, grounded in actual
quotes and numbers from the transcript, not generic filler. The social posts
each take a different hook off the same material instead of restating one
angle five times."

**1:00 — screen: `youtube.md`, chapters + thumbnail brief, then `infographic.md`**
"YouTube gets chapter markers, a description, and a thumbnail brief. The
infographic outline pulls the specific data points worth visualizing."

**1:20 — screen: `visuals/youtube-thumbnail.png` and `visuals/quote-card-linkedin.png`**
"And the visual specs don't stop at text — here are the rendered thumbnail and
quote-card assets that brief turns into, ready to publish, not just described."

---

## 5. M4 dashboard — funnel, buying committees, anomalies, narrative badge

**0:00 — screen: dashboard `index.html`, top funnel panel**
"This is the lead-intelligence dashboard, built straight from M1's enriched
roster and the event's engagement data. Top of the page: attendee-to-MQL
conversion, computed directly from the numbers, not guessed."

**0:20 — screen: scroll to top accounts / buying-committee panel**
"Below that, the accounts actually worth chasing — and for each one, the
buying committee: who from that company showed up, and in what role."

**0:40 — screen: scroll to the 7/14/30-day lifecycle movement chart**
"This tracks how contacts moved through lifecycle stages over the week
after the event, out to thirty days — where the pipeline is actually
building, not just where it started."

**1:00 — screen: anomaly callouts, then the narrative summary + its live/fallback badge**
"These two callouts flag engagement patterns worth a second look. And this
narrative line is generated fresh on load by a live model call — the badge
tells you plainly if it's live or a labeled fallback, never pretending one
is the other."

**1:20 — screen: back to `docs/index.html`, "Swap to your real event"**
"Swap in your own webinar's export and this whole dashboard is live on your
data by Monday morning — same four modules, same approval gate, same
honesty about what's real. That's the pitch: point this at your next
webinar instead of ours."
