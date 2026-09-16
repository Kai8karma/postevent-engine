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
