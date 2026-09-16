# M3 — Content Repurposing

Transcript + recording in, multi-format content package out. `repurpose.py` runs **live by
default** — real OpenRouter calls, budget-capped. `--live` is accepted as a no-op for
compatibility.

## 1. What M3 produces

| Brief output | File(s) | Spec |
|---|---|---|
| Blog draft | `blog.md` | 800–1200 words, hard gate |
| YouTube chapters + description + thumbnail brief | `youtube.md` | `## Chapters` / `## Description` / `## Thumbnail Brief` |
| Infographic outline | `infographic.md` | `## Headline Options` / `## Data Points` (6–8 points) / `## Layout` |
| Social posts | `social.md` | 5–10 `### Post` blocks (`prompts/social.md` currently asks for exactly 8: 5 LinkedIn + 3 X, one hook style each, never used twice) |
| Clips, 16:9 + 9:16, burned captions | `clips/*.mp4` + `*.srt` | top 3 moments, `--clips` (ffmpeg) |
| Thumbnail + 2 social images | `visuals/*.png` | `--images` (real image model) |
| Saved to shared drive, tagged by event | not written by this script | the n8n M3 workflow uploads `manifest.json`'s files to a Drive folder named `Post-Event/<event_slug>/<run_id>`; the module API's `record` phase then writes the folder/file ids back into `manifest.json.shared_drive` |

## 2. How the AI is the engine

1. **Extraction pass** (`prompts/extraction.md`): one call over the full transcript returns
   moments (30–60s clip candidates, `clip_worthiness` 0–1), insights, verbatim quotes, data
   points. Schema-validated (`validate_extraction`, one corrective retry) and then run through
   `normalise_extraction()`: every timestamp is snapped onto a real transcript turn, every
   speaker is replaced with whoever the transcript actually shows at that timestamp
   (`speaker_corrections`), and any moment/quote/data point that doesn't verify is dropped
   (`dropped`) — so asset prompts never see ungrounded material to begin with. Minimums: 6
   moments, 5 insights, 8 quotes, 6 data points; after normalisation at least 3 quotes and 3
   moments must survive or the extraction itself fails.
2. **Per-asset prompts** (`prompts/blog.md`, `youtube.md`, `infographic.md`, `social.md`): each
   filled with `TRANSCRIPT`, `EVENT_JSON`, `CHAPTERS`, `VALID_TIMESTAMPS`, and the verified
   extraction. Each asset is checked against `check_asset_spec()` before it's written; on
   failure the specific failure reason is appended to the prompt and the whole asset is
   regenerated once (`MAX_SPEC_ATTEMPTS = 2`). `blog.md` is a hard gate: still out of range after
   the retry raises and fails the run (the rejected draft is left on disk, stamped `REJECTED`,
   for inspection). The other three assets ship anyway if still out of spec, with a loud stderr
   warning and `within_spec: false` recorded in the manifest.
3. **Grounding verifier** (`verify_grounding.py`): after generation, every asset is re-checked
   against the transcript — timestamp claims (including YouTube chapter markers and both ends of
   a `[MM:SS-MM:SS]` range) must be a real transcript turn start or a declared segment/chapter
   boundary; quoted strings ≥25 chars attributed to a named speaker must fuzzy-match
   (`difflib.SequenceMatcher` ratio ≥ 0.90) a transcript window; `— Name, Title, Company`
   attributions must name a real event speaker (ratio ≥ 0.85); `infographic.md` additionally
   checks that every `Stat N` referenced in `## Layout` exists in `## Data Points`. This is a
   hard gate: a failing report first gets one corrective regeneration per failing asset if LLM
   budget remains (`repair_grounding`), and if it still fails, `run()` raises and the run fails —
   the drafts stay on disk but are not shipped.
4. **Call budget**: `--budget` (default `LLM_CALL_BUDGET = 8`) caps completions —1 extraction + 4
   assets + spec-gate/grounding retries all draw from it. A separate `transient_allowance` (4)
   caps failed HTTP attempts (timeouts, 429/5xx) before the run gives up rather than hammering a
   flaky provider. Every attempt, successful or not, is one row in `receipts/m3_llm_calls.json`.

## 3. Inputs

