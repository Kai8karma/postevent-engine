#!/usr/bin/env python3
"""M3 -- Grounding verifier.

M3's prompts (prompts/extraction.md, prompts/blog.md, etc.) all demand that
generated content stay tied to the transcript: verbatim quotes, real
[MM:SS] timestamps, correct speaker attribution. Nothing enforced that
until now -- an LLM invents a quote or misattributes a stat and the build
has no way to catch it. This script is that check.

It looks at each generated asset (blog.md, youtube.md, infographic.md,
social.md) and verifies three kinds of claim against the source transcript:

  1. timestamp claims  -- every [MM:SS] / [H:MM:SS] cited (including bare
     leading timestamps in a YouTube "## Chapters" list, and both ends of a
     "[MM:SS-MM:SS]" range) must be a real turn-start time in the
     transcript. Where a name appears immediately before the bracket
     ("Daniel Kim, Northwind Analytics [05:10]"), the transcript speaker
     who actually has the turn at that timestamp must match.
  2. quote claims -- every double-quoted string of a nontrivial length must
     fuzzy-match (difflib.SequenceMatcher ratio >= QUOTE_RATIO_THRESHOLD)
     some transcript window. This is the same primitive M1's dedupe already
     uses (modules/m1-enrichment/enrich.py), just pointed at prose instead
     of contact-identity strings -- no new dependency.
  3. speaker-attribution claims -- every "-- Name, Title, Company" line (or
     inline "...quote." -- Name, Title" attribution) must name someone in
     the event's actual speaker list.

Output is a grounding_report.json: per-asset counts of claims checked /
verified / failed, with each failure carrying the offending text, the
best-matching transcript window and its ratio (for quotes), and why it
failed. Exit code is 0 unless --strict is passed and something failed --
see repurpose.py's docstring for why the default is "annotate and flag
loudly", not "block the run".

Python 3 stdlib only. Zero network calls.

Usage:
    python3 verify_grounding.py --transcript data/incoming/transcript.md \
        --event data/incoming/event.json --assets-dir out/m3
    python3 verify_grounding.py ... --strict   # nonzero exit on any failure
"""
import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ASSET_NAMES = ["blog.md", "youtube.md", "infographic.md", "social.md"]

MIN_QUOTE_CHARS = 25          # below this, quoting is basically unfalsifiable
QUOTE_RATIO_THRESHOLD = 0.90  # matches the fuzzy-dedupe bar M1 uses
SPEAKER_NAME_RATIO_THRESHOLD = 0.85

