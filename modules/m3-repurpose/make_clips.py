#!/usr/bin/env python3
"""M3 -- clip cutter.

Takes the top moments from extraction.json and cuts real video out of the real
recording with ffmpeg: for each moment a 16:9 clip as shot and a 9:16 centre-crop
with burned-in captions, plus a thumbnail frame.

In/out points are snapped to Sarvam turn boundaries (the diarized entries in
out/receipts/transcription/chapter*.sarvam.json) so a clip never starts or ends
mid-sentence, and every clip is forced to 30-60 s inside a single chapter file.

Captions are built from the same Sarvam entries. Sarvam saaras:v3 returns
CHUNK-level timings (one entry per ~30 s turn), not per-word ones, so a caption
line's start/end is interpolated across its chunk in proportion to character
count. That is recorded in the receipt -- the line timings are as accurate as the
chunk boundaries allow, not frame-accurate word timings we do not have.

Fails loud when ffmpeg/ffprobe are missing, when a chapter file cannot be
obtained, or when any ffmpeg invocation exits non-zero. Never writes a partial
clip and calls it done.

Usage:
    python3 make_clips.py --out out/m3 --extraction out/m3/extraction.json \
        --event data/incoming/event.json
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
DEFAULT_SARVAM_DIR = REPO_ROOT / "out" / "receipts" / "transcription"
DEFAULT_MEDIA_DIR = REPO_ROOT / "data" / "incoming" / "media"
MIN_CLIP_S, MAX_CLIP_S = 30.0, 60.0
CAPTION_LINE_CHARS = 42
TILE_BOTTOM_TRIM = 0.16   # drops the source's own burned-in caption band
TOTAL_BUDGET_MB = 60


def fail(msg: str):
    print(f"make_clips: {msg}", file=sys.stderr)
    sys.exit(1)


def require_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        fail(f"{' and '.join(missing)} not on PATH -- clips cannot be cut (brew install ffmpeg)")


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def chapters_from_event(event: dict, media_dir: Path) -> dict:
    chapters, offset = {}, 0.0
    for rec in event.get("recording_files", []):
        n = int(rec["chapter"])
        dur = float(rec["duration_sec"])
        chapters[n] = {"chapter": n, "url": rec.get("url", ""), "duration_sec": dur,
                       "start_abs": offset, "end_abs": offset + dur,
                       "path": media_dir / f"chapter{n}.mp4"}
        offset += dur
    return chapters


def ensure_media(chapter: dict) -> Path:
    """Local chapter file, downloaded from the public CDN when absent."""
    path = chapter["path"]
    if path.exists() and path.stat().st_size > 0:
        return path
    if not chapter["url"]:
        fail(f"chapter {chapter['chapter']} has no local file ({path}) and no URL in event.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[clips] downloading chapter {chapter['chapter']} from {chapter['url']}")
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(chapter["url"], timeout=300) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.rename(path)
    print(f"[clips] chapter {chapter['chapter']}: {path.stat().st_size / 1e6:.1f} MB")
    return path


def load_turns(sarvam_dir: Path, chapter_n: int) -> list:
    """Sarvam diarized entries for one chapter, in chapter-local seconds."""
    p = sarvam_dir / f"chapter{chapter_n}.sarvam.json"
    if not p.exists():
        fail(f"missing Sarvam transcript receipt {p} -- clip cuts need real turn boundaries")
    entries = json.loads(p.read_text()).get("diarized_transcript", {}).get("entries", [])
    turns = [{"start": float(e["start_time_seconds"]), "end": float(e["end_time_seconds"]),
              "text": (e.get("transcript") or "").strip(), "speaker_id": e.get("speaker_id")}
             for e in entries if e.get("transcript")]
    turns.sort(key=lambda t: t["start"])
    return turns


def snap_window(turns: list, want_start: float, want_end: float, chapter_len: float) -> tuple:
    """Snap a requested window onto turn boundaries and force it to 30-60 s."""
    if not turns:
        fail("no Sarvam turns for this chapter")
    start_turn = min(turns, key=lambda t: abs(t["start"] - want_start))
    start = start_turn["start"]
    end = None
    for t in turns:
        if t["end"] <= start:
            continue
        if t["end"] - start >= MIN_CLIP_S:
            end = t["end"]
            break
    if end is None:                                   # ran out of turns -- take the last one
        end = turns[-1]["end"]
    if end - start > MAX_CLIP_S:                      # a long turn: cut at the cap
        end = start + MAX_CLIP_S
    if end - start < MIN_CLIP_S:                      # a short tail: extend to the floor
        end = min(start + MIN_CLIP_S + 5, chapter_len)
        start = max(0.0, min(start, end - MIN_CLIP_S))
    return round(start, 2), round(min(end, chapter_len), 2)


def wrap(text: str, width: int = CAPTION_LINE_CHARS) -> list:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def build_caption_blocks(turns: list, start: float, end: float) -> list:
    """Caption lines for [start, end), timed relative to the clip. Sarvam gives
    chunk-level timings, so each line takes a slice of its chunk proportional to
    its character count."""
    blocks = []
    for turn in turns:
        if turn["end"] <= start or turn["start"] >= end:
            continue
        lines = wrap(turn["text"])
        total_chars = sum(len(l) for l in lines) or 1
        cursor = turn["start"]
        span = max(0.4, turn["end"] - turn["start"])
        for line in lines:
            line_start, line_end = cursor, cursor + span * (len(line) / total_chars)
            cursor = line_end
            vis_start, vis_end = max(line_start, start), min(line_end, end)
            if vis_end - vis_start < 0.25:
                continue
            blocks.append({"start": round(vis_start - start, 3), "end": round(vis_end - start, 3),
                           "text": line})
    return blocks


def srt_text(blocks: list) -> str:
    return "\n".join(f"{i}\n{srt_time(b['start'])} --> {srt_time(b['end'])}\n{b['text']}\n"
                     for i, b in enumerate(blocks, 1))


CAPTION_FONTS = ["/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def caption_font(size: int = 46):
    """This ffmpeg build ships without libass and without libfreetype (no
    `subtitles`, no `drawtext` filter -- checked with `ffmpeg -filters`), so
    caption lines are rendered to transparent PNG strips with Pillow and burned
    in with `overlay`. Same burned-in result, no hidden dependency."""
    try:
        from PIL import ImageFont
    except ImportError:
        fail("Pillow is required to burn captions on this ffmpeg build "
             "(no libass/subtitles filter available) -- pip install pillow")
    for path in CAPTION_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    fail(f"no usable TTF font found in {CAPTION_FONTS} -- cannot render caption strips")


def render_caption_pngs(blocks: list, clips_dir: Path, slug: str, width: int = 980) -> list:
    """One transparent PNG per caption line, ready for the overlay chain."""
    from PIL import Image, ImageDraw
    font = caption_font()
    cap_dir = clips_dir / f"{slug}-captions"
    cap_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for i, block in enumerate(blocks, 1):
        img = Image.new("RGBA", (width, 108), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        box = draw.textbbox((0, 0), block["text"], font=font)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        pad_x, pad_y = 26, 18
        plate = ((width - text_w) // 2 - pad_x, 0,
                 (width + text_w) // 2 + pad_x, text_h + 2 * pad_y + 10)
        draw.rounded_rectangle(plate, radius=12, fill=(8, 22, 40, 205))
        draw.text(((width - text_w) // 2, pad_y - box[1] + 4), block["text"], font=font,
                  fill=(255, 255, 255, 255))
        path = cap_dir / f"{i:03d}.png"
        img.save(path)
        rendered.append({**block, "png": path})
    return rendered


def detect_content_box(src: Path, start: float, dur: float) -> tuple:
    """cropdetect over the clip window: the real picture area inside whatever
    letterbox/pillarbox the recording platform baked in. Returns (w, h, x, y)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{min(dur, 6):.2f}", "-i", str(src),
         "-vf", "cropdetect=24:2:0", "-f", "null", "-"], capture_output=True, text=True)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)
    if not found:
        return None
    return tuple(int(v) for v in found[-1])


