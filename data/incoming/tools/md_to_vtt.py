#!/usr/bin/env python3
"""Converts data/incoming/transcript.md (canonical input, [MM:SS] speaker
turns) into data/incoming/transcript.vtt -- the platform-export twin, since
Zoom/ON24/GoTo hand you a .vtt, not a hand-formatted markdown transcript.

Cue rules:
  - start = the turn's own [MM:SS] timestamp (MM may exceed 59, e.g. [61:58]
    -- these are minutes:seconds from session start, not hours:minutes).
  - end = the next turn's start timestamp; the last cue gets start + 20s.
  - text = "<v Speaker Name>turn text" (WebVTT voice tag, so a player can
    show/filter by speaker the way it would for a real platform export).

A turn line with no ":"-terminated speaker name inside the bold markers
(only the closing "[62:00] **[Session ends]**" marker in this transcript) is
still emitted as a cue, voice-tagged "SYSTEM", so no timestamped line is
silently dropped.

Stdlib only (re, pathlib).
"""
import re
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
INCOMING_DIR = TOOLS_DIR.parent
IN_PATH = INCOMING_DIR / "transcript.md"
OUT_PATH = INCOMING_DIR / "transcript.vtt"

TURN_RE = re.compile(r"^\[(\d+):(\d{2})\]\s+\*\*(.*?)\*\*(.*)$")
TAIL_SECONDS = 20  # last cue's synthetic end = its start + this many seconds


def parse_turns(text: str) -> list:
    """Returns [(start_seconds, speaker, body), ...] in file order."""
    turns = []
    for line in text.splitlines():
        m = TURN_RE.match(line.strip())
        if not m:
            continue
        minutes, seconds, bold_inner, tail = m.groups()
        start = int(minutes) * 60 + int(seconds)
        bold_inner = bold_inner.strip()
        tail = tail.strip()
        if bold_inner.endswith(":"):
            speaker = bold_inner[:-1].strip()
            body = tail
        else:
            # e.g. "[Session ends]" -- a bracketed marker, not a speaker turn.
            speaker = "SYSTEM"
            body = (bold_inner + " " + tail).strip()
        turns.append((start, speaker, body))
    return turns


def fmt_timestamp(total_seconds: float) -> str:
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    secs = int(total_seconds % 60)
    millis = round((total_seconds - int(total_seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def escape_vtt_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_vtt(turns: list) -> str:
    lines = ["WEBVTT", ""]
    for i, (start, speaker, body) in enumerate(turns):
        end = turns[i + 1][0] if i + 1 < len(turns) else start + TAIL_SECONDS
        if end <= start:  # guard: never emit a zero/negative-length cue
            end = start + 1
        cue_text = f"<v {speaker}>{escape_vtt_text(body)}"
        lines.append(str(i + 1))
        lines.append(f"{fmt_timestamp(start)} --> {fmt_timestamp(end)}")
        lines.append(cue_text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main():
    text = IN_PATH.read_text(encoding="utf-8")
    turns = parse_turns(text)
    if not turns:
        raise SystemExit(f"error: no [MM:SS] speaker turns found in {IN_PATH}")
    vtt = build_vtt(turns)
    OUT_PATH.write_text(vtt, encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(turns)} cues, from {IN_PATH.name})")


if __name__ == "__main__":
    main()
