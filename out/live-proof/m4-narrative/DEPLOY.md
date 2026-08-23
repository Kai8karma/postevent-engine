# Deploying the live narrative endpoint

`modules/m4-dashboard/api/narrative.js` is a Vercel Node serverless
function. It needs zero code changes to go live — just environment
variables on the Vercel project.

## Env vars to set (Vercel dashboard, Project → Settings → Environment
Variables — or the CLI one-liner below)

| Variable | Value |
|---|---|
| `OPENROUTER_API_KEY` | your OpenRouter key (the owner enters this themselves — never handled by this task) |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` |

Do **not** set `ANTHROPIC_API_KEY` unless you actually want the Anthropic
path (`narrative.js` prefers it over OpenRouter when both are present, and
that path was not exercised or measured tonight).

### Why `nemotron-3-super-120b-a12b:free`, not the `ultra-550b` model

Measured locally against the real production payload tonight
(`out/live-proof/m4-narrative/run_log.md`):

- `nvidia/nemotron-3-ultra-550b-a55b:free` — 29.9s and 35.2s across two
  runs. The dashboard client (`template.html`) aborts at 25000ms and shows
  the cached fallback narrative on timeout, so this model would miss the
  window on a live page load most of the time.
- `nvidia/nemotron-3-super-120b-a12b:free` — 17.1s, comfortably inside the
  25s window.

## One-line CLI to set both (Vercel CLI, run by the project owner)

```
vercel env add OPENROUTER_API_KEY production
vercel env add OPENROUTER_MODEL production
# when prompted for OPENROUTER_MODEL's value, paste: nvidia/nemotron-3-super-120b-a12b:free
```

(`vercel env add` prompts for the secret value interactively and does not
echo it back — that's why this is two commands, not one with the key inline
on the command line.)

After both vars are set, redeploy (`vercel --prod` or push to the connected
branch) and the "live" badge on the dashboard's narrative panel will start
resolving inside 25s instead of falling back to "Cached narrative."

This step was intentionally **not** performed by this task — no Vercel env
vars were set, no deployment was triggered, and the API key value was never
seen or printed. Setting these two vars is the entire remaining step to make
"one AI-generated narrative summary refreshed on load" live in production.
