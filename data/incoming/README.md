# `data/incoming/` — the event this pipeline runs on

**Event:** *From Hype to High-Impact: How to Start and Scale AI in HR* — a real, public Darwinbox on-demand webinar
(https://explore.darwinbox.com/lp/resources/events/webinar-how-to-start-and-scale-ai-in-hr).
Speakers: Q Hamirani (Chief People Officer, HighLevel) and Sudi Bjornstad Korba (SVP Sales North America, Darwinbox).

| file | what it is | where it comes from |
|---|---|---|
| `event.json` | session metadata: title, date, host, recording files, speakers, provenance notes | public event page; date assumed (see `_provenance.date`) |
| `speakers.json` | speaker names, titles, companies, bios | public event page, verbatim |
| `media/chapter{1,2,3}.mp4` (git-ignored) | the recording, three chapters, ~33 min total | Darwinbox's HubSpot file CDN (URLs in `event.json`) |
| `media/chapter{1,2,3}.mp3` (git-ignored) | mono 16 kHz audio extracted with ffmpeg | `ffmpeg -i chapterN.mp4 -vn -ac 1 -ar 16000 -b:a 48k chapterN.mp3` |
| `transcript.md` | canonical transcript, `[MM:SS] **Speaker:** text` turns | **Sarvam saaras:v3 batch STT** on the chapter audio (`modules/m3-repurpose/transcribe_batch.py`) → `tools/build_transcript.py`. No captions exist for this recording, so this is the pipeline's real transcription step, receipt in `out/receipts/transcription.json` (per-chapter Sarvam responses in `out/receipts/transcription/`) |
| `transcript.vtt` | WebVTT twin of the transcript (platform-export shape) | `python3 data/incoming/tools/md_to_vtt.py` — never hand-edit |
| `registrants.csv` | 150-row Zoom-webinar registrant export, deliberately messy | `python3 data/incoming/tools/gen_registrants.py` (seeded, deterministic) |

**Registrant data is synthetic.** Every registrant's name and email address is invented. Employers are real companies
with real domains (so Clay and HubSpot enrichment return real firmographics), spread across India, Southeast Asia, the
Middle East and North America in the industries Darwinbox sells into. No real person's contact data is used anywhere.
The two speakers are real people; their mail is routed to demo aliases because their addresses are not public.

To run the pipeline on a different event: replace `event.json`, `speakers.json`, the recording, and `registrants.csv`,
regenerate `transcript.md` with the two commands above, and adjust `config/icp.yaml` if the buyer profile changes.
