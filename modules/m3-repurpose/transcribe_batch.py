#!/usr/bin/env python3
"""
transcribe_batch.py — Sarvam saaras:v3 BATCH speech-to-text (job API v1) for long recordings.

Flow, one job per audio file, diarization + timestamps on:
    POST /speech-to-text/job/v1                      -> job_id
    POST /speech-to-text/job/v1/upload-files         -> presigned PUT URL per file
    PUT  <presigned url>                             (x-ms-blob-type: BlockBlob)
    POST /speech-to-text/job/v1/{job_id}/start
    GET  /speech-to-text/job/v1/{job_id}/status      until Completed / PartiallyCompleted / Failed
    POST /speech-to-text/job/v1/download-files       -> presigned GET URL per output (e.g. "0.json")

Usage:
    python3 transcribe_batch.py --audio a.mp3 [--audio b.mp3 ...] --out DIR
        [--language en-IN] [--num-speakers 2] [--api-key-env SARVAM_API_KEY]

Writes DIR/<stem>.sarvam.json per file and DIR/receipt.json (job ids, HTTP statuses,
timings, bytes) — the pipeline's transcription receipt. Fails loud. Stdlib only.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE = "https://api.sarvam.ai/speech-to-text/job/v1"

def http(method, url, key=None, body=None, headers=None, raw=None, timeout=120):
    h = {"api-subscription-key": key} if key else {}
    if headers: h.update(headers)
    data = raw
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def jload(b):
    try: return json.loads(b.decode() or "{}")
    except Exception: return {"_raw": b[:500].decode(errors="replace")}

def url_for(payload, filename):
    """Find the presigned URL for `filename` in whatever shape the API returns."""
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == filename and isinstance(v, str) and v.startswith("http"): return v
                if isinstance(v, dict) and (v.get("file_name") == filename or v.get("filename") == filename):
                    for vv in v.values():
                        if isinstance(vv, str) and vv.startswith("http"): return vv
                r = walk(v)
                if r: return r
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, dict) and (v.get("file_name") == filename or v.get("filename") == filename):
                    for vv in v.values():
                        if isinstance(vv, str) and vv.startswith("http"): return vv
                r = walk(v)
                if r: return r
        return None
    u = walk(payload)
    if not u:
        # single-file job: accept the only URL present
        urls = []
        def collect(o):
            if isinstance(o, str) and o.startswith("http"): urls.append(o)
            elif isinstance(o, dict): [collect(v) for v in o.values()]
            elif isinstance(o, list): [collect(v) for v in o]
        collect(payload)
        u = urls[0] if len(urls) == 1 else None
    return u

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--language", default="en-IN")
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--api-key-env", default="SARVAM_API_KEY")
    ap.add_argument("--poll-seconds", type=float, default=8.0)
    ap.add_argument("--max-wait", type=float, default=1800)
    a = ap.parse_args()
    key = os.environ.get(a.api_key_env)
    if not key: sys.exit(f"FAIL: {a.api_key_env} not set")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    receipt = {"provider": "sarvam", "model": "saaras:v3", "endpoint": BASE, "files": []}
    for audio in a.audio:
        p = Path(audio); t0 = time.time(); rec = {"file": p.name, "bytes": p.stat().st_size}
        params = {"model": "saaras:v3", "language_code": a.language, "with_timestamps": True, "with_diarization": True}
        if a.num_speakers: params["num_speakers"] = a.num_speakers
        st, b = http("POST", BASE, key, body={"job_parameters": params}); init = jload(b); rec["init_status"] = st
        if st >= 300 or "job_id" not in init: sys.exit(f"FAIL init {st}: {init}")
        job = init["job_id"]; rec["job_id"] = job
        st, b = http("POST", f"{BASE}/upload-files", key, body={"job_id": job, "files": [p.name]}); up = jload(b); rec["upload_links_status"] = st
        put_url = url_for(up, p.name) if st < 300 else None
        if not put_url: sys.exit(f"FAIL upload-links {st}: {json.dumps(up)[:400]}")
        st, b = http("PUT", put_url, raw=p.read_bytes(), headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "audio/mpeg"}, timeout=600)
        rec["upload_status"] = st
        if st >= 300: sys.exit(f"FAIL upload {st}: {b[:300]}")
        st, b = http("POST", f"{BASE}/{job}/start", key, body={"job_id": job}); rec["start_status"] = st
        if st >= 300: sys.exit(f"FAIL start {st}: {jload(b)}")
        state, s = None, {}
        while time.time() - t0 < a.max_wait:
            st, b = http("GET", f"{BASE}/{job}/status", key); s = jload(b); state = s.get("job_state")
            if state in ("Completed", "PartiallyCompleted", "Failed", "Cancelled"): break
            time.sleep(a.poll_seconds)
        rec["job_state"] = state; rec["seconds"] = round(time.time() - t0, 1)
        if state not in ("Completed", "PartiallyCompleted"): sys.exit(f"FAIL job {job} state={state}: {json.dumps(s)[:400]}")
        names = [o["file_name"] for d in s.get("job_details", []) for o in d.get("outputs", []) if o.get("file_name")]
        if not names: sys.exit(f"FAIL: no outputs listed in status: {json.dumps(s)[:400]}")
        st, b = http("POST", f"{BASE}/download-files", key, body={"job_id": job, "files": names}); dl = jload(b); rec["download_links_status"] = st
        got = []
        for n in names:
            u = url_for(dl, n)
            if not u: sys.exit(f"FAIL download-links {st}: {json.dumps(dl)[:400]}")
            st2, b2 = http("GET", u)
            if st2 >= 300: sys.exit(f"FAIL download {n} {st2}")
            dest = out / f"{p.stem}.sarvam.json"; dest.write_bytes(b2); got.append(str(dest))
        rec["outputs"] = got; receipt["files"].append(rec); print(json.dumps(rec))
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(f"receipt: {out / 'receipt.json'}")

if __name__ == "__main__": main()
