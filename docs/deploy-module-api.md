# Deploy the module API on Railway

The module API (`api/server.py`) runs the four modules behind HTTP so n8n can call them as stages.
It lives in the same Railway project as n8n; no serverless timeout.

1. Railway → the project that hosts n8n → **New → GitHub Repo** → `Kai8karma/postevent-engine`, branch `v2-darwinbox`, root `/`.
   Railway detects the `Dockerfile` (`railway.json` pins the builder + `/health` check).
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
