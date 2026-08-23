# M3 shared-drive leg — live proof

This directory documents the "all deliverables saved to a shared drive and tagged
by event" requirement from M3's spec. Until this run, M3 wrote its outputs to
`out/<run>/m3/` locally and made them browsable on the hosted site, but nothing
was ever pushed to a shared drive. This closes that gap for real, against the
user's actual Google Drive (via the Drive MCP connector), not a simulation.

## What happened

1. Searched Drive for an existing folder named
   `Post-Event Engine — acmerevenue-2026-07-20` — none existed.
2. Created that folder fresh. Folder id `1HDEuOF3tFieIaduGJxY7E35M9HCMVprt`,
   link: https://drive.google.com/drive/folders/1HDEuOF3tFieIaduGJxY7E35M9HCMVprt
3. Uploaded the 6 M3 deliverables from the LIVE run
   (`out/live-proof/m3/`: blog.md, youtube.md, infographic.md, social.md,
   extraction.json, manifest.json), each renamed to carry the event tag,
   e.g. `acmerevenue-2026-07-20 — blog.md`.
4. Composed and uploaded a 7th file, `acmerevenue-2026-07-20 — INDEX.md`,
   listing every asset, its word count (per the pipeline's own manifest.json),
   and the event metadata (name, date, tag).
5. Left sharing at the default — private to the account owner. No file was
   made public and no file was shared with any email address. This was a
   passive default, not an action: `share_file` was never called on any of
   these 7 files.
6. Wrote the full receipt — folder id/link plus per-file id/name/link/size —
   to `drive_manifest.json` in this same directory.

Full manifest: `out/live-proof-drive/drive_manifest.json`.

## How production would do this automatically

Tonight's upload was done interactively through the Google Drive MCP
connector (`create_folder`/`create_file`/`search_files`), authenticated as
the operator's own Google account. That is a legitimate one-off proof, but
it is not how a production pipeline should push assets — a production run
should not depend on an interactive MCP session.

Two real automation paths exist, both already implied by this repo's own
architecture:

- **n8n Google Drive node** — the orchestrator (`orchestrator/n8n/**`, out of
  scope for this change) already runs the M1→M4 pipeline. A Google Drive
  node dropped after the M3 step would take the same 6 files M3 already
  writes to `out/<run>/m3/`, resolve (or create) the tagged folder by event,
  and upload each file with the OAuth2 credential n8n already manages —
  no bespoke code, just a node in the existing workflow.
- **Drive API call directly from `repurpose.py`** — the module that produces
  these deliverables could call the Drive API's `files.create` (multipart
  upload) itself, right after it writes each file to disk. Exact mechanism
  is documented in `scripts/publish_to_drive.md` at the repo root: endpoint,
  required OAuth scope, and the multipart request shape.

Either path is a small, bounded addition — the hard part (naming convention,
event tagging, default-private sharing, one-folder-per-event dedup via
`search_files` before create) is already proven out by tonight's manual run
and captured in `drive_manifest.json`.
