# Assignment: Working Prototype for AI-led Post-Event Content Automation

Deadline: 2026-08-23 (Sunday) 20:00 IST. Received: 2026-08-21 (Friday) 15:25 IST.
Internal ship target: Sunday ~14:00 IST (6h early, visibly).

## Context (verbatim from assignment)

They run a high volume of webinars and events. Each generates rich inputs (recordings, session details, speaker info, attendee data, collateral) but turning these into post-event marketing assets is manual, delayed, and inconsistent. They want to test whether AI can automate a meaningful part of this workflow.

## The Ask

Build a working prototype that demonstrates how AI can automate the post-event workflow end to end. Not a concept note. Not a workflow document. A working build they can review and test live.

Expected stack: HubSpot, Clay, n8n, HTML dashboards, and LLM APIs. **AI must be the engine, not a wrapper around manual work.**

Four independent modules. Each solves a distinct problem and can be demoed on its own. Attempt all four. **Depth of execution matters more than breadth.**

## Module 1: Lead List Enrichment
- Input: raw registrant/attendee list from webinar platform (Zoom, GoTo, ON24) — incomplete, messy contact and company fields.
- AI role: fuzzy match + dedupe against HubSpot, infer missing fields (title, function, seniority, company size, industry), enrich company data, score ICP fit.
- Tools: Clay for enrichment, HubSpot for dedupe and property mapping, LLM for classification and normalisation.
- Output: HubSpot-ready file with contact and company completeness above 90 percent, ICP tier tagged, region assigned, ownership routed, lifecycle stage set. Ready for SDR handoff same day.

## Module 2: Post-Event Communications
- Input: webinar recording, transcript, speaker list, segmented attendance data (attendees, no-shows, speakers).
- AI role: segment-specific email copy, subject line variants, key-takeaway snippets. Personalise by role/industry where data allows.
- Tools: n8n orchestration, HubSpot for send and tracking, LLM for copy generation.
- Output: three email variants dispatched within 24 hours of event close — attendee thank-you (recording + takeaways), no-show catch-up (recording + CTA), speaker thank-you (performance snapshot). All logged to HubSpot with UTM tracking.

## Module 3: Content Repurposing
- Input: webinar recording + full transcript.
- AI role: extract key moments, insights, quotes, data points; generate multi-format content package tuned per channel.
- Tools: transcription API, LLM for generation, n8n pipeline, image generation for visual assets.
- Output: one blog draft (800–1200 words), YouTube chapter markers + description + thumbnail brief, one infographic outline with data points, 5–10 social posts (LinkedIn, X) with distinct hooks. Saved to shared drive, tagged by event.

## Module 4: Lead Intelligence Dashboard
- Input: HubSpot data on event contacts, engagement events (opens, clicks, page views, form fills), lifecycle stage changes.
- AI role: detect engagement anomalies, score lead interest, narrate stage movement, surface top accounts and buying committees.
- Tools: HubSpot API, n8n sync, HTML dashboard (self-contained or hosted).
- Output: live HTML dashboard — attendee-to-MQL conversion, top engaged accounts/contacts, lifecycle stage movement across 7/14/30 days, one AI-generated narrative summary refreshed on load.
