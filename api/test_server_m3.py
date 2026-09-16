#!/usr/bin/env python3
"""Zero-cost verification of api/server.py's M3 endpoints (transcribe/run/record).

Boots the real server (subprocess, MODULE_API_TOKEN=t) on a free port and hits
it over real HTTP with urllib (stdlib), same style as test_log_dispatch.py.

Scenarios:
  1. POST /run module=m3 phase=run live=false -- repurpose.py may or may not
     support --offline yet (it's being rewritten concurrently, see
     docs/module-api.md's M3 table) -- this only asserts the response JSON
     shape, never success. Zero LLM calls either way: live=false means this
     service never appends --live to repurpose.py's cmd.
  2. POST /run module=m3 phase=record against a hand-made run dir with a
     minimal manifest.json fixture -- asserts drive_manifest.json is written
     and manifest.json.shared_drive is merged in.
  3. GET /artifacts/<run>/<file>.mp4 against a hand-made fixture -- asserts
     Content-Type: video/mp4 (EXTRA_CONTENT_TYPES override, not relying on
     the container's system mime.types).
  4. POST /run with a wrong bearer token -- asserts 401.

Run: python3 api/test_server_m3.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t"

PASS_COUNT, FAIL_COUNT = 0, 0


def check(label, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {label}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {label}  {detail}")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, method="GET", token=None, body=None, timeout=30):
    """Returns (status, headers_dict, raw_bytes). Never raises on non-2xx --
    server.py's own /run contract always answers 200 with ok:false; only auth
    failures and malformed paths use real HTTP error codes, and this helper
    treats those uniformly too so callers just check `status`."""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def wait_for_server(base_url: str, timeout=20):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            status, _, _ = http(f"{base_url}/health")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not come up within {timeout}s: {last_exc}")


def scenario_run_phase(base_url: str):
    print("=== scenario 1: POST /run m3.run live=false -- assert JSON shape, not success ===")
    run_id = f"m3-servertest-run-{os.getpid()}"
    out_dir = REPO_ROOT / "out" / "api" / run_id
    try:
        status, _, raw = http(f"{base_url}/run", method="POST", token=TOKEN,
                               body={"module": "m3", "phase": "run", "run_id": run_id, "live": False, "inputs": {}})
        check("HTTP 200 (server.py answers /run with 200 even on ok:false)", status == 200, status)
        resp = json.loads(raw)
        # summary/artifacts/next only ever appear on ok:true (every phase in this
        # file follows the same convention -- an ok:false early-return carries just
        # ok/module/phase/error/log/notes, never fabricated empty placeholders for
        # the success-only fields).
        for key in ("ok", "module", "phase", "run_id", "notes", "log"):
            check(f"response has {key!r}", key in resp, resp)
        check("module == 'm3'", resp.get("module") == "m3", resp.get("module"))
        check("phase == 'run'", resp.get("phase") == "run", resp.get("phase"))
        check("run_id echoed back", resp.get("run_id") == run_id, resp.get("run_id"))
        if resp.get("ok"):
            for key in ("summary", "artifacts", "next"):
                check(f"ok:true response has {key!r}", key in resp, resp)
            summary = resp.get("summary", {})
            for key in ("blog_words", "chapters", "posts", "images", "clips", "grounding", "lane", "models"):
                check(f"ok:true summary has {key!r}", key in summary, summary)
        else:
            check("ok:false carries a non-empty error", bool(resp.get("error")), resp)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def scenario_record_phase(base_url: str):
    print("\n=== scenario 2: POST /run m3.record against a hand-made manifest.json fixture ===")
    run_id = f"m3-servertest-record-{os.getpid()}"
    out_dir = REPO_ROOT / "out" / "api" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_fixture = {"event_slug": "test-event", "generated_at": "2026-09-16T00:00:00Z",
                         "lane": "offline", "models": {}, "files": []}
    (out_dir / "manifest.json").write_text(json.dumps(manifest_fixture), encoding="utf-8")
    drive = {"folder_url": "https://drive.google.com/drive/folders/abc123", "folder_id": "abc123",
             "files": [{"name": "blog.md", "drive_file_id": "f1", "web_view_link": "https://drive.google.com/f1"}]}
    try:
        status, _, raw = http(f"{base_url}/run", method="POST", token=TOKEN,
                               body={"module": "m3", "phase": "record", "run_id": run_id, "inputs": {"drive": drive}})
        resp = json.loads(raw)
        check("HTTP 200", status == 200, status)
        check("ok:true", resp.get("ok") is True, resp)
        check("summary.files_recorded == 1", resp.get("summary", {}).get("files_recorded") == 1, resp.get("summary"))
        check("summary.folder_url matches", resp.get("summary", {}).get("folder_url") == drive["folder_url"], resp.get("summary"))

        drive_manifest_path = out_dir / "drive_manifest.json"
        check("drive_manifest.json written", drive_manifest_path.exists())
        if drive_manifest_path.exists():
            dm = json.loads(drive_manifest_path.read_text(encoding="utf-8"))
            check("drive_manifest.json.run_id matches", dm.get("run_id") == run_id, dm)
            check("drive_manifest.json.folder_id matches", dm.get("folder_id") == "abc123", dm)

        manifest_after = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        check("manifest.json.shared_drive merged in", manifest_after.get("shared_drive") == drive, manifest_after)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def scenario_artifact_mp4(base_url: str):
    print("\n=== scenario 3: GET /artifacts/<run>/clip.mp4 -- Content-Type: video/mp4 ===")
    run_id = f"m3-servertest-artifact-{os.getpid()}"
    out_dir = REPO_ROOT / "out" / "api" / run_id
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "clip1.mp4").write_bytes(b"not-a-real-mp4-just-fixture-bytes")
    try:
        status, headers, raw = http(f"{base_url}/artifacts/{run_id}/clips/clip1.mp4", token=TOKEN)
        check("HTTP 200", status == 200, status)
        check("Content-Type: video/mp4", headers.get("Content-Type") == "video/mp4", headers.get("Content-Type"))
        check("bytes round-trip", raw == b"not-a-real-mp4-just-fixture-bytes")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def scenario_bad_token(base_url: str):
    print("\n=== scenario 4: POST /run with a wrong bearer token -- 401 ===")
    status, _, raw = http(f"{base_url}/run", method="POST", token="wrong-token",
                           body={"module": "m3", "phase": "record", "inputs": {}})
    check("HTTP 401", status == 401, status)
    resp = json.loads(raw)
    check("ok:false on 401", resp.get("ok") is False, resp)


def main():
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["MODULE_API_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "api" / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_server(base_url)
        scenario_run_phase(base_url)
        scenario_record_phase(base_url)
        scenario_artifact_mp4(base_url)
        scenario_bad_token(base_url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if FAIL_COUNT and proc.stdout:
            print("\n--- server stdout/stderr (for debugging the failure above) ---")
            print(proc.stdout.read())

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