def vertical_filter(box, caption_count: int) -> tuple:
    """Build the 9:16 1080x1920 video chain plus the caption overlay chain.

    A literal centre crop of this recording lands on the seam between the two
    speaker tiles (verified on a frame grab: half a face either side), so a
    side-by-side source (content box wider than ~2.2:1) is rebuilt as the two
    tiles stacked vertically instead -- same pixels, no one cropped out. Any
    other layout gets the plain centre crop. Which one ran is in the receipt."""
    if box:
        w, h, x, y = box
    else:
        w, h, x, y = 1280, 720, 0, 0
    if w / max(h, 1) > 2.2:                      # side-by-side speakers
        half = (w // 2) - ((w // 2) % 2)
        # Trim the bottom of each tile: the recording platform burns its own
        # captions across the full width, so an untrimmed split leaves half of
        # its caption line sitting on each tile under ours.
        tile_h = int(h * (1 - TILE_BOTTOM_TRIM))
        tile_h -= tile_h % 2
        chain = [f"[0:v]crop={half}:{tile_h}:{x}:{y},scale=1080:-2[tile0]",
                 f"[0:v]crop={half}:{tile_h}:{x + half}:{y},scale=1080:-2[tile1]",
                 "[tile0][tile1]vstack=inputs=2[stacked]",
                 "[stacked]scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x0B2545[v0]"]
        layout = "stacked-tiles"
    else:
        crop_w = min(w, int(h * 9 / 16))
        crop_w -= crop_w % 2
        chain = [f"[0:v]crop={crop_w}:{h}:{x + (w - crop_w) // 2}:{y},scale=1080:1920[v0]"]
        layout = "centre-crop"
    label = "v0"
    for n in range(1, caption_count + 1):
        chain.append(f"[{label}][{n}:v]overlay=x=(W-w)/2:y=H-300:"
                     f"enable='between(t,{{start{n}}},{{end{n}}})'[v{n}]")
        label = f"v{n}"
    return chain, label, layout


def run_ffmpeg(args: list, cwd: Path) -> int:
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
                          cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip()[-600:], file=sys.stderr)
    return proc.returncode


