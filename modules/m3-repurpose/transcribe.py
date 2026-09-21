#!/usr/bin/env python3
"""
transcribe.py — real speech-to-text lane for M3 (repurposing).

Closes the gap the assignment names but the rest of the repo never built:
M3's tool list says "Transcription API" but until this file existed, M3 only
ever consumed a pre-written transcript.md. This script takes an actual audio
file and produces a real transcript via the Sarvam saaras:v3 STT API.

Usage:
    python3 transcribe.py --audio <file.wav> --out <dir> [options]

Required:
    --audio PATH        Audio file (WAV, PCM). For other formats, convert
                         first: ffmpeg -i in.mp3 -ar 16000 -ac 1 out.wav
    --out DIR            Output directory (created if missing). Writes
                         transcript.md, transcription_meta.json, run_log.txt.

Options:
    --language CODE       BCP-47 code or 'unknown' to auto-detect (default: en-IN)
    --mode MODE            transcribe | translate | verbatim | translit | codemix
    --chunk-seconds N      Max seconds per API call (default: 28; API hard
                            caps the sync endpoint at 30s, we leave margin)
    --api-key-env NAME     Env var holding the Sarvam API key (default: SARVAM_API_KEY)
    --compare PATH         Plain-text file with the "known good" source segment.
                            If given, records a difflib word-level similarity
                            ratio against the STT output in transcription_meta.json.
    --dry-run              Print the request plan (chunk count/boundaries,
                            endpoint, model, language) and exit 0. Makes ZERO
                            network calls.

Failure behavior (by design, not a bug):
    - Missing/empty API key            -> exit 1, no network call attempted.
    - Non-WAV / unreadable audio        -> exit 1, no network call attempted.
    - Network error / non-200 response  -> exit 1, prints the exact error.
    There is no fallback to a fixture transcript. A failed run produces no
    transcript.md and a non-zero exit code — never a fabricated result.

Verification status: --dry-run and the fail-loud missing-key path are both
verified in this repo's dev machine (no SARVAM_API_KEY available here); the
real network call to Sarvam's saaras:v3 endpoint has not been exercised
from this exact machine/script -- it was exercised via the Sarvam MCP tool
in an earlier interactive session instead (see README.md's Transcription
lane section). The transcript this build ships came from a real Sarvam
batch run -- see out/receipts/transcription.json. Swap in a real key
(--api-key-env or the SARVAM_API_KEY default) to close that gap.
"""
import argparse
import difflib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave

API_URL = "https://api.sarvam.ai/speech-to-text"
MODEL = "saaras:v3"


