# Publishing M3 deliverables to a shared drive — production runbook

**This is now real code, not just a runbook**: `scripts/publish_deliverables.py`
implements both lanes below. Run it after any M3 run:

```bash
python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3
```

It copies the run's deliverables into a local "shared drive" directory
(`out/shared-drive/<event_tag>/`, tagged by event, with an `INDEX.md` —
zero network, zero credentials) and additionally
uploads to Google Drive via the exact mechanism documented below whenever
`GOOGLE_DRIVE_ACCESS_TOKEN` (or `--drive-token-env NAME`) holds a live OAuth
access token. No token → the Drive lane is skipped with a labelled reason in
`publish_manifest.json`, never faked. This document is the reference for the
exact Drive API shape that code calls.

**No Drive upload has run.** No Google Drive OAuth credential is connected
to this project, `out/receipts/m3-live/manifest.json`'s `shared_drive` block
is empty (`folder_url: null`, `folder_id: null`, `files: []`), and no Drive
receipt exists anywhere in the package. Everything below is the mechanism
`publish_deliverables.py` would call once a token exists — a specification,
not a record.

## What `publish_deliverables.py` does, after M3 writes the 6 local files

```
for each event run:
    1. Resolve or create the event folder
       GET  https://www.googleapis.com/drive/v3/files
            ?q=name='Post-Event Engine — {event_tag}' and mimeType='application/vnd.google-apps.folder' and trashed=false
       -> if 0 results, POST to create it (see step 2 with mimeType=folder, no content)
       -> if 1 result, reuse its id
       -> if >1 results, that's a bug (folder should be unique per event) — alert, don't silently pick one

    2. Upload each of the 6 assets into that folder id
       POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart
       (see request shape below)

    3. Compose and upload INDEX.md the same way (word counts + event metadata)

    4. Do NOT call the permissions.create endpoint — leave the file private
       to the service/user identity that authenticated. No public link,
       no share-by-email, unless a human explicitly requests it downstream.

    5. Record { folder_id, folder_webViewLink, [{name, id, webViewLink, size}] }
       into the run manifest's shared_drive block (empty on every run so far).
```

## Endpoint

- **Folder lookup:** `GET https://www.googleapis.com/drive/v3/files`
  with query param `q` as shown above, `fields=files(id,name)`.
- **Create (folder or file):**
  `POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart`

## OAuth scope

`https://www.googleapis.com/auth/drive.file` — the narrowest scope that
still allows creating files/folders and writing to files the app itself
created. Do not request `drive` (full account access) for this use case;
`drive.file` is sufficient because every file this pipeline touches is one
it created.

## Multipart upload shape

A Drive multipart upload is two parts in one HTTP body: a JSON metadata part
and the raw file content part, joined with a boundary string.

```
POST /upload/drive/v3/files?uploadType=multipart HTTP/1.1
Host: www.googleapis.com
Authorization: Bearer {access_token}
Content-Type: multipart/related; boundary=foo_bar_baz

--foo_bar_baz
Content-Type: application/json; charset=UTF-8

{
  "name": "darwinbox-ai-in-hr-2026-08-13 — blog.md",
  "parents": ["{folder_id}"],
  "mimeType": "text/markdown"
}

--foo_bar_baz
Content-Type: text/markdown

{... raw file bytes of blog.md ...}
--foo_bar_baz--
```

For the folder-creation call, the metadata part is just
`{"name": "Post-Event Engine — {event_tag}", "mimeType": "application/vnd.google-apps.folder"}`
with no second (content) part.

Response is a `File` resource; `id` and (with `fields=id,webViewLink` on the
request, or a follow-up `files.get`) `webViewLink` are what gets recorded in
the manifest.

## State: not run

To be explicit, because this page reads like a runbook: **nothing here has
been executed against Google Drive.** There is no Drive folder, no uploaded
file, no folder or file id, and no Drive receipt in this repo. The local
lane of `publish_deliverables.py` (copying deliverables into
`out/shared-drive/<event_tag>/` with an `INDEX.md`) has likewise not been
run for the committed M3 run — no such directory exists in the package. The
Drive upload itself is wired into the n8n M3 workflow
(`orchestrator/n8n/railway/m3-content-repurposing.json`), which has not been
imported into a running n8n instance either.
