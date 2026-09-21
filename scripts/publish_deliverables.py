#!/usr/bin/env python3
"""publish_deliverables.py -- real "saved to a shared drive, tagged by
event" step for M3 (SPEC.md: "Saved to shared drive, tagged by event").

No Google Drive upload has ever run in this build: no OAuth credential is
connected, `manifest.json.shared_drive` is empty in every committed receipt,
and there is no Drive receipt anywhere in the package. This script is the code
that would perform one, with two lanes:

  1. LOCAL shared drive (always runs, zero network, zero credentials).
     Copies the M3 run's deliverables into
     `<out>/<event_tag>/<event_tag> -- <filename>` -- a real, verifiable,
     always-working "shared drive" on this machine -- and writes an
     INDEX.md + local_manifest.json next to them.

  2. Google Drive upload (runs only when an OAuth access token is present
     in the env var named by --drive-token-env, default
     GOOGLE_DRIVE_ACCESS_TOKEN). Implements the exact folder-lookup /
     multipart-upload shape documented in publish_to_drive.md via stdlib
     urllib -- no SDK. Skips with a labelled reason (not a failure) when no
     token is present; never fakes success.

Why local is the default and Drive is opt-in: Drive OAuth access tokens
expire hourly and require a 3-legged consent flow this stdlib-only,
non-interactive script correctly refuses to perform on its own (that would
mean handling a client secret and a redirect, well past "call an HTTP
API" scope) -- see scripts/publish_to_drive.md for how one would be minted. The local lane is a directory on this machine, not a shared
drive anyone else can open; it is shipped as proof the publish step and the
event tagging work, not as a substitute for the Drive leg, which has never run. Both lanes tag every file by event (folder/prefix = event_tag)
and neither ever makes a file public (no permissions.create call, ever).

Usage:
    python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3
    python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3 --dry-run
    GOOGLE_DRIVE_ACCESS_TOKEN=... python3 scripts/publish_deliverables.py \
        --m3-out out/<slug>/m3   # also uploads to Drive

Python 3 stdlib only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENT = REPO_ROOT / "data" / "incoming" / "event.json"
DEFAULT_SHARED_DRIVE = REPO_ROOT / "out" / "shared-drive"

DELIVERABLE_NAMES = ["blog.md", "youtube.md", "infographic.md", "social.md", "extraction.json", "manifest.json"]
MIME_BY_SUFFIX = {".md": "text/markdown", ".json": "application/json", ".png": "image/png"}

DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def die(msg: str, code: int = 1) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def collect_deliverables(m3_out: Path) -> list:
    """(name, path, mime) for every asset that actually exists in the M3
    run -- never invents a file that isn't there."""
    found = []
    for name in DELIVERABLE_NAMES:
        p = m3_out / name
        if p.exists():
            found.append((name, p, MIME_BY_SUFFIX.get(p.suffix, "application/octet-stream")))
    visuals_dir = m3_out / "visuals"
    if visuals_dir.is_dir():
        for png in sorted(visuals_dir.glob("*.png")):
            found.append((f"visuals/{png.name}", png, "image/png"))
    return found


def build_index_md(event_tag: str, event: dict, deliverables: list) -> str:
    lines = [
        f"# Post-Event Engine -- {event_tag}",
        "",
        f"**Event:** {event.get('event_name', '(unknown)')}",
        f"**Date:** {event.get('date', '(unknown)')}",
        f"**Host:** {event.get('host_company', '(unknown)')}",
        f"**Published:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "## Deliverables",
        "",
    ]
    for name, path, _mime in deliverables:
        size = path.stat().st_size
        words = f", {len(path.read_text(errors='replace').split())}w" if path.suffix in (".md",) else ""
        lines.append(f"- `{name}` -- {size}B{words}")
    return "\n".join(lines) + "\n"