- `--transcript` (default `data/incoming/transcript.md`) — `[MM:SS] **Speaker:** text` turns.
- `--event` (default `data/incoming/event.json`) — title, speakers, `recording_files` (chapters).
- `--sarvam-dir` (default `out/receipts/transcription`) — per-chapter `chapter<N>.sarvam.json`
  (Sarvam diarized turn boundaries); used for clip in/out points and captions, not for the text
  assets.
- `--media-dir` (default `data/incoming/media`) — local `chapter<N>.mp4` files; `make_clips.py`
  downloads a missing one from `event.json`'s `recording_files[].url`.

## 4. CLI

Live is the default lane; every flag below is from `python3 repurpose.py --help`.

- `--out OUT` (required) — output directory.
- `--transcript TRANSCRIPT` — path to the transcript (see Inputs).
- `--event EVENT` — path to event.json (see Inputs).
- `--sarvam-dir SARVAM_DIR` — per-chapter Sarvam JSON for clip cuts + captions.
- `--media-dir MEDIA_DIR` — local chapter MP4s, downloaded when absent.
- `--offline` — replay `sample_output/` instead of calling the model; only runs if
  `sample_output/.fingerprint.json` matches this transcript+event's SHA-256, otherwise fails.
- `--live` — accepted for compatibility; live is already the default, this is a no-op.
- `--live-dry-run` — build and print every live prompt from real data; zero network calls.
- `--quiet-prompts` — with `--live-dry-run`, print only the per-prompt headers.
- `--clips` — cut the top 3 moments into 16:9 + 9:16 clips (ffmpeg).
- `--images` — generate thumbnail + 2 social visuals via an image model.
- `--budget BUDGET` — LLM call ceiling (default 8).
- `--no-refresh-sample` — do not overwrite `sample_output/` with this run's output.
- `--allow-stale` — retired; accepted so older callers don't crash, no longer bypasses anything.
- `--strict-grounding` — retired; accepted for compatibility, grounding is always a hard gate now.