_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
BRACKET_TS_RE = re.compile(rf"\[({_TS})(?:\s*[–‒-]\s*({_TS}))?\]")
CHAPTER_LINE_RE = re.compile(rf"^({_TS})\s+\S")
# "Daniel Kim, Northwind Analytics [05:10]" -- name immediately before a
# timestamp bracket, within the trailing ~60 chars of context.
NAME_BEFORE_BRACKET_RE = re.compile(
    r"([A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,2}),\s*[^,\[\]\n]{0,60}$"
)
# "-- Daniel Kim, Head of Demand Gen, Northwind Analytics" on its own line.
DASH_ATTR_LINE_RE = re.compile(
    r"^\s*[—–-]{1,2}\s*([A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,3}),", re.MULTILINE
)
# '"...quote." -- Sara Alvarez, RevOps Lead' inline within a paragraph.
DASH_ATTR_INLINE_RE = re.compile(
    r'["”]\s*[—–-]{1,2}\s*([A-Z][\w\'.-]+(?:\s+[A-Z][\w\'.-]+){0,3}),'
)
STRAIGHT_QUOTE_RE = re.compile(r'"([^"\n]+)"')
CURLY_QUOTE_RE = re.compile(r'“([^”\n]+)”')
TURN_RE = re.compile(rf"^\[({_TS})\]\s*\*\*([^:*]+):\*\*\s*(.*)$")
# "## Segment 1 -- Speed-to-Lead (08:10-24:00)" -- the transcript's own
# section markers. A segment's *end* boundary is a real, stated timestamp
# even though (unlike a turn start) nothing begins speaking exactly then.
SEGMENT_HEADER_RE = re.compile(rf"\(({_TS})\s*[–‒-]\s*({_TS})\)")
# How far around a quoted span to look for a named speaker before treating
# the quote as a "this is verbatim speech" claim at all. Text quoted for
# other reasons (chart captions, rhetorical asides in a design brief) isn't
# a grounding claim -- nobody attributed it to anyone.
QUOTE_ATTRIBUTION_WINDOW_BEFORE = 180
QUOTE_ATTRIBUTION_WINDOW_AFTER = 120
# Sections that are the tool's own creative copy, not a citation of speech --
# a thumbnail headline or a title option quoted for emphasis is not a claim
# that a speaker said those words, even if a speaker's name is nearby.
SKIP_QUOTE_HEADING_RE = re.compile(r"headline options|thumbnail brief", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


def parse_timestamp(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_transcript(text: str) -> list:
    """One entry per `[MM:SS] **Speaker:** text` turn line."""
    turns = []
    for line in text.splitlines():
        m = TURN_RE.match(line.strip())
        if m:
            ts, speaker, body = m.groups()
            turns.append({"ts": ts, "sec": parse_timestamp(ts), "speaker": speaker.strip(), "text": body.strip()})
    return turns


def segment_boundary_seconds(text: str) -> set:
    """Both ends of every `(MM:SS-MM:SS)` segment header the transcript
    itself declares -- a segment's stated end time is a real citable
    timestamp even though no turn starts exactly then."""
    out = set()
    for m in SEGMENT_HEADER_RE.finditer(text):
        out.add(parse_timestamp(m.group(1)))
        out.add(parse_timestamp(m.group(2)))
    return out


def build_windows(turns: list) -> list:
    """(window_text, label, start_sec) -- single turns plus adjacent pairs,
    so a quote that straddles a turn boundary still has a home to match."""
    windows = [(t["text"], t["speaker"], t["sec"]) for t in turns]
    for i in range(len(turns) - 1):
        a, b = turns[i], turns[i + 1]
        windows.append((f'{a["text"]} {b["text"]}', f'{a["speaker"]} -> {b["speaker"]}', a["sec"]))
    return windows


def best_quote_match(quote: str, windows: list):
    """Best difflib ratio of `quote` against any transcript window. Windows
    much longer than the quote are scanned with a strided substring slide
    (same length as the quote) rather than compared whole -- SequenceMatcher
    ratio degrades with length mismatch even for a perfect substring hit."""
    best_ratio, best_snippet, best_label = 0.0, "", ""
    ql = len(quote)
    if ql == 0:
        return best_ratio, best_snippet, best_label
    qlow = quote.lower()
    for wtext, label, _sec in windows:
        wlow = wtext.lower()
        wl = len(wlow)
        if wl == 0:
            continue
        if wl <= ql * 1.4:
            r = SequenceMatcher(None, qlow, wlow).ratio()
            if r > best_ratio:
                best_ratio, best_snippet, best_label = r, wtext, label
            continue
        stride = max(4, ql // 6)
        for start in range(0, wl - ql + 1, stride):
            sub = wlow[start:start + ql]
            r = SequenceMatcher(None, qlow, sub).ratio()
            if r > best_ratio:
                best_ratio, best_snippet, best_label = r, wtext[start:start + ql], label
        tail = wlow[-ql:]
        r = SequenceMatcher(None, qlow, tail).ratio()
        if r > best_ratio:
            best_ratio, best_snippet, best_label = r, wtext[-ql:], label
    return best_ratio, best_snippet, best_label


def _mentions_speaker(window: str, speaker_names: list) -> bool:
    for name in speaker_names:
        if name in window:
            return True
        last = name.split()[-1]
        if len(last) > 2 and last in window:
            return True
    return False


def _heading_before(text: str, pos: int):
    heading = None
    for m in HEADING_RE.finditer(text):
        if m.start() > pos:
            break
        heading = m.group(1)
    return heading


def find_quotes(text: str, speaker_names: list) -> list:
    """Double/curly-quoted spans long enough to be a real claim, restricted
    to spans a named speaker is actually attributed near -- a quoted design
    caption ("18 hrs -> <2 hrs") in an infographic layout section is not a
    claim that anyone said those words -- and excluding headline/thumbnail
    sections outright, since that copy is the tool's own creative writing."""
    found = []
    for rx in (STRAIGHT_QUOTE_RE, CURLY_QUOTE_RE):
        for m in rx.finditer(text):
            q = m.group(1).strip()
            if len(q) < MIN_QUOTE_CHARS:
                continue
            heading = _heading_before(text, m.start())
            if heading and SKIP_QUOTE_HEADING_RE.search(heading):
                continue
            before = text[max(0, m.start() - QUOTE_ATTRIBUTION_WINDOW_BEFORE):m.start()]
            after = text[m.end():m.end() + QUOTE_ATTRIBUTION_WINDOW_AFTER]
            if _mentions_speaker(before, speaker_names) or _mentions_speaker(after, speaker_names):
                found.append(q)
    return found


def find_timestamp_claims(text: str, check_chapters: bool) -> list:
    claims = []
    for m in BRACKET_TS_RE.finditer(text):
        ts1, ts2 = m.group(1), m.group(2)
        context_before = text[max(0, m.start() - 60):m.start()]
        nm = NAME_BEFORE_BRACKET_RE.search(context_before)
        name = nm.group(1).strip() if nm else None
        claims.append({
            "raw": m.group(0),
            "ts_list": [ts1] + ([ts2] if ts2 else []),
            "name": name,
            "context": text[max(0, m.start() - 40):m.end() + 10].strip(),
        })
    if check_chapters:
        for line in text.splitlines():
            cm = CHAPTER_LINE_RE.match(line.strip())
            if cm:
                claims.append({"raw": line.strip(), "ts_list": [cm.group(1)], "name": None, "context": line.strip()})
    return claims


def find_speaker_attributions(text: str) -> list:
    """Same "creative-copy sections aren't claims" skip find_quotes() uses
    (SKIP_QUOTE_HEADING_RE) -- a Headline Options bullet like "Speed,
    Segmentation, ROI: ..." (capitalized word + comma at line start)
    otherwise misreads as a "-- Name," attribution line."""
    names = []
    for rx in (DASH_ATTR_LINE_RE, DASH_ATTR_INLINE_RE):
        for m in rx.finditer(text):
            heading = _heading_before(text, m.start())
            if heading and SKIP_QUOTE_HEADING_RE.search(heading):
                continue
            names.append(m.group(1).strip())
    return names


STAT_REF_RE = re.compile(r"\bStat\s+(\d+)\b")


def find_undefined_stat_refs(text: str) -> list:
    """infographic.md only: every "Stat N" the Layout section cites by
    ordinal must exist as a Data Points entry -- catches a Layout describing
    a stat number no Data Points bullet defines (shipped defect, 2026-08:
    Layout referenced "Stat 7" with only 6 Data Points)."""
    dp_m = re.search(r"^##\s*Data Points\s*$", text, re.MULTILINE)
    layout_m = re.search(r"^##\s*Layout\s*$", text, re.MULTILINE)
    if not dp_m or not layout_m or layout_m.start() <= dp_m.end():
        return []
    n_points = len(re.findall(r"^\s*(?:\d+\.\s*)?\*\*", text[dp_m.end():layout_m.start()], re.MULTILINE))
    return sorted({n for n in map(int, STAT_REF_RE.findall(text[layout_m.end():])) if n > n_points})


def match_speaker(name: str, speaker_names: list):
    best_name, best_ratio = None, 0.0
    for s in speaker_names:
        r = SequenceMatcher(None, name.lower(), s.lower()).ratio()
        if r > best_ratio:
            best_ratio, best_name = r, s
    return best_name, best_ratio


def verify_asset(name: str, text: str, turns: list, windows: list, valid_ts_seconds: set, speaker_names: list) -> dict:
    claims = []
    is_youtube = "## Chapters" in text

    for tc in find_timestamp_claims(text, check_chapters=is_youtube):
        for ts in tc["ts_list"]:
            sec = parse_timestamp(ts)
            ok = sec in valid_ts_seconds
            claim = {"type": "timestamp", "text": tc["raw"], "context": tc["context"], "verified": ok}
            if not ok:
                claim["reason"] = f"timestamp {ts} does not appear as a transcript turn start"
            claims.append(claim)
        if tc["name"] and len(tc["ts_list"]) == 1:
            matched_speaker, ratio = match_speaker(tc["name"], speaker_names)
            if matched_speaker and ratio >= SPEAKER_NAME_RATIO_THRESHOLD:
                sec = parse_timestamp(tc["ts_list"][0])
                turn = next((t for t in turns if t["sec"] == sec), None)
                if turn is not None:
                    same = SequenceMatcher(None, turn["speaker"].lower(), matched_speaker.lower()).ratio() >= SPEAKER_NAME_RATIO_THRESHOLD
                    claim2 = {
                        "type": "speaker_at_timestamp",
                        "text": f'{tc["name"]} @ [{tc["ts_list"][0]}]',
                        "context": tc["context"],
                        "verified": same,
                    }
                    if not same:
                        claim2["reason"] = f'cited speaker "{tc["name"]}" but the transcript speaker at [{tc["ts_list"][0]}] is "{turn["speaker"]}"'
                    claims.append(claim2)

    for q in find_quotes(text, speaker_names):
        ratio, snippet, label = best_quote_match(q, windows)
        ok = ratio >= QUOTE_RATIO_THRESHOLD
        claim = {
            "type": "quote",
            "text": q,
            "verified": ok,
            "best_match_ratio": round(ratio, 3),
            "best_match_window": snippet,
            "best_match_location": label,
        }
        if not ok:
            claim["reason"] = f"no transcript window matched at ratio >= {QUOTE_RATIO_THRESHOLD} (best {ratio:.3f})"
        claims.append(claim)

    if name == "infographic.md":
        for n in find_undefined_stat_refs(text):
            claims.append({
                "type": "structural", "text": f"Stat {n}",
                "context": "## Layout", "verified": False,
                "reason": f'## Layout references "Stat {n}" but ## Data Points has no entry {n}',
            })

    for nm in find_speaker_attributions(text):
        matched_speaker, ratio = match_speaker(nm, speaker_names)
        ok = ratio >= SPEAKER_NAME_RATIO_THRESHOLD
        claim = {"type": "speaker_attribution", "text": nm, "verified": ok}
        if not ok:
            claim["reason"] = f'attributed name "{nm}" not found in event speaker list ({", ".join(speaker_names)})'
        claims.append(claim)

    failed = [c for c in claims if not c["verified"]]
    return {
        "asset": name,
        "claims_checked": len(claims),
        "verified": len(claims) - len(failed),
        "failed_count": len(failed),
        "failed": failed,
        "pass": len(failed) == 0,
    }


def check_assets(transcript_text: str, event: dict, asset_texts: dict) -> dict:
    """Public entry point for repurpose.py -- no file I/O, no argparse."""
    turns = parse_transcript(transcript_text)
    windows = build_windows(turns)
    valid_ts_seconds = {t["sec"] for t in turns} | segment_boundary_seconds(transcript_text)
    speaker_names = [s["name"] for s in event.get("speakers", [])]

    assets = {name: verify_asset(name, text, turns, windows, valid_ts_seconds, speaker_names)
              for name, text in asset_texts.items()}
    checked = sum(r["claims_checked"] for r in assets.values())
    verified = sum(r["verified"] for r in assets.values())
    failed = sum(r["failed_count"] for r in assets.values())
    return {
        "overall_pass": all(r["pass"] for r in assets.values()),
        "totals": {"claims_checked": checked, "verified": verified, "failed": failed},
        "assets": assets,
    }


def annotate_text(text: str, asset_report: dict) -> str:
    """Prepend a visible flag to an asset that failed grounding, so a human
    reviewer sees it before publish without having to open grounding_report.json.
    No-op when the asset passed."""
    if asset_report is None or asset_report["pass"]:
        return text
    lines = [
        f"> GROUNDING CHECK FLAGGED {asset_report['failed_count']} of {asset_report['claims_checked']} "
        f"claim(s) that did not verify against the transcript -- review before publishing "
        f"(see grounding_report.json).",
    ]
    for c in asset_report["failed"][:5]:
        snippet = c["text"] if len(c["text"]) <= 120 else c["text"][:117] + "..."
        lines.append(f"> - [{c['type']}] {snippet!r} -- {c.get('reason', '')}")
    if asset_report["failed_count"] > 5:
        lines.append(f"> - ...and {asset_report['failed_count'] - 5} more")
    banner = "\n".join(lines) + "\n\n"
    if text.startswith("<!--"):
        nl = text.find("\n")
        if nl != -1:
            return text[:nl + 1] + "\n" + banner + text[nl + 1:]
    return banner + text


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify M3 generated assets are grounded in the source transcript.")
    ap.add_argument("--transcript", type=Path, required=True)
    ap.add_argument("--event", type=Path, required=True)
    ap.add_argument("--assets-dir", type=Path, required=True,
                     help="Directory containing blog.md / youtube.md / infographic.md / social.md")
    ap.add_argument("--report", type=Path, default=None,
                     help="grounding_report.json output path (default: <assets-dir>/grounding_report.json)")
    ap.add_argument("--strict", action="store_true", help="Exit 1 if any claim fails verification")
    args = ap.parse_args()

    if not args.transcript.exists():
        print(f"verify_grounding: transcript not found: {args.transcript}", file=sys.stderr)
        return 1
    if not args.event.exists():
        print(f"verify_grounding: event.json not found: {args.event}", file=sys.stderr)
        return 1

    transcript_text = args.transcript.read_text()
    event = json.loads(args.event.read_text())
    asset_texts = {}
    for name in ASSET_NAMES:
        p = args.assets_dir / name
        if p.exists():
            asset_texts[name] = p.read_text()
    if not asset_texts:
        print(f"verify_grounding: no assets found in {args.assets_dir}", file=sys.stderr)
        return 1

    report = check_assets(transcript_text, event, asset_texts)
    report_path = args.report or (args.assets_dir / "grounding_report.json")
    report_path.write_text(json.dumps(report, indent=2))

    t = report["totals"]
    status = "PASS" if report["overall_pass"] else "FAIL"
    print(f"M3 grounding check ({status}): {t['verified']}/{t['claims_checked']} claims verified, "
          f"{t['failed']} failed -> {report_path}")
    for name, r in report["assets"].items():
        if not r["pass"]:
            print(f"  {name}: {r['failed_count']}/{r['claims_checked']} failed")

    if args.strict and not report["overall_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