def probe_duration(path: Path) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return round(float(proc.stdout.strip()), 2)
    except ValueError:
        return 0.0


def cut_clip(moment: dict, chapter: dict, turns: list, clips_dir: Path, index: int) -> dict:
    src = ensure_media(chapter)
    local_start = moment["start_seconds"] - chapter["start_abs"]
    local_end = moment["end_seconds"] - chapter["start_abs"]
    start, end = snap_window(turns, local_start, local_end, chapter["duration_sec"])
    dur = round(end - start, 2)
    slug = f"clip{index}-ch{chapter['chapter']}"
    srt_name, wide_name = f"{slug}.srt", f"{slug}-16x9.mp4"
    vert_name, thumb_name = f"{slug}-9x16.mp4", f"{slug}-thumb.png"

    blocks = build_caption_blocks(turns, start, end)
    (clips_dir / srt_name).write_text(srt_text(blocks))
    captions = render_caption_pngs(blocks, clips_dir, slug)

    codes = {}
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    codes["16x9"] = run_ffmpeg(["-ss", f"{start:.2f}", "-i", str(src.resolve()), "-t", f"{dur:.2f}"]
                               + encode + [wide_name], clips_dir)

    # Burn the captions into the 9:16 cut by overlaying the rendered strips, each
    # enabled only for its own time window (see caption_font() for why not subtitles=).
    box = detect_content_box(src, start, dur)
    chain, label, layout = vertical_filter(box, len(captions))
    inputs = []
    for n, cap in enumerate(captions, 1):
        inputs += ["-i", str(cap["png"].relative_to(clips_dir))]
        chain = [c.replace(f"{{start{n}}}", f"{cap['start']:.2f}").replace(f"{{end{n}}}", f"{cap['end']:.2f}")
                 for c in chain]
    codes["9x16"] = run_ffmpeg(
        ["-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src.resolve())] + inputs
        + ["-filter_complex", ";".join(chain), "-map", f"[{label}]", "-map", "0:a?"]
        + encode + [vert_name], clips_dir)
    codes["thumb"] = run_ffmpeg(["-ss", f"{start + min(3.0, dur / 2):.2f}", "-i", str(src.resolve()),
                                 "-frames:v", "1", thumb_name], clips_dir)

    files = {}
    for key, name in (("landscape", wide_name), ("vertical", vert_name),
                      ("captions", srt_name), ("thumbnail", thumb_name)):
        path = clips_dir / name
        files[key] = {"file": name, "bytes": path.stat().st_size if path.exists() else 0,
                      "duration_s": probe_duration(path) if name.endswith(".mp4") else None}
    return {
        "moment": moment.get("title", ""),
        "speaker": moment.get("speaker", ""),
        "clip_worthiness": moment.get("clip_worthiness"),
        "chapter": chapter["chapter"],
        "source_file": src.name,
        "in_chapter_local_s": start,
        "out_chapter_local_s": end,
        "in_absolute_s": round(start + chapter["start_abs"], 2),
        "out_absolute_s": round(end + chapter["start_abs"], 2),
        "requested_duration_s": round(moment["end_seconds"] - moment["start_seconds"], 2),
        "duration_s": dur,
        "caption_lines": len(captions),
        "content_box": box,
        "vertical_layout": layout,
        "tile_bottom_trim": TILE_BOTTOM_TRIM,
        "ffmpeg_exit_codes": codes,
        "files": files,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Cut the top extraction moments into 16:9 + 9:16 clips.")
    ap.add_argument("--out", type=Path, required=True, help="M3 run directory (clips/ and receipts/ go here)")
    ap.add_argument("--extraction", type=Path, required=True)
    ap.add_argument("--event", type=Path, required=True)
    ap.add_argument("--sarvam-dir", type=Path, default=DEFAULT_SARVAM_DIR, dest="sarvam_dir")
    ap.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR, dest="media_dir")
    ap.add_argument("--top", type=int, default=3, help="How many moments to cut (default 3)")
    args = ap.parse_args()

    require_tools()
    if not args.extraction.exists():
        fail(f"extraction not found: {args.extraction}")
    extraction = json.loads(args.extraction.read_text())
    event = json.loads(args.event.read_text())
    chapters = chapters_from_event(event, args.media_dir)
    if not chapters:
        fail("event.json lists no recording_files -- nothing to cut")

    moments = [m for m in extraction.get("moments", []) if m.get("chapter") in chapters]
    moments.sort(key=lambda m: -(m.get("clip_worthiness") or 0))
    if len(moments) < args.top:
        fail(f"only {len(moments)} usable moment(s) in {args.extraction} -- need {args.top}")

    clips_dir = args.out / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results, turn_cache = [], {}
    for i, moment in enumerate(moments[:args.top], 1):
        ch = chapters[moment["chapter"]]
        turn_cache.setdefault(ch["chapter"], load_turns(args.sarvam_dir, ch["chapter"]))
        print(f"[clips] {i}/{args.top} ch{ch['chapter']} {moment['start']}-{moment['end']} "
              f"(worth {moment.get('clip_worthiness')}): {moment.get('title', '')[:60]}")
        results.append(cut_clip(moment, ch, turn_cache[ch["chapter"]], clips_dir, i))

    total_bytes = sum(f["bytes"] for r in results for f in r["files"].values())
    receipt = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "ffmpeg",
        "ffmpeg_version": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
                                    .stdout.splitlines()[0],
        "event_slug": extraction.get("event_slug"),
        "seconds": round(time.monotonic() - started, 1),
        "clips": results,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1e6, 2),
        "caption_source": "sarvam saaras:v3 diarized chunk timings; line starts/ends interpolated "
                          "within each chunk by character count (chunk-level, not word-level, is what "
                          "the API returns)",
        "caption_burn_method": "Pillow-rendered PNG strips composited with ffmpeg overlay+enable "
                               "(this ffmpeg build has no libass/subtitles or libfreetype/drawtext filter)",
        "duration_rule": f"{int(MIN_CLIP_S)}-{int(MAX_CLIP_S)}s, snapped to Sarvam turn boundaries",
    }
    receipts_dir = args.out / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / "m3_clips.json").write_text(json.dumps(receipt, indent=2))

    bad = [r for r in results if any(c != 0 for c in r["ffmpeg_exit_codes"].values())]
    out_of_range = [r for r in results
                    if not all(MIN_CLIP_S - 1 <= (f["duration_s"] or 0) <= MAX_CLIP_S + 1
                               for f in r["files"].values() if f["duration_s"] is not None)]
    for r in results:
        print(f"[clips] {r['files']['landscape']['file']} {r['duration_s']}s "
              f"({r['files']['landscape']['bytes'] / 1e6:.1f} MB) + "
              f"{r['files']['vertical']['file']} ({r['files']['vertical']['bytes'] / 1e6:.1f} MB) + captions")
    print(f"[clips] {len(results)} clip(s), {receipt['total_mb']} MB total -> {clips_dir}")
    if receipt["total_mb"] > TOTAL_BUDGET_MB:
        print(f"[clips] WARNING: {receipt['total_mb']} MB exceeds the ~{TOTAL_BUDGET_MB} MB budget",
              file=sys.stderr)
    if bad:
        fail(f"{len(bad)} clip(s) had a non-zero ffmpeg exit -- see receipts/m3_clips.json")
    if out_of_range:
        fail(f"{len(out_of_range)} clip(s) landed outside {int(MIN_CLIP_S)}-{int(MAX_CLIP_S)}s "
             "-- see receipts/m3_clips.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
