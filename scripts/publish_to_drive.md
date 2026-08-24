# Publishing M3 deliverables to a shared drive — production runbook

**This is now real code, not just a runbook**: `scripts/publish_deliverables.py`
implements both lanes below. Run it after any M3 run:

```bash
python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3
```

It always copies the run's deliverables into a real local "shared drive"
(`out/shared-drive/<event_tag>/`, tagged by event, with an `INDEX.md` —
zero network, zero credentials, genuinely works every time) and additionally
uploads to Google Drive via the exact mechanism documented below whenever
`GOOGLE_DRIVE_ACCESS_TOKEN` (or `--drive-token-env NAME`) holds a live OAuth
access token — see the README's "Google Drive handoff" for how to mint one.
No token → the Drive lane is skipped with a labelled reason in
`publish_manifest.json`, never faked. This document remains the reference
for the exact Drive API shape that code calls.

Tonight's actual first Drive upload (`out/live-proof-drive/`) was done
through the Google Drive MCP connector (an authenticated session, not raw
HTTP), which was the right tool for a one-off interactive proof before this
script existed but not something a script should shell out to in
production. Below is the real mechanism `publish_deliverables.py` calls.

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
       to the run's manifest (mirrors out/live-proof-drive/drive_manifest.json).
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
  "name": "acmerevenue-2026-07-20 — blog.md",
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

## What was actually used tonight (not this)

Tonight's upload used the Google Drive MCP connector's `create_folder` /
`create_file` / `search_files` tools, authenticated as the operator's own
Google account via the existing MCP OAuth session — not a direct call to the
endpoint above. That is the honest record of tonight's mechanism; see
`out/live-proof-drive/README.md` and `out/live-proof-drive/drive_manifest.json`
for the receipt (folder + 7 file ids/links). The API shape documented above
is what a production integration in `repurpose.py` (or an n8n Google Drive
node in `orchestrator/n8n/**`) would call instead of depending on an
interactive MCP session.