Env vars read by `repurpose.py`: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`,
`LLM_BATCH_DEADLINE_S`, `LLM_REASONING` (names only; `OPENROUTER_API_KEY` falls back to a line in
`~/.config/postevent/llm.env` when the env var is unset).

## 5. Outputs

```
<out>/
├── extraction.json
├── blog.md / youtube.md / infographic.md / social.md   (each stamped <!-- event: <slug> | generated: <mode> -->)
├── grounding_report.json
├── manifest.json
├── receipts/
│   ├── m3_llm_calls.json
│   ├── raw/*.txt              (every raw completion, for inspection)
│   ├── m3_images.json         (--images)
│   └── m3_clips.json          (--clips)
├── visuals/*.png               (--images; only the ones that actually generated)
├── clips/
│   ├── clip<N>-ch<C>-16x9.mp4, clip<N>-ch<C>-9x16.mp4, clip<N>-ch<C>.srt, clip<N>-ch<C>-thumb.png
│   └── clip<N>-ch<C>-captions/*.png   (intermediate caption strips)
└── dry-run/*.prompt.md         (--live-dry-run only, replaces everything above)
```
`--offline` writes the same top-level names, stamped `offline-replay`; visuals and clips are
live-only and are not replayed.

`manifest.json`, per `docs/module-api.md`'s M3 contract: `{event_slug, generated_at, lane,
models: {text, image}, files: [{name, kind, bytes, source, grounded}], shared_drive: {folder_url,
folder_id, files}}`. `repurpose.py`'s manifest also carries `summary`, `spec_check`, `grounding`,
`llm`, `notes`, plus `assets`/`llm_calls_made` kept for an existing caller's back-compat — these
extra top-level keys are not part of the documented contract.

Provenance: `source` is `llm` for the four text assets and `extraction.json`, `image_model` for
`visuals/*.png`, `ffmpeg` for `clips/*.mp4`, `sarvam` for `clips/*.srt`. `grounded` is `true`/`false`
per text asset once the grounding report exists (`null` before it runs), `true` for clips/captions,
and `null` for visuals and `grounding_report.json` itself.

## 6. Clips (`make_clips.py`)

Picks the top `--top` (default 3) moments from `extraction.json` by `clip_worthiness`, restricted
to moments whose `chapter` has a matching recording file. For each: the requested window is
snapped onto real Sarvam turn boundaries (`snap_window`) and forced to 30–60s inside one chapter.
The 16:9 cut is a plain ffmpeg re-encode of that window. The 9:16 cut runs `cropdetect` to find the
real picture area inside the source's own letterbox, then either centre-crops it or — for a
side-by-side two-speaker layout (content box wider than ~2.2:1) — rebuilds it as the two speaker
tiles stacked vertically (trimming each tile's bottom band, which carries the source's own burned
captions), scaled to 1080×1920. Captions come from the Sarvam diarized entries (chunk-level
timings, line starts/ends interpolated within a chunk by character count), written as a standalone
`.srt` for every clip and burned into the 9:16 cut only, as Pillow-rendered PNG strips composited
with ffmpeg's `overlay`+`enable` (this ffmpeg build has no `libass`/`subtitles` or
`drawtext` filter). Requires `ffmpeg` and `ffprobe` on `PATH` (fails loud if missing) and Pillow
for caption rendering. Fails the run if any ffmpeg invocation exits non-zero or any output lands
outside 30–60s; warns (does not fail) if the run's total clip size exceeds ~60MB.

## 7. Images (`gen_visuals.py`)

Discovers image-capable OpenRouter models (`GET /api/v1/models`, filtered on
`architecture.output_modalities` containing `"image"`), preferring `google/gemini-2.5-flash-image`
or an `OPENROUTER_IMAGE_MODEL` override, else the cheapest-priced candidate. Generates three
images grounded in this run's own extraction + `youtube.md`'s Thumbnail Brief:
`youtube-thumbnail.png` (16:9), `social-square.png` (1:1), `social-portrait.png` (4:5). Prompts
explicitly ask for no text/lettering in the image (image models garble baked-in text); the copy
itself lives in `youtube.md`/`infographic.md` for a downstream overlay step, not in this script.
On HTTP 402 (insufficient credits): if the error names an affordable token ceiling ≥900 tokens,
the same model is retried once capped to that ceiling; otherwise generation falls through to the
next discovered model. If every candidate fails for a given image, that file is simply not
written — the failure (model, HTTP status, error) is recorded in `receipts/m3_images.json` and no
placeholder or template image is emitted in its place. `IMAGE_DEADLINE_S` sets the per-call
timeout (default 180s).

## 8. Tests

`test_spec_gate.py` — zero-network, mocks `call_llm` to exercise `run_live()`'s regenerate-on-fail
loop: an over-length `blog.md` regenerates once and passes; a `blog.md` that never fits spec fails
the whole run after `MAX_SPEC_ATTEMPTS` (hard gate); a `youtube.md` missing a required section hits
the same gate; an unlisted chapter timestamp and a duplicated social hook style are rejected
directly via `check_asset_spec()`; the real (unmocked) `--offline` path is also checked to confirm
it still populates the spec-gate result with zero LLM calls. Run: `python3 test_spec_gate.py`.

`test_manifest.py` — validates an already-written `manifest.json` against `docs/module-api.md`'s M3
contract: required top-level keys, `lane` is `live`/`offline`, a non-empty `files` list where every
entry has `name`/`kind`/`bytes`/`source`/`grounded` and exists on disk at the stated byte size, the
four channel kinds are present, text assets carry a boolean `grounded`, visuals only claim a visual
source, `shared_drive` starts empty, and the LLM receipt exists with `calls_made <= budget`. Run:
`python3 test_manifest.py <run_dir>` (defaults to `out/verify-w3/m3`).

## 9. Module API / n8n

`api/server.py`'s `POST /run` wraps this module as `module: "m3"` with phases `transcribe` (
`transcribe_batch.py` + `tools/build_transcript.py` → `transcript.md`), `run` (this script), and
`record` (writes n8n's Drive upload results into `manifest.json.shared_drive`) — full request/
response shapes in `docs/module-api.md`, section "M3 — phases and files (W3)". The n8n workflow
`m3-content-repurposing.json` calls all three phases in sequence from one webhook
(`POST /webhook/m3-run`, body `{event_slug, transcribe?, images?, clips?}`), then creates a Drive
folder and uploads `next.files`; full node list and body shapes in
`orchestrator/n8n/railway/README.md`, section "§M3 — Content Repurposing (Railway)".
