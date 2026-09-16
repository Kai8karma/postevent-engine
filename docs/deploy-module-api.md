# Deploy the module API on Railway

The module API (`api/server.py`) runs the four modules behind HTTP so n8n can call them as stages.
It lives in the same Railway project as n8n; no serverless timeout.

1. Railway → the project that hosts n8n → **New → GitHub Repo** → `Kai8karma/postevent-engine`, branch `v2-darwinbox`, root `/`.
   Railway detects the `Dockerfile` (`railway.json` pins the builder + `/health` check). The `Dockerfile` installs
   `ffmpeg` via apt — M3's `transcribe` phase (mono/16kHz audio extraction) and its clip cutting both need a real
   binary, not a Python package; nothing extra to configure in Railway itself.
2. **Variables:** `MODULE_API_TOKEN` (any long random string — the same value goes into n8n's variables),
   `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (optional comma chain), `HUBSPOT_TOKEN`, `SARVAM_API_KEY`,
   `PUBLIC_BASE_URL` (the service's public URL once generated). `PORT` is injected by Railway.
3. **Networking → Generate Domain.** Copy the URL into n8n's variables as `MODULE_API_URL`.
4. Smoke test:
   ```bash
   curl -s https://<service>.up.railway.app/health
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m1","live":true,"inputs":{"registrants_csv_path":"data/incoming/registrants.csv"}}'
   ```
   Expect `ok:true`, a `run_id`, an `artifacts` map, and `receipts` paths you can GET with the same bearer header.
5. Redeploy = push to `v2-darwinbox`. Run outputs live under `out/api/<run_id>/` in the container (ephemeral);
   anything that must persist is pushed to HubSpot / Drive by the modules themselves.
6. M3's three phases (`transcribe` → `run` → `record`, see `docs/module-api.md`'s M3 table):
   ```bash
   # transcribe: downloads event.json's recording_files (or inputs.audio_urls) if not already at
   # data/incoming/media/, extracts mono 16kHz audio with ffmpeg, runs Sarvam batch STT, writes transcript.md
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m3","phase":"transcribe","run_id":"m3-demo-1","inputs":{}}'

   # run: repurpose.py over the transcript -- reuses m3-demo-1's own transcript.md from the transcribe call
   # above (no transcript_md_path needed); options.clips/images opt in when repurpose.py's --help supports them
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m3","phase":"run","run_id":"m3-demo-1","live":true,"inputs":{"options":{"clips":true,"images":true}}}'

   # record: n8n's Google Drive upload results, written into drive_manifest.json + manifest.json.shared_drive
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m3","phase":"record","run_id":"m3-demo-1","inputs":{"drive":{"folder_url":"https://drive.google.com/…","folder_id":"abc123","files":[]}}}'
   ```
7. M4's four phases (`seed` → `sync` → `analyze` → `render`, see `docs/module-api.md`'s M4 table), all against
   one `run_id` so each phase reads the files the previous one wrote under `out/api/<run_id>/m4/`:
   ```bash
   # seed: writes this event's engagement stream + lifecycle changes INTO HubSpot for the tagged contacts
   # (custom behavioural events, or the postevent_* counter properties when the portal refuses definitions)
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m4","phase":"seed","run_id":"m4-demo-1","live":true,"inputs":{"event_slug":"darwinbox-ai-in-hr-2026-08-13"}}'

   # sync: reads the portal back -> snapshot.json + receipts/m4_hubspot_sync.json
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m4","phase":"sync","run_id":"m4-demo-1","live":true,"inputs":{"event_slug":"darwinbox-ai-in-hr-2026-08-13"}}'

   # analyze: LLM + deterministic validator over the snapshot -> analysis.json
   # (inputs.options.budget caps the phase's LLM calls; omit it for the module's own default)
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m4","phase":"analyze","run_id":"m4-demo-1","live":true,"inputs":{"options":{"budget":8}}}'

   # render: self-contained index.html + dashboard_data.json
   curl -s -X POST https://<service>.up.railway.app/run -H "Authorization: Bearer $MODULE_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"module":"m4","phase":"render","run_id":"m4-demo-1","live":true,"inputs":{}}'

   # narrative: read the current one, or ?refresh=1 to re-run analyze first (add &live=0 to refresh
   # on the offline lane -- zero network, zero spend)
   curl -s -H "Authorization: Bearer $MODULE_API_TOKEN" https://<service>.up.railway.app/narrative/m4-demo-1
   curl -s -H "Authorization: Bearer $MODULE_API_TOKEN" "https://<service>.up.railway.app/narrative/m4-demo-1?refresh=1"

   # the rendered dashboard, with window.NARRATIVE_ENDPOINT = "/narrative/m4-demo-1" injected before its
   # first <script> so the page refreshes its own narrative on load, same origin
   open https://<service>.up.railway.app/dashboard/m4-demo-1/
   ```
   `live:false` runs the same phases with `--offline` (no HubSpot writes/reads, no LLM calls). Every
   `summary` field is read straight out of the files the phase wrote; a file the phase did not write leaves
   its fields `null` and names itself in `notes`.
8. **`PUBLIC_DASHBOARD` (M4 only).** A browser cannot send a bearer header, so `GET /dashboard/<run_id>/`
   and `GET /narrative/<run_id>` answer **401 by default** — the page above only opens for someone holding
   the token. Setting the Railway variable `PUBLIC_DASHBOARD=1` drops the bearer requirement **on those two
   GET routes only**; `POST /run` and `GET /artifacts/...` stay bearer-only in either case. Leave it unset
   unless the dashboard link is meant to be publicly viewable — anyone with the run id can then read that
   run's narrative and rendered page (both carry contact-level data), and `?refresh=1` on a public
   `/narrative/<run_id>` re-runs `analyze` on the live lane, which spends real LLM credits per request.
9. **Vercel (console + dashboard only).** `modules/m4-dashboard/api/narrative.js` is now a thin proxy:
   `/api/narrative?run_id=<id>&refresh=1` → `${MODULE_API_URL}/narrative/<id>?refresh=1` with the bearer
   header attached server-side, 25 s timeout, module API JSON returned verbatim. Two project env vars,
   both names only here: **`MODULE_API_URL`** (the Railway URL from step 3) and **`MODULE_API_TOKEN`** (the
   same value as step 2). No model provider key belongs on Vercel any more — the proxy makes no model calls.
   With `MODULE_API_URL` unset the function answers `503 {"error":"MODULE_API_URL not configured"}`.
   Local check against a module API you are already running:
   ```bash
   MODULE_API_URL=http://127.0.0.1:8080 MODULE_API_TOKEN=$MODULE_API_TOKEN \
     node modules/m4-dashboard/api/_local_invoke.js m4-demo-1 --refresh --offline
   ```
