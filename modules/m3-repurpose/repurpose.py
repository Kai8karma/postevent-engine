#!/usr/bin/env python3
"""M3 -- Content Repurposing (live by default).

Input : the real webinar recording (event.json.recording_files) + the real
        Sarvam transcript (data/incoming/transcript.md, absolute [MM:SS]
        timestamps) + the per-chapter Sarvam JSON (turn boundaries).
AI role: extract key moments / insights / verbatim quotes / data points, then
        generate one content package per channel.
Output : extraction.json, blog.md (800-1200w, hard gate), youtube.md
        (chapters + description + thumbnail brief), infographic.md,
        social.md (5-10 posts), visuals/*.png (real image model),
        clips/*.mp4 + *.srt (ffmpeg), grounding_report.json, manifest.json.

Lanes
  (default)      live: real OpenRouter calls, budget-capped at 8 per run.
  --live-dry-run builds every prompt with real data and prints it. Zero network.
  --offline      replays sample_output/ -- and only if .fingerprint.json matches
                 the transcript+event passed in. Mismatch = hard failure, never
                 a silent replay of someone else's event.

A successful live run refreshes sample_output/ + .fingerprint.json, so the
offline lane always replays *this* event's last real output.

LLM budget: LLM_CALL_BUDGET (8) HTTP calls per run, per-call wall clock
LLM_BATCH_DEADLINE_S (default 120s), model chain from OPENROUTER_MODEL
(default nvidia/nemotron-3-super-120b-a12b:free). Every call lands in
receipts/m3_llm_calls.json with ts/model/purpose/latency_ms/http_status/parse_ok.

Grounding: every quote the extraction produces is matched against the
transcript before any asset prompt sees it, and every claim in the four
written assets is re-checked by verify_grounding (--strict semantics: a miss
fails the run).

Usage:
    python3 repurpose.py --out out/m3 --clips --images
    python3 repurpose.py --out out/m3 --live-dry-run
    python3 repurpose.py --out out/m3 --offline
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import verify_grounding

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
SHARED_DIR = REPO_ROOT / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from utm import with_utm  # shared/utm.py -- canonical impl  # noqa: E402

DEFAULT_TRANSCRIPT = REPO_ROOT / "data" / "incoming" / "transcript.md"
DEFAULT_EVENT = REPO_ROOT / "data" / "incoming" / "event.json"
DEFAULT_SARVAM_DIR = REPO_ROOT / "out" / "receipts" / "transcription"
DEFAULT_MEDIA_DIR = REPO_ROOT / "data" / "incoming" / "media"
SAMPLE_DIR = MODULE_DIR / "sample_output"
PROMPTS_DIR = MODULE_DIR / "prompts"
FINGERPRINT_PATH = SAMPLE_DIR / ".fingerprint.json"

ASSETS = ["blog.md", "youtube.md", "infographic.md", "social.md"]
SAMPLE_FILES = ["extraction.json"] + ASSETS

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
LLM_CALL_BUDGET = 8

BLOG_MIN_WORDS, BLOG_MAX_WORDS = 800, 1200
MIN_CHAPTER_MARKERS = 3
MIN_INFOGRAPHIC_POINTS = 6
MIN_SOCIAL_POSTS, MAX_SOCIAL_POSTS = 5, 10
MIN_LINKEDIN, MIN_X = 3, 2


# --- fingerprint (offline replay guard) ----------------------------------

def compute_fingerprint(transcript_path: Path, event_path: Path) -> dict:
    """Hash of the exact inputs sample_output/ was generated from. The offline
    lane replays that output only when these match."""
    return {
        "transcript_sha256": hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        if transcript_path.exists() else "",
        "event_sha256": hashlib.sha256(event_path.read_bytes()).hexdigest()
        if event_path.exists() else "",
        "event_name": (json.loads(event_path.read_text()).get("event_name", "")
                       if event_path.exists() else ""),
    }


def check_fingerprint(transcript_path: Path, event_path: Path) -> None:
    current = compute_fingerprint(transcript_path, event_path)
    if not FINGERPRINT_PATH.exists():
        raise RuntimeError(
            "--offline: sample_output/.fingerprint.json is missing -- there is no verified "
            "cached run to replay. Run live once to produce one.")
    stored = json.loads(FINGERPRINT_PATH.read_text())
    drift = [k for k in ("transcript_sha256", "event_sha256") if stored.get(k) != current[k]]
    if drift:
        raise RuntimeError(
            f"--offline: cached sample_output was generated from different inputs ({', '.join(drift)} "
            f"differ; cached event = {stored.get('event_name')!r}, requested = {current['event_name']!r}). "
            "Replaying it would attribute one event's content to another. Run live instead.")
    missing = [n for n in SAMPLE_FILES if not (SAMPLE_DIR / n).exists()]
    if missing:
        raise RuntimeError(f"--offline: sample_output is incomplete: missing {', '.join(missing)}")


# --- event / transcript helpers ------------------------------------------

def event_slug(event: dict) -> str:
    """Canonical event tag (darwinbox-ai-in-hr-2026-08-13). event.json names it
    explicitly; the host+date derivation is only a fallback for events that don't."""
    if event.get("event_slug"):
        return event["event_slug"]
    host = event.get("host_domain", "event").split(".")[0]
    return f"{host}-{event.get('date', '')}".strip("-")


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-")


def campaign_slug(event: dict) -> str:
    """Mirror of m2-comms/comms.py's campaign_slug so both modules' links roll
    into the same campaign. If M2's slugging changes, change both."""
    name = event["event_name"].split(":", 1)[0]
    return f"{slugify(name)}-{event['date']}"


def chapter_table(event: dict, media_dir: Path) -> list:
    """The three real recording chapters with their absolute offsets into the
    transcript timeline (chapter N starts where chapters 1..N-1 ended)."""
    chapters, offset = [], 0.0
    for rec in event.get("recording_files", []):
        dur = float(rec["duration_sec"])
        chapters.append({
            "chapter": int(rec["chapter"]),
            "url": rec.get("url", ""),
            "duration_sec": dur,
            "start_abs": offset,
            "end_abs": offset + dur,
            "media_path": str(media_dir / f"chapter{int(rec['chapter'])}.mp4"),
        })
        offset += dur
    return chapters


def chapter_for(seconds: float, chapters: list):
    for ch in chapters:
        if ch["start_abs"] <= seconds < ch["end_abs"]:
            return ch
    return chapters[-1] if chapters else None