def publish_local(event_tag: str, event: dict, deliverables: list, shared_drive_root: Path) -> dict:
    """The real, always-working lane: copies every deliverable into
    <shared_drive_root>/<event_tag>/, prefixed and tagged by event, exactly
    like the folder-per-event scheme documented for Drive -- just on local
    disk, where it needs no credentials to verify."""
    folder = shared_drive_root / event_tag
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, path, _mime in deliverables:
        safe_name = name.replace("/", "__")  # visuals/foo.png -> visuals__foo.png, one flat folder per event
        dest = folder / f"{event_tag} -- {safe_name}"
        dest.write_bytes(path.read_bytes())
        written.append({"name": dest.name, "source": str(path), "bytes": dest.stat().st_size})
    index_text = build_index_md(event_tag, event, deliverables)
    (folder / "INDEX.md").write_text(index_text)
    written.append({"name": "INDEX.md", "source": "composed (not a repo file)", "bytes": len(index_text.encode())})
    return {"status": "ok", "folder": str(folder), "files": written}


# --- Google Drive lane: real HTTP, only runs with a real access token ---

def _drive_token(env_name: str) -> str:
    return os.environ.get(env_name, "").strip()


def _drive_request(token: str, url: str, method: str = "GET", data: bytes = None, content_type: str = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Drive API HTTP {e.code} for {method} {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Drive API unreachable ({e.reason}) for {method} {url}") from None


def _build_multipart_related(metadata: dict, content_bytes: bytes, content_mime: str) -> tuple:
    """Two-part multipart/related body: JSON metadata + raw file bytes.
    Exact shape documented in publish_to_drive.md's 'Multipart upload
    shape' section."""
    boundary = uuid.uuid4().hex
    buf = bytearray()
    buf += f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
    buf += json.dumps(metadata).encode()
    buf += b"\r\n"
    if content_bytes is not None:
        buf += f"--{boundary}\r\nContent-Type: {content_mime}\r\n\r\n".encode()
        buf += content_bytes
        buf += b"\r\n"
    buf += f"--{boundary}--".encode()
    return bytes(buf), f"multipart/related; boundary={boundary}"


def find_or_create_drive_folder(token: str, event_tag: str) -> dict:
    folder_name = f"Post-Event Engine -- {event_tag}"
    q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    from urllib.parse import urlencode
    lookup_url = f"{DRIVE_FILES_API}?{urlencode({'q': q, 'fields': 'files(id,name,webViewLink)'})}"
    result = _drive_request(token, lookup_url)
    files = result.get("files", [])
    if len(files) > 1:
        raise RuntimeError(f"Drive folder '{folder_name}' is not unique ({len(files)} matches) -- alert, not auto-picked")
    if files:
        return files[0]
    body, content_type = _build_multipart_related(
        {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}, None, None)
    create_url = f"{DRIVE_UPLOAD_API}?uploadType=multipart&fields=id,name,webViewLink"
    return _drive_request(token, create_url, method="POST", data=body, content_type=content_type)


def upload_drive_file(token: str, folder_id: str, name: str, content_bytes: bytes, mime: str) -> dict:
    metadata = {"name": name, "parents": [folder_id], "mimeType": mime}
    body, content_type = _build_multipart_related(metadata, content_bytes, mime)
    url = f"{DRIVE_UPLOAD_API}?uploadType=multipart&fields=id,name,webViewLink,size"
    return _drive_request(token, url, method="POST", data=body, content_type=content_type)


def publish_drive(token: str, event_tag: str, event: dict, deliverables: list, index_text: str, dry_run: bool) -> dict:
    plan = {
        "folder_lookup_query": f"name='Post-Event Engine -- {event_tag}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        "files_to_upload": [f"{event_tag} -- {n.replace('/', '__')}" for n, _p, _m in deliverables] + ["INDEX.md"],
        "scope": DRIVE_SCOPE,
        "sharing": "private (no permissions.create call -- never made public)",
    }
    if dry_run:
        return {"status": "dry-run", "would_do": plan}
    try:
        folder = find_or_create_drive_folder(token, event_tag)
        folder_id = folder["id"]
        uploaded = []
        for name, path, mime in deliverables:
            safe_name = f"{event_tag} -- {name.replace('/', '__')}"
            f = upload_drive_file(token, folder_id, safe_name, path.read_bytes(), mime)
            uploaded.append({"name": safe_name, "source": str(path), "id": f.get("id"), "webViewLink": f.get("webViewLink")})
        f = upload_drive_file(token, folder_id, "INDEX.md", index_text.encode(), "text/markdown")
        uploaded.append({"name": "INDEX.md", "source": "composed", "id": f.get("id"), "webViewLink": f.get("webViewLink")})
        return {
            "status": "ok",
            "folder": {"id": folder_id, "name": folder.get("name"), "webViewLink": folder.get("webViewLink")},
            "files": uploaded,
        }
    except RuntimeError as e:
        return {"status": "failed", "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--m3-out", required=True, help="M3 run's output directory (blog.md, youtube.md, ...)")
    ap.add_argument("--event", default=str(DEFAULT_EVENT), help="event.json for name/date metadata")
    ap.add_argument("--out", default=str(DEFAULT_SHARED_DRIVE), help="local shared-drive root directory")
    ap.add_argument("--drive-token-env", default="GOOGLE_DRIVE_ACCESS_TOKEN",
                     help="env var holding a Drive OAuth access token (default: GOOGLE_DRIVE_ACCESS_TOKEN)")
    ap.add_argument("--dry-run", action="store_true",
                     help="skip the real Drive network calls (still writes the local copy + Drive request plan); local lane makes zero network calls regardless of this flag")
    args = ap.parse_args()

    m3_out = Path(args.m3_out)
    if not m3_out.is_dir():
        die(f"--m3-out not found or not a directory: {m3_out}")

    manifest_path = m3_out / "manifest.json"
    event_tag = None
    if manifest_path.exists():
        try:
            event_tag = json.loads(manifest_path.read_text()).get("event_tag")
        except json.JSONDecodeError:
            pass

    event = {}
    event_path = Path(args.event)
    if event_path.exists():
        try:
            event = json.loads(event_path.read_text())
        except json.JSONDecodeError:
            event = {}
    if not event_tag:
        domain = event.get("host_domain", "event").split(".")[0]
        event_tag = f"{domain}-{event.get('date', '')}".strip("-") or "event"

    deliverables = collect_deliverables(m3_out)
    if not deliverables:
        die(f"no deliverables found in {m3_out} (expected one of {DELIVERABLE_NAMES})")

    local_result = publish_local(event_tag, event, deliverables, Path(args.out))
    print(f"local shared drive: {len(local_result['files'])} file(s) -> {local_result['folder']}")

    token = _drive_token(args.drive_token_env)
    index_text = build_index_md(event_tag, event, deliverables)
    if not token:
        drive_result = {
            "status": "skipped",
            "reason": f"no OAuth access token in ${args.drive_token_env} -- Drive lane not attempted "
                      "(the local copy above is written; the Drive leg has never run in this build)",
        }
        print(f"Google Drive: SKIPPED -- {drive_result['reason']}")
    else:
        drive_result = publish_drive(token, event_tag, event, deliverables, index_text, args.dry_run)
        if drive_result["status"] == "ok":
            print(f"Google Drive: {len(drive_result['files'])} file(s) -> {drive_result['folder'].get('webViewLink')}")
        elif drive_result["status"] == "dry-run":
            print(f"Google Drive: dry-run -- would upload {len(drive_result['would_do']['files_to_upload'])} file(s), zero network calls made")
        else:
            print(f"Google Drive: FAILED -- {drive_result.get('error')}", file=sys.stderr)

    manifest = {
        "event_tag": event_tag,
        "event_name": event.get("event_name"),
        "event_date": event.get("date"),
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_m3_out": str(m3_out),
        "local": local_result,
        "drive": drive_result,
    }
    out_manifest_path = m3_out / "publish_manifest.json"
    out_manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"publish manifest -> {out_manifest_path}")

    return 1 if drive_result.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
