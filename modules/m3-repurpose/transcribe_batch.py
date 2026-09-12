#!/usr/bin/env python3
"""
transcribe_batch.py — Sarvam saaras:v3 BATCH speech-to-text for long recordings.

Batch flow (one job per audio file, diarization + timestamps supported):
    init  -> PUT audio to the returned Azure Blob SAS path
          -> start job -> poll status -> list + download output JSON.

Usage:
    python3 transcribe_batch.py --audio a.mp3 [--audio b.mp3 ...] --out DIR
        [--language en-IN] [--num-speakers 2] [--api-key-env SARVAM_API_KEY]

Writes per file: DIR/<stem>.sarvam.json (raw output) and one DIR/receipt.json
with job ids, timings, byte counts and HTTP statuses — the pipeline's receipt.
Fails loud on a missing key or any non-2xx. Stdlib only.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error, urllib.parse
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
            payload = r.read(); return r.status, payload
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def jload(b):
    try: return json.loads(b.decode() or "{}")
    except Exception: return {"_raw": b[:500].decode(errors="replace")}

def sas_join(container_sas: str, name: str) -> str:
    base, _, q = container_sas.partition("?")
    return f"{base.rstrip('/')}/{urllib.parse.quote(name)}?{q}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--language", default="en-IN")
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--api-key-env", default="SARVAM_API_KEY")
    ap.add_argument("--poll-seconds", type=float, default=10.0)
    ap.add_argument("--max-wait", type=float, default=1800)
    a = ap.parse_args()
    key = os.environ.get(a.api_key_env)
    if not key: sys.exit(f"FAIL: {a.api_key_env} not set")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    receipt = {"provider": "sarvam", "model": "saaras:v3", "endpoint": BASE, "files": []}
    for audio in a.audio:
        p = Path(audio); t0 = time.time(); rec = {"file": p.name, "bytes": p.stat().st_size}
        cfg = {"model": "saaras:v3", "with_timestamps": True, "with_diarization": True, "language_code": a.language}
        if a.num_speakers: cfg["num_speakers"] = a.num_speakers
        st, b = http("POST", f"{BASE}/initialise", key, body=cfg); init = jload(b); rec["init_status"] = st
        if st >= 300 or "job_id" not in init: sys.exit(f"FAIL init {st}: {init}")
        job = init["job_id"]; rec["job_id"] = job
        st, b = http("PUT", sas_join(init["input_storage_path"], p.name), raw=p.read_bytes(),
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "audio/mpeg"}, timeout=600)
        rec["upload_status"] = st
        if st >= 300: sys.exit(f"FAIL upload {st}: {b[:300]}")
        st, b = http("POST", f"{BASE}/{job}/start", key, body={"job_id": job}); rec["start_status"] = st
        if st >= 300: sys.exit(f"FAIL start {st}: {jload(b)}")
        state = None
        while time.time() - t0 < a.max_wait:
            st, b = http("GET", f"{BASE}/{job}/status", key); s = jload(b); state = s.get("job_state")
            if state in ("Completed", "Failed", "Cancelled"): break
            time.sleep(a.poll_seconds)
        rec["job_state"] = state; rec["seconds"] = round(time.time() - t0, 1)
        if state != "Completed": sys.exit(f"FAIL job {job} state={state}: {s}")
        # output container: list blobs, download the json(s)
        outp = init["output_storage_path"]; base, _, q = outp.partition("?")
        st, b = http("GET", f"{base}?restype=container&comp=list&{q}")
        names = [n for n in __import__("re").findall(r"<Name>([^<]+)</Name>", b.decode(errors='replace'))]
        if st >= 300 or not names:
            # fall back: outputs named <input_file_id>.json — the status payload names them
            names = [o["file_name"] for d in s.get("job_details", []) for o in d.get("outputs", [])]
        got = []
        for n in names:
            st2, b2 = http("GET", sas_join(outp, n))
            if st2 < 300 and n.endswith(".json"):
                dest = out / f"{p.stem}.sarvam.json"; dest.write_bytes(b2); got.append(str(dest))
        rec["outputs"] = got
        if not got: sys.exit(f"FAIL: job {job} completed but no output json fetched (names={names})")
        receipt["files"].append(rec); print(json.dumps(rec))
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(f"receipt: {out/'receipt.json'}")

if __name__ == "__main__": main()