def die(msg: str, code: int = 1) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def read_wav_info(path: str):
    """Return (duration_sec, n_channels, sampwidth, framerate) for a PCM WAV file."""
    try:
        with wave.open(path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            duration = frames / float(rate) if rate else 0.0
            return duration, channels, sampwidth, rate
    except wave.Error as e:
        die(
            f"'{path}' is not a readable PCM WAV file ({e}). "
            f"Convert it first, e.g.: ffmpeg -i input.ext -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
        )
    except FileNotFoundError:
        die(f"audio file not found: {path}")


def slice_wav(path: str, out_path: str, start_sec: float, end_sec: float) -> None:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        w.setpos(int(start_sec * rate))
        n_frames = int((end_sec - start_sec) * rate)
        frames = w.readframes(n_frames)
        with wave.open(out_path, "wb") as out:
            out.setnchannels(w.getnchannels())
            out.setsampwidth(w.getsampwidth())
            out.setframerate(rate)
            out.writeframes(frames)


def build_multipart(fields: dict, file_field: str, file_path: str, file_bytes: bytes):
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    def write(s):
        buf.write(s.encode("utf-8") if isinstance(s, str) else s)

    for name, value in fields.items():
        write(f"--{boundary}\r\n")
        write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        write(f"{value}\r\n")

    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="{file_field}"; filename="{os.path.basename(file_path)}"\r\n')
    write("Content-Type: audio/wav\r\n\r\n")
    buf.write(file_bytes)
    write("\r\n")
    write(f"--{boundary}--\r\n")

    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def call_stt(chunk_path: str, api_key: str, language_code: str, mode: str) -> dict:
    with open(chunk_path, "rb") as f:
        audio_bytes = f.read()

    body, content_type = build_multipart(
        fields={"model": MODEL, "language_code": language_code, "mode": mode},
        file_field="file",
        file_path=chunk_path,
        file_bytes=audio_bytes,
    )

    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "api-subscription-key": api_key,
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        die(f"Sarvam STT API returned HTTP {e.code} for {chunk_path}: {detail}")
    except urllib.error.URLError as e:
        die(f"Sarvam STT API unreachable ({e.reason}) for {chunk_path}. Check network/DNS.")


def main():
    ap = argparse.ArgumentParser(description="Real STT transcription lane for M3.")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--language", default="en-IN")
    ap.add_argument("--mode", default="transcribe",
                     choices=["transcribe", "translate", "verbatim", "translit", "codemix"])
    ap.add_argument("--chunk-seconds", type=float, default=28.0)
    ap.add_argument("--api-key-env", default="SARVAM_API_KEY")
    ap.add_argument("--compare", default=None, help="Path to reference text for similarity scoring")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    duration, channels, sampwidth, rate = read_wav_info(args.audio)

    n_chunks = max(1, int(duration // args.chunk_seconds) + (1 if duration % args.chunk_seconds > 0.01 else 0))
    boundaries = []
    t = 0.0
    while t < duration:
        end = min(t + args.chunk_seconds, duration)
        boundaries.append((round(t, 2), round(end, 2)))
        t = end

    if args.dry_run:
        print("DRY RUN — no network calls will be made.")
        print(f"  endpoint       : POST {API_URL}")
        print(f"  model          : {MODEL}")
        print(f"  mode           : {args.mode}")
        print(f"  language_code  : {args.language}")
        print(f"  api_key_env    : {args.api_key_env} ({'set' if os.environ.get(args.api_key_env) else 'NOT SET'})")
        print(f"  audio          : {args.audio}")
        print(f"  duration_sec   : {duration:.2f}  (channels={channels}, sampwidth={sampwidth}, rate={rate})")
        print(f"  chunk_seconds  : {args.chunk_seconds}")
        print(f"  planned_chunks : {len(boundaries)}")
        for i, (s, e) in enumerate(boundaries, 1):
            print(f"    chunk {i}: {s}s -> {e}s")
        print(f"  output_dir     : {args.out}")
        sys.exit(0)

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        die(
            f"{args.api_key_env} is not set (or empty). Refusing to fall back to a "
            f"fixture transcript. Export {args.api_key_env}=<your Sarvam key> and re-run."
        )

    os.makedirs(args.out, exist_ok=True)
    run_log = []

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        run_log.append(line)

    log(f"audio={args.audio} duration={duration:.2f}s chunks={len(boundaries)} mode={args.mode} lang={args.language}")

    wall_start = time.time()
    transcript_parts = []
    tmp_dir = os.path.join(args.out, "_chunks_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    for i, (s, e) in enumerate(boundaries, 1):
        chunk_path = os.path.join(tmp_dir, f"chunk_{i:02d}.wav")
        slice_wav(args.audio, chunk_path, s, e)
        log(f"chunk {i}/{len(boundaries)} [{s}s-{e}s] -> POST {API_URL}")
        result = call_stt(chunk_path, api_key, args.language, args.mode)
        text = result.get("transcript", "")
        if not text:
            die(f"chunk {i} returned an empty transcript from a 200 response: {result}")
        transcript_parts.append(text.strip())
        log(f"chunk {i}/{len(boundaries)} OK ({len(text.split())} words)")
        os.remove(chunk_path)
    os.rmdir(tmp_dir)

    wall_elapsed = time.time() - wall_start
    full_transcript = " ".join(transcript_parts)
    word_count = len(full_transcript.split())

    transcript_md_path = os.path.join(args.out, "transcript.md")
    with open(transcript_md_path, "w") as f:
        f.write("# Live STT transcript\n\n")
        f.write(f"Source audio: `{args.audio}`\n\n")
        f.write(full_transcript + "\n")

    similarity = None
    if args.compare:
        with open(args.compare) as f:
            reference = f.read()
        similarity = difflib.SequenceMatcher(None, reference.split(), full_transcript.split()).ratio()
        log(f"similarity vs {args.compare}: {similarity:.4f}")

    meta = {
        "provider": "Sarvam AI",
        "model": MODEL,
        "mode": args.mode,
        "language_code": args.language,
        "endpoint": API_URL,
        "audio_path": os.path.abspath(args.audio),
        "audio_duration_sec": round(duration, 2),
        "audio_channels": channels,
        "audio_sample_rate_hz": rate,
        "n_chunks": len(boundaries),
        "chunk_seconds": args.chunk_seconds,
        "wall_clock_sec": round(wall_elapsed, 2),
        "word_count": word_count,
        "similarity_ratio_vs_reference": similarity,
        "reference_path": os.path.abspath(args.compare) if args.compare else None,
        "cost": "not reported by API response",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta_path = os.path.join(args.out, "transcription_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    log_path = os.path.join(args.out, "run_log.txt")
    with open(log_path, "w") as f:
        f.write("\n".join(run_log) + "\n")

    print(f"\nOK — wrote {transcript_md_path}, {meta_path}, {log_path}")
    print(f"duration={duration:.2f}s words={word_count} wall_clock={wall_elapsed:.2f}s"
          + (f" similarity={similarity:.4f}" if similarity is not None else ""))


if __name__ == "__main__":
    main()