def parse_ts(ts: str):
    """'MM:SS' / 'HH:MM:SS' -> seconds. None when unparseable."""
    if not isinstance(ts, str):
        return None
    m = re.match(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*$", ts)
    if not m:
        return None
    h, mm, ss = m.groups()
    return int(h or 0) * 3600 + int(mm) * 60 + int(ss)


def fmt_ts(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def load_sarvam_turns(sarvam_dir: Path, chapters: list) -> list:
    """Sarvam diarized entries for all chapters, lifted to absolute seconds.
    These are the only real turn boundaries we have -- clips snap to them."""
    turns = []
    for ch in chapters:
        p = sarvam_dir / f"chapter{ch['chapter']}.sarvam.json"
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        for e in data.get("diarized_transcript", {}).get("entries", []):
            turns.append({
                "chapter": ch["chapter"],
                "start_abs": ch["start_abs"] + float(e["start_time_seconds"]),
                "end_abs": ch["start_abs"] + float(e["end_time_seconds"]),
                "speaker_id": e.get("speaker_id"),
                "text": e.get("transcript", ""),
            })
    turns.sort(key=lambda t: t["start_abs"])
    return turns


def transcript_timestamps(transcript_text: str, event: dict = None) -> list:
    """Every timestamp an asset is allowed to cite: the [MM:SS] turn starts in
    transcript.md plus the recording's own chapter boundaries (00:00 and the
    start of chapters 2/3). Identical to the set verify_grounding enforces."""
    # Derived from verify_grounding's own parser, not a second regex: the list a
    # prompt is handed and the set the gate enforces must be the same set, or the
    # model gets told a timestamp is legal and then fails for using it.
    stamps = {t["sec"] for t in verify_grounding.parse_transcript(transcript_text)}
    stamps |= verify_grounding.segment_boundary_seconds(transcript_text)
    if event:
        stamps |= verify_grounding.recording_boundary_seconds(event)
    return sorted(s for s in stamps if s is not None)


def snap_timestamp(seconds, valid: list):
    """Snap a model-proposed timestamp onto the nearest real transcript turn.
    Returns (snapped_seconds, drift) or (None, None) when nothing is close."""
    if seconds is None or not valid:
        return None, None
    best = min(valid, key=lambda v: abs(v - seconds))
    drift = abs(best - seconds)
    return (best, drift) if drift <= 45 else (None, None)


# --- UTM tagging ----------------------------------------------------------

CHANNEL_UTM = {
    "blog.md": {"utm_source": "blog", "utm_medium": "content"},
    "youtube.md": {"utm_source": "youtube", "utm_medium": "video"},
}
SOCIAL_PLATFORM_UTM = {
    "linkedin": {"utm_source": "linkedin", "utm_medium": "social"},
    "x": {"utm_source": "x", "utm_medium": "social"},
}
SOCIAL_POST_RE = re.compile(r"### Post (\d+).*?(?=### Post \d+|\Z)", re.S)
SOCIAL_PLATFORM_LINE_RE = re.compile(r"(?:\*\*|\*)?Platform:(?:\*\*|\*)?\s*(\S+)", re.I)
SOCIAL_HOOK_LINE_RE = re.compile(r"(?:\*\*|\*)?Hook style:(?:\*\*|\*)?\s*(.+)", re.I)


def _tag_social(text: str, campaign: str, recording_url: str) -> str:
    def replace_block(m: "re.Match") -> str:
        block, post_num = m.group(0), m.group(1)
        pm = SOCIAL_PLATFORM_LINE_RE.search(block)
        platform = (pm.group(1) if pm else "linkedin").strip().lower().strip("*")
        utm = SOCIAL_PLATFORM_UTM.get(platform, SOCIAL_PLATFORM_UTM["linkedin"])
        tagged = with_utm(recording_url, utm["utm_source"], utm["utm_medium"], campaign, f"social-post-{post_num}")
        return block.replace("[link]", f"[recording]({tagged})")
    return SOCIAL_POST_RE.sub(replace_block, text)


def add_utm_links(name: str, text: str, event: dict) -> str:
    """Tag outbound links with the same UTM taxonomy M2 uses. infographic.md
    carries no publishable link (its CTA is a design mockup) and is untouched."""
    recording_url = event.get("recording_url", "")
    if not recording_url:
        return text
    campaign = campaign_slug(event)
    if name == "blog.md":
        utm = CHANNEL_UTM["blog.md"]
        tagged = with_utm(recording_url, utm["utm_source"], utm["utm_medium"], campaign, "blog")
        return re.sub(r"\(" + re.escape(recording_url) + r"\)", f"({tagged})", text)
    if name == "youtube.md":
        utm = CHANNEL_UTM["youtube.md"]
        tagged = with_utm(recording_url, utm["utm_source"], utm["utm_medium"], campaign, "youtube-description")
        marker = f"**Recording link:** [Watch the full session]({tagged})"
        if "## Thumbnail Brief" in text:
            return text.replace("## Thumbnail Brief", marker + "\n\n## Thumbnail Brief", 1)
        return text.rstrip("\n") + "\n\n" + marker + "\n"
    if name == "social.md":
        return _tag_social(text, campaign, recording_url)
    return text


def word_count(text: str) -> int:
    return len(text.split())


def stamp(text: str, tag: str, mode: str) -> str:
    return f"<!-- event: {tag} | generated: {mode} -->\n" + text


# --- spec gate ------------------------------------------------------------
# Single source of truth for "in spec", shared by the regenerate loop and the
# manifest. blog.md is a HARD gate: out of range after one corrective retry
# fails the run (an 1,800-word "800-1200 word blog" is a broken deliverable,
# not a flagged one).
MAX_SPEC_ATTEMPTS = 2  # first attempt + one corrective retry
REQUIRED_SECTIONS = {
    "youtube.md": ["## Chapters", "## Description", "## Thumbnail Brief"],
    "infographic.md": ["## Headline Options", "## Data Points", "## Layout"],
}
HARD_GATE = {"blog.md"}
CHAPTER_LINE_RE = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)\s+\S", re.M)
INFOGRAPHIC_POINT_RE = re.compile(r"^\s*(?:[-*]\s*|\d+\.\s*)?\*\*.+?\*\*\s*[—–:-]", re.M)


def count_social_posts(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("### Post"))


def social_platform_counts(text: str) -> dict:
    counts = {"linkedin": 0, "x": 0, "other": 0}
    for m in SOCIAL_POST_RE.finditer(text):
        pm = SOCIAL_PLATFORM_LINE_RE.search(m.group(0))
        p = (pm.group(1).strip().lower().strip("*") if pm else "other")
        counts[p if p in counts else "other"] += 1
    return counts


def social_hooks(text: str) -> list:
    return [m.group(1).strip().strip("*").lower()
            for m in (SOCIAL_HOOK_LINE_RE.search(b.group(0)) for b in SOCIAL_POST_RE.finditer(text))
            if m]


def chapter_markers(text: str) -> list:
    section = text.split("## Description", 1)[0]
    return [m.group(1) for m in CHAPTER_LINE_RE.finditer(section)]


def check_asset_spec(name: str, text: str, valid_seconds=None) -> tuple:
    """(ok, detail) for one generated asset. `detail` is specific enough to
    paste straight into the corrective-retry prompt."""
    if name == "blog.md":
        n = word_count(text)
        if n < BLOG_MIN_WORDS:
            return False, f"blog draft was {n} words -- spec requires {BLOG_MIN_WORDS}-{BLOG_MAX_WORDS} -- add {BLOG_MIN_WORDS - n}+ words"
        if n > BLOG_MAX_WORDS:
            return False, f"blog draft was {n} words -- spec requires {BLOG_MIN_WORDS}-{BLOG_MAX_WORDS} -- cut {n - BLOG_MAX_WORDS}+ words"
        return True, ""
    if name == "social.md":
        n = count_social_posts(text)
        if n < MIN_SOCIAL_POSTS:
            return False, f"social.md had {n} post(s) -- spec requires {MIN_SOCIAL_POSTS}-{MAX_SOCIAL_POSTS} -- add {MIN_SOCIAL_POSTS - n} more"
        if n > MAX_SOCIAL_POSTS:
            return False, f"social.md had {n} post(s) -- spec requires {MIN_SOCIAL_POSTS}-{MAX_SOCIAL_POSTS} -- cut {n - MAX_SOCIAL_POSTS}"
        counts = social_platform_counts(text)
        if counts["linkedin"] < MIN_LINKEDIN or counts["x"] < MIN_X:
            return False, (f"social.md platform mix was LinkedIn={counts['linkedin']} X={counts['x']} "
                           f"(untagged={counts['other']}) -- spec requires at least {MIN_LINKEDIN} LinkedIn and "
                           f"{MIN_X} X, and every post must carry a `Platform:` line")
        hooks = social_hooks(text)
        if len(hooks) < n:
            return False, f"social.md: only {len(hooks)} of {n} posts carry a `Hook style:` line -- every post needs one"
        if len(set(hooks)) < len(hooks):
            dupes = sorted({h for h in hooks if hooks.count(h) > 1})
            return False, f"social.md reused hook style(s) {dupes} -- every post needs a distinct hook"
        return True, ""
    required = REQUIRED_SECTIONS.get(name)
    if required:
        missing = [h for h in required if h not in text]
        if missing:
            return False, f"{name} is missing required section(s): {', '.join(missing)}"
    if name == "youtube.md":
        marks = chapter_markers(text)
        if len(marks) < MIN_CHAPTER_MARKERS:
            return False, (f"youtube.md '## Chapters' had {len(marks)} marker(s) -- spec requires at least "
                           f"{MIN_CHAPTER_MARKERS}, one per recording chapter plus topic shifts, format `MM:SS Title`")
        if marks[0] not in ("00:00", "0:00"):
            return False, f"youtube.md chapters must start at 00:00 (first marker was {marks[0]})"
        if valid_seconds:
            bad = [t for t in marks if parse_ts(t) not in valid_seconds and parse_ts(t) != 0]
            if bad:
                return False, (f"youtube.md chapter timestamps {bad} do not appear in the transcript -- "
                               "every marker must be a timestamp that literally appears in it")
    if name == "infographic.md":
        pts = INFOGRAPHIC_POINT_RE.findall(text.split("## Data Points", 1)[-1].split("## Layout", 1)[0])
        if len(pts) < MIN_INFOGRAPHIC_POINTS:
            return False, (f"infographic.md '## Data Points' had {len(pts)} point(s) in the required "
                           f"`**<number>** — <what it measures> *<Speaker, Company [MM:SS]>*` format -- "
                           f"spec requires at least {MIN_INFOGRAPHIC_POINTS}")
    return True, ""


# --- OpenRouter plumbing + call budget ------------------------------------

class BudgetExceeded(RuntimeError):
    """Run asked for more LLM calls than LLM_CALL_BUDGET allows."""


class ModelRejected(RuntimeError):
    """This model did not produce a completion. `transient` marks the failures
    worth one immediate retry (connection reset, timeout, 429, 5xx) as opposed
    to a permanent rejection (unknown model id, 400)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class LLMLedger:
    """Call budget + receipt tape.

    Two separate ceilings, because they control different risks:
      * `budget` (8) caps COMPLETIONS -- the model outputs this run is allowed to
        consume: 1 extraction + 4 assets + up to 3 corrective regenerations.
      * `transient_allowance` (4) caps failed attempts that produced no output
        (provider 502s, connection resets, timeouts). Letting those eat the
        completion budget is how a flaky upstream silently costs you the
        corrective retry that fixes an ungrounded quote.
    Every attempt of either kind is one row in the receipt."""

    def __init__(self, budget: int = LLM_CALL_BUDGET, raw_dir: Path = None, transient_allowance: int = 4):
        self.budget = budget
        self.transient_allowance = transient_allowance
        self.calls = []
        self.raw_dir = raw_dir          # every raw completion is kept for inspection

    @property
    def completions(self) -> int:
        return sum(1 for c in self.calls if c.get("parse_ok"))

    @property
    def failures(self) -> int:
        return sum(1 for c in self.calls if not c.get("parse_ok"))

    @property
    def spent(self) -> int:
        """Completions -- what "LLM calls used" means in the manifest/summary."""
        return self.completions

    @property
    def attempts(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        return self.budget - self.completions

    def spend(self, purpose: str) -> None:
        if self.remaining <= 0:
            raise BudgetExceeded(
                f"LLM completion budget exhausted ({self.budget}) before '{purpose}' -- "
                "raise --budget only with a reason; the free tier is ~50 requests/day")
        if self.failures >= self.transient_allowance:
            raise BudgetExceeded(
                f"{self.failures} failed LLM attempts (allowance {self.transient_allowance}) before "
                f"'{purpose}' -- the provider is not answering; stopping rather than hammering it")

    def record(self, **row) -> None:
        row.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.calls.append(row)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "budget": self.budget,
            "completions": self.completions,
            "calls_made": self.completions,
            "failed_attempts": self.failures,
            "transient_allowance": self.transient_allowance,
            "http_attempts": self.attempts,
            "deadline_s": deadline_seconds(),
            "calls": self.calls,
        }, indent=2))


def deadline_seconds() -> int:
    try:
        return max(10, int(os.environ.get("LLM_BATCH_DEADLINE_S", "120")))
    except ValueError:
        return 120


def openrouter_key() -> str:
    """OPENROUTER_API_KEY env var, else ~/.config/postevent/llm.env. Never printed."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if OPENROUTER_CONFIG_PATH.exists():
        for line in OPENROUTER_CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "OPENROUTER_API_KEY":
                    return v.strip().strip('"').strip("'")
    return ""


def models_to_try() -> list:
    """OPENROUTER_MODEL is one id or a comma-separated chain, tried in order."""
    env_models = [m.strip() for m in os.environ.get("OPENROUTER_MODEL", "").split(",") if m.strip()]
    return env_models or [DEFAULT_MODEL]


def strip_json_fence(text: str) -> str:
    """First complete JSON value in the output. Tolerates fences, preamble and
    trailing prose (reasoning models routinely add a sentence after the fence)."""
    if not text:
        raise ValueError("LLM returned empty content")
    text = text.strip()
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                _, end = dec.raw_decode(text, i)
                return text[i:end]
            except ValueError:
                continue
    raise ValueError("no JSON value found in LLM output")


def _post(key: str, model: str, prompt: str, max_tokens: int, timeout: int) -> tuple:
    """One HTTP POST. Returns (content, http_status). Raises ModelRejected so
    the caller can fall through to the next model in the chain."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    # The default model is a reasoning model: left on, it spends the entire
    # completion budget thinking and returns truncated JSON (measured: 20,000
    # completion tokens, 4.6k chars of content, finish_reason=length). Off, the
    # same prompt answers directly. LLM_REASONING=on restores it.
    if os.environ.get("LLM_REASONING", "off").strip().lower() != "on":
        body["reasoning"] = {"enabled": False}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://kai8karma.github.io/agentkai/",
                 "X-Title": "Post-Event Engine -- M3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise ModelRejected(f"HTTP {e.code} for {model!r}: {body}",
                            transient=e.code == 429 or 500 <= e.code < 600) from None
    except TimeoutError:
        raise ModelRejected(f"{model!r} exceeded the {timeout}s per-call deadline", transient=True) from None
    except OSError as e:
        # urllib.error.URLError and ConnectionResetError both land here: the
        # request left the machine but no response came back.
        raise ModelRejected(f"transport error for {model!r}: {e}", transient=True) from None
    except json.JSONDecodeError as e:
        raise ModelRejected(f"{model!r} returned unparseable HTTP body: {e}", transient=True) from None
    # OpenRouter can return a provider error as a 200 with no choices.
    if isinstance(data, dict) and data.get("error"):
        # OpenRouter returns provider failures as a 200 with an error body; a
        # 429/5xx in there is the same transient situation as an HTTP 429/5xx.
        err = data["error"] or {}
        code = err.get("code") if isinstance(err.get("code"), int) else None
        message = str(err.get("message"))[:200]
        transient = (code == 429 or (code is not None and 500 <= code < 600)
                     or any(w in message.lower() for w in ("overload", "rate limit", "temporarily", "timeout")))
        raise ModelRejected(f"provider error for {model!r}: {code} {message}", transient=transient)
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ModelRejected(f"unexpected response shape from {model!r}: {e}") from None
    finish = data["choices"][0].get("finish_reason")
    if not content:
        raise ModelRejected(f"{model!r} returned empty content (finish_reason={finish})")
    if finish == "length":
        raise ModelRejected(f"{model!r} hit the {max_tokens}-token cap before finishing "
                            f"(finish_reason=length) -- output would be truncated")
    return content, status


def call_llm(prompt: str, purpose: str, ledger: LLMLedger, max_tokens: int = 8000) -> str:
    """Single chokepoint for every LLM call in this module. Budget-checked,
    deadline-bounded, receipted (one row per HTTP call), model chain from
    OPENROUTER_MODEL. Fails loud -- never returns a stub."""
    key = openrouter_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY (env or ~/.config/postevent/llm.env)")
    timeout = deadline_seconds()
    last_err = None
    for model in models_to_try():
        for attempt in (1, 2):
            ledger.spend(purpose)
            started = time.monotonic()
            try:
                content, status = _post(key, model, prompt, max_tokens, timeout)
            except ModelRejected as e:
                ledger.record(model=model, purpose=purpose, attempt=attempt,
                              latency_ms=int((time.monotonic() - started) * 1000),
                              http_status=_status_from_error(str(e)), parse_ok=False, error=str(e)[:300])
                last_err = e
                if e.transient and attempt == 1 and ledger.remaining > 0:
                    print(f"[llm] {purpose}: {e} -- one retry", file=sys.stderr)
                    time.sleep(8)
                    continue
                print(f"[llm] {purpose}: {e} -- next model in chain", file=sys.stderr)
                break
            raw_path = None
            if ledger.raw_dir:
                ledger.raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = ledger.raw_dir / f"{ledger.spent:02d}-{purpose.replace('.md', '').replace(' ', '-')}.txt"
                raw_path.write_text(content)
            ledger.record(model=model, purpose=purpose, attempt=attempt,
                          latency_ms=int((time.monotonic() - started) * 1000),
                          http_status=status, parse_ok=True, chars=len(content),
                          raw=str(raw_path.relative_to(ledger.raw_dir.parent.parent)) if raw_path else None)
            return content
    raise RuntimeError(f"all candidate models failed for '{purpose}': {last_err}")


def _status_from_error(msg: str):
    m = re.search(r"HTTP (\d{3})", msg)
    return int(m.group(1)) if m else None


def fill(template: str, **kw) -> str:
    for key, value in kw.items():
        template = template.replace("{{" + key + "}}", value)
    return template


# --- extraction -----------------------------------------------------------

EXTRACTION_MIN = {"moments": 6, "insights": 5, "quotes": 8, "data_points": 6}


def validate_extraction(obj) -> list:
    """Structural problems with the raw extraction JSON, phrased for the model."""
    problems = []
    if not isinstance(obj, dict):
        return ["top-level value must be a JSON object"]
    for key, minimum in EXTRACTION_MIN.items():
        items = obj.get(key)
        if not isinstance(items, list):
            problems.append(f"'{key}' must be a JSON array")
            continue
        if len(items) < minimum:
            problems.append(f"'{key}' had {len(items)} entries -- need at least {minimum}")
    for i, m in enumerate(obj.get("moments") or []):
        if not isinstance(m, dict):
            problems.append(f"moments[{i}] must be an object")
            continue
        for field in ("start", "end", "title", "speaker", "clip_worthiness"):
            if field not in m:
                problems.append(f"moments[{i}] is missing '{field}'")
        if parse_ts(m.get("start")) is None or parse_ts(m.get("end")) is None:
            problems.append(f"moments[{i}] start/end must be MM:SS strings that appear in the transcript")
    for i, q in enumerate(obj.get("quotes") or []):
        if not isinstance(q, dict) or not q.get("quote") or not q.get("speaker") or parse_ts(q.get("timestamp")) is None:
            problems.append(f"quotes[{i}] needs 'quote' (verbatim), 'speaker' and 'timestamp' (MM:SS)")
    for i, d in enumerate(obj.get("data_points") or []):
        if not isinstance(d, dict) or not d.get("value") or parse_ts(d.get("timestamp")) is None:
            problems.append(f"data_points[{i}] needs 'value', 'what', 'speaker' and 'timestamp' (MM:SS)")
    return problems[:12]


def locate_quote(quote: str, turns: list, windows: list):
    """Find where a quote actually lives in the transcript. Exact substring of a
    single turn first (gives the true speaker AND the true timestamp), fuzzy
    window match second. Returns (speaker, seconds, ratio) -- speaker/seconds are
    None when nothing matched well enough."""
    norm = " ".join(quote.split()).lower()
    for turn in turns:
        if norm and norm in " ".join(turn["text"].split()).lower():
            return turn["speaker"], turn["sec"], 1.0
    ratio, _snippet, label = verify_grounding.best_quote_match(quote, windows)
    if ratio < verify_grounding.QUOTE_RATIO_THRESHOLD:
        return None, None, ratio
    speaker = label.split("->")[0].strip() if label else None
    sec = next((t["sec"] for t in turns if t["speaker"] == speaker), None)
    return speaker, sec, ratio


def pick_clip_end(start: int, proposed_end, valid_seconds: list, chapter: dict):
    """A moment's end must itself be a real, citable timestamp inside the same
    chapter, 30-60s after the start -- downstream copy quotes these ranges."""
    upper = int(chapter["end_abs"]) if chapter else max(valid_seconds)
    window = [v for v in valid_seconds if start + 30 <= v <= min(start + 60, upper)]
    if proposed_end in window:
        return proposed_end
    if window:
        return min(window, key=lambda v: abs(v - (proposed_end or start + 45)))
    later = [v for v in valid_seconds if start < v <= upper]
    return min(later, key=lambda v: abs(v - (start + 45))) if later else None


def normalise_extraction(raw: dict, transcript_text: str, chapters: list, valid_seconds: list, turns: list) -> dict:
    """Pure-code pass over the model's extraction. Timestamps snap onto real
    transcript turns, speakers are taken from the transcript rather than from
    what the model asserted, moment windows are forced to 30-60s inside one
    chapter, and any quote that is not actually in the transcript is DROPPED --
    so no downstream prompt ever sees an ungrounded quote. Everything dropped or
    corrected is recorded, not hidden."""
    parsed_turns = verify_grounding.parse_transcript(transcript_text)
    windows = verify_grounding.build_windows(parsed_turns)
    speaker_at = {t["sec"]: t["speaker"] for t in parsed_turns}
    dropped = {"quotes": [], "moments": [], "data_points": []}
    corrections = []

    def snap(entry, field):
        sec, _ = snap_timestamp(parse_ts(entry.get(field)), valid_seconds)
        return sec

    def true_speaker(claimed, sec):
        actual = speaker_at.get(sec)
        if actual and claimed and actual.lower() != str(claimed).lower():
            corrections.append({"at": fmt_ts(sec), "claimed_speaker": claimed, "actual_speaker": actual})
        return actual or claimed or ""

    moments = []
    for m in raw.get("moments") or []:
        start = snap(m, "start")
        if start is None:
            dropped["moments"].append({"title": m.get("title"), "reason": "start timestamp not in transcript"})
            continue
        ch = chapter_for(start, chapters)
        if ch and start >= ch["end_abs"] - 30:   # too close to the chapter end to cut 30s
            dropped["moments"].append({"title": m.get("title"), "reason": "less than 30s left in the chapter"})
            continue
        end = pick_clip_end(start, snap(m, "end"), valid_seconds, ch)
        if end is None:
            dropped["moments"].append({"title": m.get("title"), "reason": "no valid 30-60s window after start"})
            continue
        try:
            worth = max(0.0, min(1.0, float(m.get("clip_worthiness", 0.5))))
        except (TypeError, ValueError):
            worth = 0.5
        moments.append({
            "title": str(m.get("title", ""))[:120], "speaker": true_speaker(m.get("speaker"), start),
            "why": m.get("why", m.get("summary", "")), "start": fmt_ts(start), "end": fmt_ts(end),
            "start_seconds": float(start), "end_seconds": float(end),
            "duration_seconds": float(end - start), "clip_worthiness": round(worth, 2),
            "chapter": ch["chapter"] if ch else None,
        })
    moments.sort(key=lambda m: -m["clip_worthiness"])

    quotes = []
    for q in raw.get("quotes") or []:
        text = (q.get("quote") or "").strip().strip('"\u201c\u201d')
        if len(text) < verify_grounding.MIN_QUOTE_CHARS:
            dropped["quotes"].append({"quote": text[:90], "reason": "shorter than the verifiable minimum"})
            continue
        speaker, sec, ratio = locate_quote(text, parsed_turns, windows)
        if sec is None:
            dropped["quotes"].append({"quote": text[:90], "reason": f"not verbatim in the transcript (best {ratio:.2f})"})
            continue
        ch = chapter_for(sec, chapters)
        quotes.append({"quote": text, "speaker": speaker, "timestamp": fmt_ts(sec),
                       "timestamp_seconds": float(sec), "topic": q.get("topic", ""),
                       "chapter": ch["chapter"] if ch else None, "match_ratio": round(ratio, 3)})

    data_points = []
    for d in raw.get("data_points") or []:
        sec = snap(d, "timestamp")
        if sec is None:
            dropped["data_points"].append({"value": str(d.get("value"))[:60], "reason": "timestamp not in transcript"})
            continue
        ch = chapter_for(sec, chapters)
        data_points.append({"value": str(d.get("value", "")).strip(), "what": d.get("what", d.get("context", "")),
                            "speaker": true_speaker(d.get("speaker"), sec), "timestamp": fmt_ts(sec),
                            "chapter": ch["chapter"] if ch else None})

    insights = []
    for ins in raw.get("insights") or []:
        if not isinstance(ins, dict):
            continue
        sec = snap(ins, "timestamp")
        insights.append({"insight": ins.get("insight", ins.get("summary", "")),
                         "speaker": true_speaker(ins.get("speaker"), sec) if sec is not None else ins.get("speaker", ""),
                         "timestamp": fmt_ts(sec) if sec is not None else None,
                         "chapter": chapter_for(sec, chapters)["chapter"] if sec is not None else None,
                         "so_what": ins.get("so_what", "")})

    return {"moments": moments, "insights": insights, "quotes": quotes, "data_points": data_points,
            "dropped": dropped, "speaker_corrections": corrections}


def build_extraction_context(extraction: dict, limit_quotes: int = 12) -> str:
    """Compact, verified-only view of the extraction handed to asset prompts."""
    return json.dumps({
        "moments": extraction["moments"][:10],
        "insights": extraction["insights"][:8],
        "quotes": extraction["quotes"][:limit_quotes],
        "data_points": extraction["data_points"][:12],
    }, indent=1)


def chapters_context(chapters: list) -> str:
    return "\n".join(
        f"- Chapter {c['chapter']}: {fmt_ts(c['start_abs'])}-{fmt_ts(c['end_abs'])} "
        f"({int(c['duration_sec'])}s of recording, file {Path(c['media_path']).name})"
        for c in chapters)


def timestamps_context(valid_seconds: list) -> str:
    return ", ".join(fmt_ts(s) for s in valid_seconds)


# --- prompt assembly ------------------------------------------------------

def build_prompts(transcript_text: str, event: dict, chapters: list, valid_seconds: list,
                  extraction_context: str) -> list:
    """(purpose, prompt) for every call the live lane makes, in order."""
    common = {
        "TRANSCRIPT": transcript_text,
        "EVENT_JSON": json.dumps(event, indent=2),
        "CHAPTERS": chapters_context(chapters),
        "VALID_TIMESTAMPS": timestamps_context(valid_seconds),
        "EXTRACTION": extraction_context,
    }
    out = [("extraction", fill((PROMPTS_DIR / "extraction.md").read_text(), **common))]
    for name in ASSETS:
        out.append((name, fill((PROMPTS_DIR / name).read_text(), **common)))
    return out


DRY_RUN_EXTRACTION_NOTE = (
    "[--live-dry-run: the extraction call is not executed (zero network). In a live run this "
    "slot holds the verified extraction JSON: moments (start/end/clip_worthiness), insights, "
    "transcript-verified verbatim quotes, and data points.]")


# --- live lane ------------------------------------------------------------

def generate_extraction(transcript_text: str, event: dict, chapters: list, valid_seconds: list,
                        turns: list, ledger: LLMLedger) -> dict:
    """Extraction call + schema validation + one corrective retry."""
    base = build_prompts(transcript_text, event, chapters, valid_seconds, "")[0][1]
    prompt, problems = base, []
    for attempt in (1, 2):
        raw_text = call_llm(prompt, "extraction", ledger, max_tokens=12000)
        try:
            obj = json.loads(strip_json_fence(raw_text))
            problems = validate_extraction(obj)
        except ValueError as e:
            obj, problems = None, [f"output was not parseable JSON: {e}"]
        if not problems:
            extraction = normalise_extraction(obj, transcript_text, chapters, valid_seconds, turns)
            if len(extraction["quotes"]) >= 3 and len(extraction["moments"]) >= 3:
                extraction["attempts"] = attempt
                return extraction
            problems = [f"after transcript verification only {len(extraction['quotes'])} quote(s) and "
                        f"{len(extraction['moments'])} moment(s) survived -- quotes must be copied "
                        "character-for-character from the transcript and timestamps must be real"]
        if attempt == 1:
            print(f"[extraction] attempt 1 rejected: {problems} -- one corrective retry", file=sys.stderr)
            prompt = base + ("\n\n---\nCORRECTION (your previous output was rejected): "
                             + "; ".join(problems) +
                             "\nReturn the ENTIRE JSON object again, fixing every point above. "
                             "Output JSON only, no prose.\n")
    raise RuntimeError(f"extraction failed schema validation after a corrective retry: {problems}")


def generate_asset(name: str, prompt: str, event: dict, valid_seconds: list, ledger: LLMLedger) -> tuple:
    """One asset + spec gate + one corrective retry. Returns (text, ok, detail, attempts)."""
    base, text, ok, detail = prompt, "", False, ""
    attempt = 0
    for attempt in range(1, MAX_SPEC_ATTEMPTS + 1):
        raw = call_llm(prompt, name, ledger, max_tokens=8000)
        text = add_utm_links(name, raw.strip(), event)
        ok, detail = check_asset_spec(name, text, valid_seconds)
        if ok:
            break
        if attempt < MAX_SPEC_ATTEMPTS:
            print(f"[spec gate] {name} attempt {attempt}: {detail} -- regenerating", file=sys.stderr)
            prompt = base + (f"\n\n---\nREGENERATION NOTE (attempt {attempt} was rejected by the spec gate): "
                             f"{detail}. Rewrite the ENTIRE asset honouring the output contract above -- "
                             "a full coherent replacement, not a patched draft.\n")
    return text, ok, detail, attempt


def run_live(transcript_text: str, event: dict, out_dir: Path, chapters=None, sarvam_dir: Path = DEFAULT_SARVAM_DIR,
             ledger: LLMLedger = None) -> dict:
    """Extraction -> 4 assets, budget-capped, spec-gated, chapter-aware."""
    ledger = ledger or LLMLedger()
    chapters = chapters if chapters is not None else chapter_table(event, DEFAULT_MEDIA_DIR)
    tag = event_slug(event)
    valid_seconds = transcript_timestamps(transcript_text, event)
    turns = load_sarvam_turns(sarvam_dir, chapters)

    extraction = generate_extraction(transcript_text, event, chapters, valid_seconds, turns, ledger)
    extraction["event_slug"] = tag
    extraction["chapters"] = [{k: c[k] for k in ("chapter", "start_abs", "end_abs", "duration_sec")} for c in chapters]
    (out_dir / "extraction.json").write_text(json.dumps(extraction, indent=2))
    print(f"[extraction] {len(extraction['moments'])} moments, {len(extraction['insights'])} insights, "
          f"{len(extraction['quotes'])} transcript-verified quotes "
          f"({len(extraction['dropped']['quotes'])} dropped), {len(extraction['data_points'])} data points")

    ctx = build_extraction_context(extraction)
    prompts = dict(build_prompts(transcript_text, event, chapters, valid_seconds, ctx))

    manifest_assets, spec_gate = {}, {}
    for name in ASSETS:
        text, ok, detail, attempts = generate_asset(name, prompts[name], event, valid_seconds, ledger)
        if not ok and name in HARD_GATE:
            (out_dir / name).write_text(stamp(text, tag, "live-REJECTED"))
            raise RuntimeError(
                f"{name} failed its hard spec gate after {attempts} attempts ({detail}). "
                f"The out-of-spec draft is at {out_dir / name} for inspection but the run is a failure -- "
                "M3 does not ship a blog outside 800-1200 words.")
        if not ok:
            print(f"[spec gate] {name}: still out of spec after {attempts} attempts ({detail})", file=sys.stderr)
        (out_dir / name).write_text(stamp(text, tag, "live"))
        manifest_assets[name] = {"path": str(out_dir / name), "words": word_count(text)}
        spec_gate[name] = {"attempts": attempts, "within_spec": ok, "detail": detail}

    return {"event_tag": tag, "event_slug": tag, "mode": "live", "assets": manifest_assets,
            "_spec_gate": spec_gate, "_extraction": extraction, "llm_calls_made": ledger.spent}


def run_offline(event: dict, out_dir: Path) -> dict:
    """Replay the last verified live run (fingerprint already checked)."""
    tag = event_slug(event)
    manifest_assets, spec_gate = {}, {}
    for name in SAMPLE_FILES:
        text = (SAMPLE_DIR / name).read_text()
        if name in ASSETS:
            body = re.sub(r"^<!-- event:.*?-->\n", "", text)
            ok, detail = check_asset_spec(name, body)
            if not ok:
                raise RuntimeError(f"--offline: cached {name} is out of spec ({detail}) -- "
                                   "the cache is only refreshed by a passing live run, so this means it was "
                                   "hand-edited. Run live.")
            (out_dir / name).write_text(stamp(body, tag, "offline-replay"))
            manifest_assets[name] = {"path": str(out_dir / name), "words": word_count(body)}
            spec_gate[name] = {"attempts": 0, "within_spec": True, "detail": ""}
        else:
            (out_dir / name).write_text(text)
    return {"event_tag": tag, "event_slug": tag, "mode": "offline", "assets": manifest_assets,
            "_spec_gate": spec_gate, "llm_calls_made": 0}


def run_live_dry_run(transcript_text: str, event: dict, out_dir: Path, chapters: list, quiet: bool) -> dict:
    """Every prompt the live lane would send, built with real data, printed and
    saved. Zero network calls -- nothing here touches call_llm."""
    valid_seconds = transcript_timestamps(transcript_text, event)
    prompts = build_prompts(transcript_text, event, chapters, valid_seconds, DRY_RUN_EXTRACTION_NOTE)
    dry_dir = out_dir / "dry-run"
    dry_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"event_slug": event_slug(event), "mode": "live-dry-run",
                "planned_calls": len(prompts), "budget": LLM_CALL_BUDGET,
                "max_calls_with_retries": len(prompts) + MAX_SPEC_ATTEMPTS + 1, "prompts": {}}
    for i, (purpose, prompt) in enumerate(prompts, 1):
        path = dry_dir / f"{i:02d}-{purpose.replace('.md', '')}.prompt.md"
        path.write_text(prompt)
        manifest["prompts"][purpose] = {"path": str(path), "words": word_count(prompt), "chars": len(prompt)}
        print(f"=== PROMPT {i}/{len(prompts)}: {purpose} ({word_count(prompt)}w, {len(prompt)} chars) ===")
        if not quiet:
            print(prompt)
    return manifest


# --- grounding ------------------------------------------------------------

def run_grounding(out_dir: Path, event: dict, transcript_text: str) -> dict:
    asset_texts = {n: (out_dir / n).read_text() for n in ASSETS if (out_dir / n).exists()}
    if not asset_texts:
        return {}
    report = verify_grounding.check_assets(transcript_text, event, asset_texts)
    (out_dir / "grounding_report.json").write_text(json.dumps(report, indent=2))
    t = report["totals"]
    print(f"M3 grounding ({'PASS' if report['overall_pass'] else 'FAIL'}): "
          f"{t['verified']}/{t['claims_checked']} claims verified, {t['failed']} failed "
          f"-> {out_dir / 'grounding_report.json'}")
    for name, r in report["assets"].items():
        if not r["pass"]:
            for c in r["failed"][:6]:
                print(f"  {name}: [{c['type']}] {c['text'][:110]!r} -- {c.get('reason', '')}", file=sys.stderr)
    return report


def repair_grounding(out_dir: Path, event: dict, transcript_text: str, report: dict, prompts: dict,
                     valid_seconds: list, ledger: LLMLedger) -> dict:
    """One corrective regeneration per ungrounded asset, budget permitting.
    Same shape as the spec gate's retry: name the exact failed claims."""
    failing = [n for n, r in report["assets"].items() if not r["pass"]]
    for name in failing:
        if ledger.remaining <= 0:
            print(f"[grounding] no LLM budget left to repair {name}", file=sys.stderr)
            break
        detail = "; ".join(f"[{c['type']}] {c['text'][:120]!r} ({c.get('reason', '')})"
                           for c in report["assets"][name]["failed"][:6])
        prompt = prompts[name] + (
            "\n\n---\nGROUNDING FAILURE (your previous draft was rejected): these claims could not be "
            f"verified against the transcript: {detail}. Rewrite the ENTIRE asset. Use ONLY verbatim quotes "
            "and timestamps that appear in the extraction/transcript above; if you are unsure a line was "
            "said exactly that way, paraphrase it without quotation marks.\n")
        raw = call_llm(prompt, f"{name} (grounding repair)", ledger, max_tokens=8000)
        text = add_utm_links(name, raw.strip(), event)
        ok, spec_detail = check_asset_spec(name, text, valid_seconds)
        if not ok:
            print(f"[grounding] repaired {name} broke the spec gate ({spec_detail}) -- keeping the original",
                  file=sys.stderr)
            continue
        (out_dir / name).write_text(stamp(text, event_slug(event), "live-grounding-repair"))
    return run_grounding(out_dir, event, transcript_text)


# --- sub-tools (images, clips) -------------------------------------------

def run_subtool(script: str, argv: list, label: str, timeout: int) -> dict:
    cmd = [sys.executable, str(MODULE_DIR / script)] + argv
    print(f"[{label}] {' '.join(Path(c).name if c.endswith('.py') else c for c in cmd[1:3])} ...")
    try:
        proc = subprocess.run(cmd, timeout=timeout)
        return {"returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        print(f"[{label}] timed out after {timeout}s", file=sys.stderr)
        return {"returncode": 124}


# --- manifest -------------------------------------------------------------

KIND_BY_NAME = {"blog.md": "blog", "youtube.md": "youtube", "infographic.md": "infographic",
                "social.md": "social", "extraction.json": "extraction", "manifest.json": "manifest",
                "grounding_report.json": "report"}


def build_manifest(out_dir: Path, event: dict, lane: str, text_model: str, image_model,
                   spec_check: dict, grounding: dict, ledger: LLMLedger, notes: list,
                   summary: dict) -> dict:
    """manifest.json per docs/module-api.md M3: every file names its source and
    whether it was grounding-checked. Nothing pretends a render is a generated
    image; `source` is one of llm | image_model | ffmpeg | sarvam | html_render."""
    files = []

    def add(path: Path, kind: str, source: str, grounded):
        if path.exists():
            files.append({"name": str(path.relative_to(out_dir)), "kind": kind, "bytes": path.stat().st_size,
                          "source": source, "grounded": grounded})

    grounded_assets = (grounding or {}).get("assets", {})
    for name in ASSETS:
        g = grounded_assets.get(name)
        add(out_dir / name, KIND_BY_NAME[name], "llm", bool(g["pass"]) if g else None)
    add(out_dir / "extraction.json", "extraction", "llm", True if grounding else None)
    add(out_dir / "grounding_report.json", "report", "llm", None)

    images_receipt = out_dir / "receipts" / "m3_images.json"
    if images_receipt.exists():
        notes.extend(json.loads(images_receipt.read_text()).get("notes", []))
    for png in sorted((out_dir / "visuals").glob("*.png")) if (out_dir / "visuals").is_dir() else []:
        add(png, "visual", "image_model", None)

    clips_dir = out_dir / "clips"
    if clips_dir.is_dir():
        for f in sorted(clips_dir.iterdir()):
            if f.suffix == ".mp4":
                add(f, "clip", "ffmpeg", True)
            elif f.suffix == ".srt":
                add(f, "caption", "sarvam", True)
            elif f.suffix == ".png":
                add(f, "visual", "ffmpeg", None)

    return {
        "event_slug": event_slug(event),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lane": lane,
        "models": {"text": text_model, "image": image_model},
        "files": files,
        "shared_drive": {"folder_url": None, "folder_id": None, "files": []},
        "summary": summary,
        "spec_check": spec_check,
        "grounding": ({"overall_pass": grounding["overall_pass"], "totals": grounding["totals"],
                       "report": "grounding_report.json"} if grounding else None),
        "llm": {"budget": ledger.budget, "calls_made": ledger.spent,
                "http_attempts": ledger.attempts, "failed_attempts": ledger.failures,
                "deadline_s": deadline_seconds(), "receipt": "receipts/m3_llm_calls.json"},
        "notes": notes,
        # Back-compat for api/run.py's summarize_m3(), which reads
        # manifest["assets"] and manifest["llm_calls_made"].
        "assets": {f["name"]: {"bytes": f["bytes"], "source": f["source"]} for f in files},
        "llm_calls_made": ledger.spent,
    }


def refresh_sample_output(out_dir: Path, transcript_path: Path, event_path: Path, model: str) -> list:
    """A passing live run becomes the offline lane's replay set."""
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in SAMPLE_FILES:
        src = out_dir / name
        if src.exists():
            shutil.copyfile(src, SAMPLE_DIR / name)
            copied.append(name)
    fp = compute_fingerprint(transcript_path, event_path)
    fp["refreshed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fp["model"] = model
    fp["files"] = copied
    FINGERPRINT_PATH.write_text(json.dumps(fp, indent=2))
    return copied


# --- orchestration --------------------------------------------------------

def run(args) -> int:
    if not args.event.exists():
        raise RuntimeError(f"event.json not found: {args.event}")
    event = json.loads(args.event.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "receipts").mkdir(exist_ok=True)
    chapters = chapter_table(event, args.media_dir)
    if not args.transcript.exists():
        raise RuntimeError(f"transcript not found: {args.transcript}")
    transcript_text = args.transcript.read_text()

    if args.live_dry_run:
        manifest = run_live_dry_run(transcript_text, event, args.out, chapters, args.quiet_prompts)
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"\nM3 live-dry-run: {manifest['planned_calls']} prompts built from real data, "
              f"zero network calls, budget {LLM_CALL_BUDGET} "
              f"(worst case with corrective retries: {manifest['max_calls_with_retries']}). "
              f"Prompts -> {args.out / 'dry-run'}")
        return 0

    ledger = LLMLedger(args.budget, raw_dir=args.out / "receipts" / "raw")
    notes, valid_seconds = [], transcript_timestamps(transcript_text, event)
    if args.offline:
        check_fingerprint(args.transcript, args.event)
        result = run_offline(event, args.out)
        notes.append("offline replay of the last passing live run (fingerprint-matched); "
                     "visuals and clips are live-only outputs and are not replayed")
        text_model = json.loads(FINGERPRINT_PATH.read_text()).get("model", "see sample_output/.fingerprint.json")
    else:
        try:
            result = run_live(transcript_text, event, args.out, chapters, args.sarvam_dir, ledger)
        finally:
            ledger.write(args.out / "receipts" / "m3_llm_calls.json")
        text_model = next((c["model"] for c in reversed(ledger.calls) if c.get("parse_ok")), models_to_try()[0])

    spec_gate = result.pop("_spec_gate", {})
    extraction = result.pop("_extraction", None)
    if extraction is None and (args.out / "extraction.json").exists():
        extraction = json.loads((args.out / "extraction.json").read_text())

    # Grounding is a gate, not an annotation: a miss fails the run (with one
    # corrective regeneration first if there is budget for it).
    report = run_grounding(args.out, event, transcript_text)
    if report and not report["overall_pass"] and not args.offline and ledger.remaining > 0:
        prompts = dict(build_prompts(transcript_text, event, chapters, valid_seconds,
                                     build_extraction_context(extraction)))
        report = repair_grounding(args.out, event, transcript_text, report, prompts, valid_seconds, ledger)
        ledger.write(args.out / "receipts" / "m3_llm_calls.json")

    image_model = None
    if args.images and not args.offline:
        rc = run_subtool("gen_visuals.py", ["--out", str(args.out), "--extraction", str(args.out / "extraction.json"),
                                            "--youtube", str(args.out / "youtube.md"), "--event", str(args.event)],
                         "images", timeout=420)
        receipt = args.out / "receipts" / "m3_images.json"
        if receipt.exists():
            image_model = json.loads(receipt.read_text()).get("model")
        elif rc["returncode"] != 0:
            notes.append("image generation produced no receipt (gen_visuals.py failed) -- no visuals shipped")
    elif args.images:
        notes.append("--images ignored in --offline mode (image generation is a live-only call)")

    if args.clips and not args.offline:
        rc = run_subtool("make_clips.py", ["--out", str(args.out), "--extraction", str(args.out / "extraction.json"),
                                           "--event", str(args.event), "--sarvam-dir", str(args.sarvam_dir),
                                           "--media-dir", str(args.media_dir)], "clips", timeout=900)
        if rc["returncode"] != 0:
            raise RuntimeError(f"make_clips.py exited {rc['returncode']} -- clips were requested and did not ship")
    elif args.clips:
        notes.append("--clips ignored in --offline mode (clips are cut from the recording by ffmpeg, live only)")

    # Spec numbers, read back off disk so the manifest reports what shipped.
    blog_words = result["assets"].get("blog.md", {}).get("words", 0)
    social_text = (args.out / "social.md").read_text() if (args.out / "social.md").exists() else ""
    yt_text = (args.out / "youtube.md").read_text() if (args.out / "youtube.md").exists() else ""
    info_text = (args.out / "infographic.md").read_text() if (args.out / "infographic.md").exists() else ""
    posts = count_social_posts(social_text)
    platforms = social_platform_counts(social_text)
    markers = chapter_markers(yt_text)
    info_points = len(INFOGRAPHIC_POINT_RE.findall(
        info_text.split("## Data Points", 1)[-1].split("## Layout", 1)[0]))
    spec_check = {
        "blog_words": blog_words, "blog_spec": f"{BLOG_MIN_WORDS}-{BLOG_MAX_WORDS}",
        "blog_within_spec": BLOG_MIN_WORDS <= blog_words <= BLOG_MAX_WORDS,
        "social_posts": posts, "social_spec": f"{MIN_SOCIAL_POSTS}-{MAX_SOCIAL_POSTS}",
        "social_within_spec": MIN_SOCIAL_POSTS <= posts <= MAX_SOCIAL_POSTS
                              and platforms["linkedin"] >= MIN_LINKEDIN and platforms["x"] >= MIN_X,
        "social_platforms": platforms,
        "youtube_chapter_markers": len(markers),
        "youtube_sections_ok": spec_gate.get("youtube.md", {}).get("within_spec", len(markers) >= MIN_CHAPTER_MARKERS),
        "infographic_data_points": info_points,
        "infographic_sections_ok": spec_gate.get("infographic.md", {}).get("within_spec",
                                                                           info_points >= MIN_INFOGRAPHIC_POINTS),
        "gate": spec_gate,
    }

    clips_receipt = args.out / "receipts" / "m3_clips.json"
    images_receipt = args.out / "receipts" / "m3_images.json"
    summary = {
        "blog_words": blog_words,
        "chapters": len(markers),
        "posts": {"linkedin": platforms["linkedin"], "x": platforms["x"]},
        "images": sum(1 for i in json.loads(images_receipt.read_text()).get("images", []) if i.get("ok")) if images_receipt.exists() else 0,
        "clips": len(json.loads(clips_receipt.read_text()).get("clips", [])) if clips_receipt.exists() else 0,
        "grounding": {"checked": report["totals"]["claims_checked"], "passed": report["totals"]["verified"]}
        if report else None,
        "lane": result["mode"],
        "models": {"text": text_model, "image": image_model},
        "extraction": {k: len(extraction[k]) for k in ("moments", "insights", "quotes", "data_points")}
        if extraction else None,
    }

    manifest = build_manifest(args.out, event, result["mode"], text_model, image_model,
                              spec_check, report, ledger, notes, summary)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if report and not report["overall_pass"]:
        raise RuntimeError(
            f"grounding gate: {report['totals']['failed']} claim(s) in the generated assets do not verify "
            f"against the transcript -- see {args.out / 'grounding_report.json'}. The run is a failure; "
            "the drafts are on disk for inspection but must not ship.")
    if not spec_check["blog_within_spec"]:
        raise RuntimeError(f"blog.md shipped at {blog_words} words, outside {BLOG_MIN_WORDS}-{BLOG_MAX_WORDS}")

    if not args.offline and not args.no_refresh_sample:
        copied = refresh_sample_output(args.out, args.transcript, args.event, text_model)
        print(f"[sample] refreshed sample_output/ from this run: {', '.join(copied)}")

    print(f"M3 repurpose ({manifest['lane']}): blog {blog_words}w, {len(markers)} chapter markers, "
          f"{info_points} infographic data points, {posts} social posts "
          f"(LinkedIn {platforms['linkedin']} / X {platforms['x']}), "
          f"{summary['images']} image(s), {summary['clips']} clip(s), "
          f"{ledger.spent}/{ledger.budget} LLM completions "
          f"({ledger.attempts} HTTP attempts) -> {args.out}")
    return 0


def main():
    p = argparse.ArgumentParser(description="M3: webinar recording + transcript -> multi-format content package.")
    p.add_argument("--out", type=Path, required=True, help="Output directory")
    p.add_argument("--transcript", type=Path, default=DEFAULT_TRANSCRIPT)
    p.add_argument("--event", type=Path, default=DEFAULT_EVENT)
    p.add_argument("--sarvam-dir", type=Path, default=DEFAULT_SARVAM_DIR, dest="sarvam_dir",
                   help="Per-chapter Sarvam JSON (turn boundaries used for clip cuts + captions)")
    p.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR, dest="media_dir",
                   help="Local chapter MP4s; downloaded from event.json recording_files when absent")
    p.add_argument("--offline", action="store_true",
                   help="Replay sample_output/ instead of calling the model. Only runs if "
                        "sample_output/.fingerprint.json matches this transcript+event; otherwise fails.")
    p.add_argument("--live", action="store_true",
                   help="Accepted for compatibility -- live is the default lane and this flag is a no-op.")
    p.add_argument("--live-dry-run", dest="live_dry_run", action="store_true",
                   help="Build and print every live prompt from real data. Zero network calls.")
    p.add_argument("--quiet-prompts", dest="quiet_prompts", action="store_true",
                   help="With --live-dry-run, print only the per-prompt headers (prompts still written to disk).")
    p.add_argument("--clips", action="store_true", help="Cut the top 3 moments into 16:9 + 9:16 clips (ffmpeg)")
    p.add_argument("--images", action="store_true", help="Generate thumbnail + 2 social visuals via an image model")
    p.add_argument("--budget", type=int, default=LLM_CALL_BUDGET, help=f"LLM call ceiling (default {LLM_CALL_BUDGET})")
    p.add_argument("--no-refresh-sample", dest="no_refresh_sample", action="store_true",
                   help="Do not overwrite sample_output/ with this run's output")
    p.add_argument("--allow-stale", dest="allow_stale", action="store_true",
                   help="Retired. Accepted so older callers do not crash, but it no longer bypasses the "
                        "offline fingerprint check -- a mismatch always fails.")
    p.add_argument("--strict-grounding", dest="strict_grounding", action="store_true",
                   help="Retired. Accepted for compatibility: grounding is always a hard gate now.")
    args = p.parse_args()
    if args.allow_stale:
        print("WARNING: --allow-stale is retired -- the offline fingerprint check cannot be bypassed.",
              file=sys.stderr)

    try:
        return run(args)
    except (RuntimeError, BudgetExceeded) as e:
        print(f"M3 FAILED: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"M3 FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
