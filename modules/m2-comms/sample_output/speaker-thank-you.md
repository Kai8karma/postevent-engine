---
segment: speaker
subject_a: "Your session numbers are in: 82 attendees, 58% avg watch-through"
subject_b: "Thank you, {{first_name}} — here's how Tuesday's session performed"
to_count: 3
utm_campaign: pipeline-after-the-webinar-2026-07-20
utm_content_a: speaker-thank-you-a
utm_content_b: speaker-thank-you-b
---

Hi {{first_name}},

Thank you for joining "Pipeline After the Webinar: Turning Event Engagement into Revenue" — {{own_point_line}}

**Performance snapshot**
- **Attendance:** {{attendance_count}} live attendees (of {{registered_count}} registered)
- **Average watch time:** {{avg_watch_minutes}} minutes of the {{duration_min}}-minute session ({{watch_pct}}% average completion)
- **Your quote that's already being reshared:** {{top_quote}}

{{other_speakers_line}}

{{internal_or_external_note}}

The recording, the repurposed content package (blog, social posts, infographic outline), and this email are all tagged to a single campaign ID in our CRM, per the point made live on the call — every asset gets its own UTM, no exceptions, so in a quarter we can actually tell you which piece of this touched a closed deal instead of shrugging and saying "something webinar-related, probably."

Thank you again for the sharpest, most number-heavy session we've run this year.

Priya Nair
VP Marketing, ACME Revenue Cloud

---
*Internal build note: this event's actual numbers — 82 attendees, 68 no-shows, 35.8 min average watch time — are computed live from `data/fixtures/segments.json` and `data/incoming/registrants.csv`, not hardcoded. [Recording]({{recording_link}})*
