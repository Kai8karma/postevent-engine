#!/usr/bin/env python3
"""
build_transcript.py — Sarvam batch JSON (one per chapter) -> data/incoming/transcript.md
in the repo's canonical `[MM:SS] **Speaker Name:** text` turn format, with the
header block the rest of the pipeline reads. Chapter offsets come from event.json.

Usage: python3 build_transcript.py --event data/incoming/event.json
         --chapter 1=out/transcription/chapter1.sarvam.json --chapter 2=... --chapter 3=...
         --speaker-map "SPEAKER_00=Sudi Bjornstad Korba" --speaker-map "SPEAKER_01=Q Hamirani"
         --out data/incoming/transcript.md
"""
import argparse, json, re
from pathlib import Path

def find_entries(obj):
    """Locate the diarized turn list regardless of nesting: dicts with a speaker id + start."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and any(k in obj[0] for k in ("speaker_id","speaker")) and any(k in obj[0] for k in ("start_time_seconds","start")):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = find_entries(v)
            if r: return r
    if isinstance(obj, list):
        for v in obj:
            r = find_entries(v)
            if r: return r
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True); ap.add_argument("--chapter", action="append", required=True)
    ap.add_argument("--speaker-map", action="append", default=[]); ap.add_argument("--out", required=True)
    ap.add_argument("--merge-gap", type=float, default=2.0, help="merge consecutive same-speaker turns closer than this (s)")
    ap.add_argument("--replace", action="append", default=["Darwin Box=Darwinbox", "Darwin box=Darwinbox", "darwin box=Darwinbox"],
                    help="literal STT spelling fixes, FROM=TO (defaults fix the host name)")
    a = ap.parse_args()
    ev = json.loads(Path(a.event).read_text())
    offsets, acc = {}, 0
    for rf in sorted(ev["recording_files"], key=lambda r: r["chapter"]):
        offsets[rf["chapter"]] = acc; acc += rf["duration_sec"]
    smap = dict(kv.split("=", 1) for kv in a.speaker_map)
    turns = []
    for spec in a.chapter:
        ch, path = spec.split("=", 1); ch = int(ch)
        data = json.loads(Path(path).read_text())
        ents = find_entries(data)
        if not ents:
            # no diarization: fall back to one block from the plain transcript
            txt = data.get("transcript") or ""
            turns.append((offsets[ch], "Unknown", txt.strip())); continue
        for e in ents:
            spk = str(e.get("speaker_id", e.get("speaker", "?")))
            start = float(e.get("start_time_seconds", e.get("start", 0))) + offsets[ch]
            text = (e.get("transcript") or e.get("text") or "").strip()
            for kv in a.replace:
                f, _, t = kv.partition("="); text = text.replace(f, t)
            if text: turns.append((start, smap.get(spk, spk), text))
    turns.sort(key=lambda t: t[0])
    merged = []
    for s, spk, txt in turns:
        if merged and merged[-1][1] == spk and s - merged[-1][3] < a.merge_gap:
            merged[-1][2] += " " + txt; merged[-1][3] = s
        else: merged.append([s, spk, txt, s])
    spk_line = ", ".join(f"{s['name']} ({s['title']}, {s['company']})" for s in ev["speakers"])
    lines = [f"# {ev['event_name']}", "", f"**Host:** {ev['host_company']} ({ev['host_domain']})",
             f"**Date:** {ev['date']}", f"**Platform:** {ev['platform']}", f"**Duration:** {ev['duration_min']} minutes",
             f"**Speakers:** {spk_line}", "", "---", ""]
    for s, spk, txt, _ in merged:
        m, sec = divmod(int(s), 60); lines.append(f"[{m:02d}:{sec:02d}] **{spk}:** {txt}"); lines.append("")
    m, sec = divmod(acc, 60); lines.append(f"[{m:02d}:{sec:02d}] **[Session ends]**")
    Path(a.out).write_text("\n".join(lines) + "\n")
    words = sum(len(t[2].split()) for t in merged)
    print(f"turns={len(merged)} words={words} speakers={sorted(set(t[1] for t in merged))} -> {a.out}")

if __name__ == "__main__": main()
