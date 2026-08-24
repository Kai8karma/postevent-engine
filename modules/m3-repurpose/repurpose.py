#!/usr/bin/env python3
"""M3 -- Content Repurposing.

Turns a webinar transcript into a multi-format content package: blog draft,
YouTube chapters/description/thumbnail brief, infographic outline, and 8
social posts (5 LinkedIn, 3 X).

Offline (default): copies sample_output/ into --out, stamps each asset with
an event tag, and writes manifest.json describing what shipped.

--live: runs the real pipeline -- one `claude -p` extraction pass over the
transcript using prompts/extraction.md, then one `claude -p` call per asset
type (blog/youtube/infographic/social) using prompts/<asset>.md with
{{TRANSCRIPT}}, {{EVENT_JSON}}, {{EXTRACTION}} substituted in. Any failure
(missing binary, timeout, non-zero exit incl. auth) fails loud -- clear
stderr message, exit 1 -- rather than silently writing nothing.

Every asset is checked against check_asset_spec() BEFORE it is written to
disk (blog: 800-1200 words, social: 5-10 posts, youtube/infographic: their
fixed 3-section contracts). A failure regenerates with the specific reason
appended to the prompt ("blog was 1277 words, cap is 1200 -- cut 77+
words"), up to 3 attempts; if still out of spec after 3, the asset ships
anyway with a loud stderr warning and manifest.json's spec_check block
flags it -- never silently.

--live-dry-run: builds all 5 real prompts (extraction + 4 assets) with real
transcript/event data substituted in and writes them to <out>/dry-run/, but
never calls claude -p. Zero network calls -- use to verify the --live path
is wired correctly when auth is unavailable.

Python 3 stdlib only. Zero network calls in the default (offline) path.

Usage:
    python3 repurpose.py --out out/m3
    python3 repurpose.py --transcript data/incoming/transcript.md \
        --event data/incoming/event.json --out out/m3 --live
    python3 repurpose.py --out out/m3 --live-dry-run
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import verify_grounding

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
DEFAULT_TRANSCRIPT = REPO_ROOT / "data" / "incoming" / "transcript.md"
DEFAULT_EVENT = REPO_ROOT / "data" / "incoming" / "event.json"
SAMPLE_DIR = MODULE_DIR / "sample_output"
PROMPTS_DIR = MODULE_DIR / "prompts"
FINGERPRINT_PATH = SAMPLE_DIR / ".fingerprint.json"

ASSETS = ["blog.md", "youtube.md", "infographic.md", "social.md"]

# --- OpenRouter fallback backend for --live (see LLM_BACKEND below) ---
# Verified live against https://openrouter.ai/api/v1/models on 2026-08-23:
# anthropic/claude-3.7-sonnet and anthropic/claude-3.5-sonnet no longer exist
# on OpenRouter (404), so the fallback chain below uses the three current
# Anthropic Sonnet ids instead, newest first.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL_FALLBACKS = [
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-sonnet-4.5",
]
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"


def compute_fingerprint(transcript_path: Path, event_path: Path) -> dict:
    """Hash of the exact inputs sample_output/*.md was authored against --
    the frankenstein guard's ground truth for offline mode."""
    transcript_bytes = transcript_path.read_bytes() if transcript_path.exists() else b""
    event_name = ""
    if event_path.exists():
        try:
            event_name = json.loads(event_path.read_text()).get("event_name", "")
        except json.JSONDecodeError:
            event_name = ""
    return {
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "event_name": event_name,
    }


def check_fingerprint(transcript_path: Path, event_path: Path, allow_stale: bool) -> None:
    """Offline mode (run_offline) replays sample_output/*.md verbatim against
    whatever event.json was passed in. Refuse to do that silently if the
    caller swapped events without --live -- see PLAN.md punch-list #1 /
    judge finding: FRANKENSTEIN GUARD."""
    current = compute_fingerprint(transcript_path, event_path)
    stale_msg = (
        "cached AI samples were generated from a different event transcript -- "
        "run with --live to regenerate (or pass --allow-stale to force)"
    )
    if not FINGERPRINT_PATH.exists():
        if allow_stale:
            print(f"WARNING: no fingerprint on record -- {stale_msg}", file=sys.stderr)
            return
        raise RuntimeError(stale_msg)
    stored = json.loads(FINGERPRINT_PATH.read_text())
    if (stored.get("transcript_sha256") != current["transcript_sha256"]
            or stored.get("event_name") != current["event_name"]):
        if allow_stale:
            print(f"WARNING: {stale_msg} -- proceeding anyway (--allow-stale)", file=sys.stderr)
            return
        raise RuntimeError(stale_msg)


def event_tag(event: dict) -> str:
    """Short tag stamped into each asset, e.g. acmerevenue-2026-08-19."""
    domain = event.get("host_domain", "event").split(".")[0]
    date = event.get("date", "")
    return f"{domain}-{date}".strip("-")


# --- UTM tagging (see README.md for the full table) ---
# slugify() / campaign_slug() are a byte-for-byte mirror of
# modules/m2-comms/comms.py's functions of the same name -- M3 doesn't
# import M2 (kept independent per the module boundary) but must produce the
# identical campaign slug so both modules' links roll into the same
# HubSpot/analytics campaign. If M2's slugging logic ever changes, update
# both. with_utm() generalizes M2's version (which hardcodes
# source=webinar/medium=email, M2's only channel) to accept source/medium
# per channel -- same four utm_* keys, same campaign format, not a second
# scheme.
CHANNEL_UTM = {
    "blog.md": {"utm_source": "blog", "utm_medium": "content"},
    "youtube.md": {"utm_source": "youtube", "utm_medium": "video"},
}
SOCIAL_PLATFORM_UTM = {
    "linkedin": {"utm_source": "linkedin", "utm_medium": "social"},
    "x": {"utm_source": "x", "utm_medium": "social"},
}


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-")


def campaign_slug(event: dict) -> str:
    name = event["event_name"].split(":", 1)[0]
    return f"{slugify(name)}-{event['date']}"


def with_utm(url: str, utm_source: str, utm_medium: str, campaign: str, content: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q.update({
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": campaign,
        "utm_content": content,
    })
    return urlunparse(parts._replace(query=urlencode(q)))


SOCIAL_POST_RE = re.compile(r"### Post (\d+).*?(?=### Post \d+|\Z)", re.S)
SOCIAL_PLATFORM_LINE_RE = re.compile(r"\*\*Platform:\*\*\s*(\S+)")


def _tag_social(text: str, campaign: str, recording_url: str) -> str:
    """Every social post ends its copy with the placeholder link token
    `[link]` (prompts/social.md's output contract) -- swap each one for a
    tracked link, per-post utm_content, platform-specific utm_source."""
    def replace_block(m: "re.Match") -> str:
        block = m.group(0)
        post_num = m.group(1)
        pm = SOCIAL_PLATFORM_LINE_RE.search(block)
        platform = (pm.group(1) if pm else "linkedin").strip().lower()
        utm = SOCIAL_PLATFORM_UTM.get(platform, SOCIAL_PLATFORM_UTM["linkedin"])
        tagged = with_utm(recording_url, utm["utm_source"], utm["utm_medium"], campaign, f"social-post-{post_num}")
        return block.replace("[link]", f"[recording]({tagged})")
    return SOCIAL_POST_RE.sub(replace_block, text)


def add_utm_links(name: str, text: str, event: dict) -> str:
    """Tags every outbound link in blog.md / youtube.md / social.md with UTM
    params matching M2's taxonomy. infographic.md carries no real outbound
    link (its CTA text is a design mockup, not a publishable link) and is
    left untouched. No-op if the event has no recording_url to tag."""
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
    """Prefix an asset with a machine-traceable event stamp (invisible on render)."""
    return f"<!-- event: {tag} | generated: {mode} -->\n" + text


# --- Spec gate (SPEC.md M3: blog 800-1200 words, 5-10 social posts, and the
# fixed 3-section output contracts prompts/youtube.md and prompts/
# infographic.md both mandate). check_asset_spec() is the single source of
# truth both run_live()'s regenerate loop and run()'s manifest["spec_check"]
# report from -- one definition, so "what counts as in spec" can't drift
# between the gate and the report. ---
MAX_SPEC_ATTEMPTS = 3
REQUIRED_SECTIONS = {
    "youtube.md": ["## Chapters", "## Description", "## Thumbnail Brief"],
    "infographic.md": ["## Headline Options", "## Data Points", "## Layout"],
}


def check_asset_spec(name: str, text: str) -> tuple:
    """Structural/numeric spec check for one generated asset's body text
    (post-UTM, pre-stamp). Returns (ok, detail) -- detail is a SPECIFIC,
    human-readable description of the failure (exact overage/shortfall,
    named missing sections), not a generic "out of spec" -- it gets fed
    verbatim into the --live regenerate prompt so the model has something
    actionable to fix, and into manifest.json for a human reviewer.
    detail == "" when ok is True."""
    if name == "blog.md":
        n = word_count(text)
        if n < 800:
            return False, f"blog draft was {n} words -- spec requires 800-1200 -- add {800 - n}+ words"
        if n > 1200:
            return False, f"blog draft was {n} words -- spec requires 800-1200 -- cut {n - 1200}+ words"
        return True, ""
    if name == "social.md":
        n = sum(1 for line in text.splitlines() if line.startswith("### Post"))
        if n < 5:
            return False, f"social.md had {n} post(s) -- spec requires 5-10 -- add {5 - n} more post(s)"
        if n > 10:
            return False, f"social.md had {n} post(s) -- spec requires 5-10 -- cut {n - 10} post(s)"
        return True, ""
    required = REQUIRED_SECTIONS.get(name)
    if required:
        missing = [h for h in required if h not in text]
        if missing:
            return False, f"{name} is missing required section(s): {', '.join(missing)}"
        return True, ""
    return True, ""


def run_offline(event: dict, out_dir: Path) -> dict:
    tag = event_tag(event)
    manifest = {"event_tag": tag, "mode": "offline", "assets": {}}
    spec_gate = {}
    for name in ASSETS:
        text = add_utm_links(name, (SAMPLE_DIR / name).read_text(), event)
        ok, detail = check_asset_spec(name, text)
        if not ok:
            # Offline mode makes zero LLM calls by design (see module
            # docstring) -- there is nothing to regenerate against, so a
            # spec violation here means sample_output/<name> itself needs
            # editing. Fail loud on stderr rather than silently shipping a
            # canned sample that's out of spec.
            print(f"[spec gate] {name}: offline sample violates spec ({detail}) -- "
                  f"cannot regenerate (offline mode is a fixed replay, zero LLM calls) -- "
                  f"edit sample_output/{name} or run --live", file=sys.stderr)
        (out_dir / name).write_text(stamp(text, tag, "offline-sample"))
        manifest["assets"][name] = {"path": str(out_dir / name), "words": word_count(text)}
        spec_gate[name] = {"attempts": 1, "within_spec": ok, "detail": detail}
    manifest["_spec_gate"] = spec_gate
    # Rendered visual assets (thumbnail, quote card) ride along when present;
    # produced by tools/render_visuals.py from the thumbnail brief in youtube.md.
    visuals_dir = SAMPLE_DIR / "visuals"
    if visuals_dir.is_dir():
        (out_dir / "visuals").mkdir(parents=True, exist_ok=True)
        for png in sorted(visuals_dir.glob("*.png")):
            (out_dir / "visuals" / png.name).write_bytes(png.read_bytes())
            manifest["assets"][f"visuals/{png.name}"] = {"path": str(out_dir / "visuals" / png.name), "bytes": png.stat().st_size}
    return manifest


def _openrouter_key() -> str:
    """OPENROUTER_API_KEY env var, else a KEY=VALUE line in
    ~/.config/postevent/llm.env. Never logged/printed."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    if OPENROUTER_CONFIG_PATH.exists():
        for line in OPENROUTER_CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OPENROUTER_API_KEY":
                return v.strip().strip('"').strip("'")
    return ""


class _OpenRouterModelError(RuntimeError):
    """Model id rejected by OpenRouter (400/404) -- caller tries the next
    candidate in OPENROUTER_MODEL_FALLBACKS instead of giving up."""


def _strip_json_fence(text: str) -> str:
    """Return the first complete JSON value (object or array) inside the LLM
    output as text. Tolerates markdown fences, a preamble, and trailing prose --
    reasoning-style models (e.g. OpenRouter stealth/ox-alpha) routinely add a
    sentence after the closing fence, which breaks a bare json.loads."""
    if text is None:
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


def _openrouter_models_to_try() -> list:
    """OPENROUTER_MODEL may be one id or a comma-separated chain
    ("a:free,b:free,c") -- tried in order; a model that 400/404s, or that is
    still rate-limited/overloaded after retries, hands off to the next."""
    env_models = [m.strip() for m in os.environ.get("OPENROUTER_MODEL", "").split(",") if m.strip()]
    if env_models:
        return env_models + [m for m in OPENROUTER_MODEL_FALLBACKS if m not in env_models]
    return list(OPENROUTER_MODEL_FALLBACKS)


def _openrouter_request(key: str, model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 12000,  # reasoning models spend completion tokens thinking before the JSON
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://kai8karma.github.io/agentkai/",
            "X-Title": "Post-Event Engine",
        },
    )
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:  # ox-alpha ~2 min/call
                data = json.loads(resp.read().decode("utf-8"))
            # OpenRouter can return a provider/rate-limit error as a 200 with
            # {"error": {...}} and no "choices" (seen on free-tier models).
            if isinstance(data, dict) and data.get("error"):
                err = data["error"] or {}
                code = err.get("code")
                msg = f"openrouter: provider error for {model!r}: {code} {str(err.get('message'))[:200]}"
                if attempt < 3:
                    time.sleep(5 * attempt)
                    continue
                raise _OpenRouterModelError(msg)  # exhausted -> let the caller try the next model
            try:
                content = data["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                raise RuntimeError(f"openrouter: unexpected response shape: {e}") from e
            if not content:
                fr = data["choices"][0].get("finish_reason")
                raise RuntimeError(f"openrouter: model {model!r} returned empty content (finish_reason={fr}); raise max_tokens or use a non-reasoning model")
            return content
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (400, 404):
                raise _OpenRouterModelError(f"model {model!r} rejected (HTTP {e.code}): {body}") from None
            if e.code == 429 or 500 <= e.code < 600:
                if attempt < 3:
                    time.sleep(5 * attempt)
                    continue
                raise _OpenRouterModelError(f"openrouter HTTP {e.code} for {model!r} (after retries): {body}") from None
            raise RuntimeError(f"openrouter HTTP {e.code} for {model!r}: {body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"openrouter request failed: {e.reason}") from None
    raise RuntimeError(f"openrouter: exhausted retries for {model!r}")


def _call_openrouter(prompt: str) -> str:
    key = _openrouter_key()
    if not key:
        raise RuntimeError(
            "OpenRouter API key not found (set OPENROUTER_API_KEY or ~/.config/postevent/llm.env)"
        )
    last_err = None
    for model in _openrouter_models_to_try():
        try:
            return _openrouter_request(key, model, prompt)
        except _OpenRouterModelError as e:
            last_err = e
            continue
    raise RuntimeError(f"openrouter: all candidate models failed: {last_err}")


def _call_claude_cli(prompt: str) -> str:
    """`claude -p` backend. USER must not propagate (keychain 401 quirk)."""
    env = os.environ.copy()
    env.pop("USER", None)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, env=env, timeout=180,
        )
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found on PATH -- install/auth it, or run without --live") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude -p timed out after 180s") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (exit {proc.returncode}) -- check `claude auth status` / "
            f"`claude login`: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()


def call_claude(prompt: str) -> str:
    """Single chokepoint for --live LLM calls, dispatched via LLM_BACKEND (env:
    auto|claude|openrouter; default auto). auto tries `claude -p` first,
    falling back to OpenRouter only if a key is available; explicit
    claude/openrouter skip straight to that backend. Raises RuntimeError with
    an actionable message when nothing works -- never called in
    --live-dry-run mode, and not caught anywhere between here and main(), so
    a --live failure fails loud (clear message, exit 1) instead of silently
    writing nothing."""
    backend = os.environ.get("LLM_BACKEND", "auto").strip().lower()
    if backend not in ("auto", "claude", "openrouter"):
        backend = "auto"

    claude_err = None
    if backend in ("auto", "claude"):
        try:
            return _call_claude_cli(prompt)
        except RuntimeError as e:
            claude_err = e
            if backend == "claude":
                raise

    key = _openrouter_key()
    if backend == "openrouter" and not key:
        raise RuntimeError(
            "LLM_BACKEND=openrouter but no OpenRouter key found "
            "(set OPENROUTER_API_KEY or ~/.config/postevent/llm.env)"
        )
    if key:
        try:
            return _call_openrouter(prompt)
        except RuntimeError as e:
            if claude_err is not None:
                raise RuntimeError(f"claude -p failed ({claude_err}); openrouter fallback also failed: {e}") from e
            raise

    if claude_err is not None:
        raise claude_err
    raise RuntimeError("no LLM backend available -- authenticate the `claude` CLI or set OPENROUTER_API_KEY")


def fill(template: str, transcript: str, event_json: str, extraction: str = "") -> str:
    return (template.replace("{{TRANSCRIPT}}", transcript)
                    .replace("{{EVENT_JSON}}", event_json)
                    .replace("{{EXTRACTION}}", extraction))


def run_live(transcript_text: str, event: dict, out_dir: Path) -> dict:
    tag = event_tag(event)
    event_json = json.dumps(event, indent=2)
    mode_label = f"live-{os.environ.get('LLM_BACKEND', 'auto').strip().lower() or 'auto'}"

    extraction_prompt = fill((PROMPTS_DIR / "extraction.md").read_text(), transcript_text, event_json)
    extraction_raw = _strip_json_fence(call_claude(extraction_prompt))
    (out_dir / "extraction.json").write_text(extraction_raw)
    llm_calls = 1

    manifest = {"event_tag": tag, "mode": "live", "assets": {}}
    spec_gate = {}
    for name in ASSETS:
        base_prompt = fill((PROMPTS_DIR / name).read_text(), transcript_text, event_json, extraction_raw)
        prompt = base_prompt
        text, ok, detail, attempt = None, False, "", 0
        # Gate BEFORE the asset ever reaches disk: generate, check against
        # check_asset_spec(), and on failure regenerate with the specific
        # failure appended to the prompt (e.g. "blog was 1277 words, cap is
        # 1200 -- cut 77+ words") rather than a bare retry. Bounded at
        # MAX_SPEC_ATTEMPTS (3) -- see PLAN.md punch-list: the word-count
        # gate used to annotate a spec violation after the file was already
        # written, never regenerating.
        for attempt in range(1, MAX_SPEC_ATTEMPTS + 1):
            raw = call_claude(prompt)
            llm_calls += 1
            text = add_utm_links(name, raw, event)
            ok, detail = check_asset_spec(name, text)
            if ok:
                break
            if attempt < MAX_SPEC_ATTEMPTS:
                print(f"[spec gate] {name} attempt {attempt}/{MAX_SPEC_ATTEMPTS} failed: {detail} "
                      f"-- regenerating", file=sys.stderr)
                prompt = base_prompt + (
                    f"\n\n---\nREGENERATION NOTE (attempt {attempt} rejected by the spec gate): "
                    f"{detail}. Rewrite the ENTIRE asset from scratch honoring the output contract "
                    "above -- do not just trim or pad the previous draft; produce a coherent full "
                    "replacement that satisfies both the contract and this note.\n"
                )
        if not ok:
            print(f"[spec gate] {name}: FAILED spec check after {MAX_SPEC_ATTEMPTS} attempts "
                  f"({detail}) -- shipping anyway, flagged loudly in manifest.json spec_check "
                  "rather than silently", file=sys.stderr)
        (out_dir / name).write_text(stamp(text, tag, mode_label))
        manifest["assets"][name] = {"path": str(out_dir / name), "words": word_count(text)}
        spec_gate[name] = {"attempts": attempt, "within_spec": ok, "detail": detail}
    manifest["_spec_gate"] = spec_gate
    # Live-signal receipt (api/run.py's P0 fix): reaching this line means the
    # extraction call plus every asset call above already succeeded --
    # call_claude() fails loud (RuntimeError) on any backend problem, so
    # there is no silent-fallback count to worry about here. llm_calls_made
    # includes every regeneration attempt, not just one-per-asset.
    manifest["llm_calls_made"] = llm_calls
    return manifest


EXTRACTION_DRY_RUN_PLACEHOLDER = (
    "[--live-dry-run: extraction pass is not executed -- zero network calls. "
    "In --live mode this would be the JSON object described in prompts/extraction.md's "
    "output contract (key_moments, quotes, data_points), produced by calling claude -p "
    "on the extraction prompt below.]"
)


def run_live_dry_run(transcript_text: str, event: dict, out_dir: Path) -> dict:
    """Builds every prompt the --live path would send to `claude -p` -- extraction
    plus all 4 asset prompts -- with real transcript/event data substituted in, and
    writes them to <out>/dry-run/*.prompt.md. call_claude() is never invoked (zero
    network calls), so this is code-inspectable-correct even with claude -p auth
    down. The asset prompts fill {{EXTRACTION}} with a labeled placeholder since the
    extraction call itself is skipped for the same zero-network reason."""
    tag = event_tag(event)
    event_json = json.dumps(event, indent=2)
    dry_dir = out_dir / "dry-run"
    dry_dir.mkdir(parents=True, exist_ok=True)

    stems_and_prompts = [("extraction", fill((PROMPTS_DIR / "extraction.md").read_text(), transcript_text, event_json))]
    for name in ASSETS:
        stem = name[:-3]  # strip ".md"
        prompt = fill((PROMPTS_DIR / name).read_text(), transcript_text, event_json, EXTRACTION_DRY_RUN_PLACEHOLDER)
        stems_and_prompts.append((stem, prompt))

    manifest = {"event_tag": tag, "mode": "live-dry-run", "planned_calls": len(stems_and_prompts), "prompts": {}}
    for stem, prompt in stems_and_prompts:
        path = dry_dir / f"{stem}.prompt.md"
        path.write_text(prompt)
        manifest["prompts"][stem] = {"path": str(path), "words": word_count(prompt), "chars": len(prompt)}
    return manifest


def run_grounding_check(args, event: dict, transcript_text: str) -> dict:
    """Runs after generation in both lanes (offline replay and --live) --
    verify_grounding checks every timestamp/quote/speaker-attribution claim
    in the 4 written assets against the transcript, writes
    grounding_report.json, and annotates any asset that failed with a
    visible flag (see verify_grounding.annotate_text). The default is to
    annotate and warn loudly rather than fail the run: this is a
    human-reviewed content pipeline (a blog draft, social copy) headed for
    a review queue, not an automated publish -- a flagged claim should stop
    a reviewer's eye, not silently block the whole batch from ever reaching
    them. --strict-grounding is there for a CI/publish gate that wants the
    harder failure instead."""
    asset_texts = {}
    for name in ASSETS:
        p = args.out / name
        if p.exists():
            asset_texts[name] = p.read_text()
    if not asset_texts:
        return {}
    report = verify_grounding.check_assets(transcript_text, event, asset_texts)
    (args.out / "grounding_report.json").write_text(json.dumps(report, indent=2))
    for name, text in asset_texts.items():
        annotated = verify_grounding.annotate_text(text, report["assets"].get(name))
        if annotated != text:
            (args.out / name).write_text(annotated)
    t = report["totals"]
    status = "PASS" if report["overall_pass"] else "FLAGGED"
    print(f"M3 grounding check ({status}): {t['verified']}/{t['claims_checked']} claims verified, "
          f"{t['failed']} failed -> {args.out / 'grounding_report.json'}")
    return report


def run(args) -> int:
    if not args.event.exists():
        raise RuntimeError(f"event.json not found: {args.event}")
    event = json.loads(args.event.read_text())

    args.out.mkdir(parents=True, exist_ok=True)

    transcript_text = None
    if args.transcript.exists():
        transcript_text = args.transcript.read_text()
    elif args.live or args.live_dry_run:
        raise RuntimeError(f"transcript not found: {args.transcript}")

    if args.live_dry_run:
        manifest = run_live_dry_run(transcript_text, event, args.out)
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"M3 repurpose (live-dry-run): {manifest['planned_calls']} planned claude -p call(s), "
              f"zero network calls made, event_tag={manifest['event_tag']}. "
              f"Prompts -> {args.out / 'dry-run'}:")
        for stem, info in manifest["prompts"].items():
            print(f"  {stem}.prompt.md -- {info['words']}w / {info['chars']} chars")
        return 0

    if args.live:
        manifest = run_live(transcript_text, event, args.out)
    else:
        check_fingerprint(args.transcript, args.event, args.allow_stale)
        manifest = run_offline(event, args.out)

    # Spec numerics, gated (SPEC.md M3: blog 800-1200 words, 5-10 social
    # posts, plus youtube.md/infographic.md's fixed 3-section contracts).
    # spec_gate was computed BEFORE each asset was written (run_live()
    # regenerates on failure, up to MAX_SPEC_ATTEMPTS, before ever touching
    # disk; run_offline() checks the fixed sample before writing it since
    # there's no LLM to regenerate against offline). This block folds that
    # gate result into the manifest -- numbers + a within_spec flag per
    # asset, so a violation that survived every attempt is visible here, not
    # hidden.
    spec_gate = manifest.pop("_spec_gate", {})
    blog_words = manifest["assets"].get("blog.md", {}).get("words", 0)
    social_text = (args.out / "social.md").read_text() if (args.out / "social.md").exists() else ""
    social_posts = sum(1 for line in social_text.splitlines() if line.startswith("### Post"))
    blog_gate = spec_gate.get("blog.md", {})
    social_gate = spec_gate.get("social.md", {})
    manifest["spec_check"] = {
        "blog_words": blog_words, "blog_spec": "800-1200",
        "blog_within_spec": blog_gate.get("within_spec", 800 <= blog_words <= 1200),
        "social_posts": social_posts, "social_spec": "5-10",
        "social_within_spec": social_gate.get("within_spec", 5 <= social_posts <= 10),
        "youtube_sections_ok": spec_gate.get("youtube.md", {}).get("within_spec"),
        "infographic_sections_ok": spec_gate.get("infographic.md", {}).get("within_spec"),
        "gate": spec_gate,  # per-asset attempts + specific failure detail, see check_asset_spec()
    }

    if transcript_text is not None:
        grounding_report = run_grounding_check(args, event, transcript_text)
        if grounding_report:
            manifest["grounding_check"] = {
                "overall_pass": grounding_report["overall_pass"],
                "totals": grounding_report["totals"],
                "report": str(args.out / "grounding_report.json"),
            }
    else:
        grounding_report = {}
        print("M3 grounding check: SKIPPED -- no transcript available to verify assets against "
              f"({args.transcript} not found)", file=sys.stderr)

    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    if args.strict_grounding and grounding_report and not grounding_report["overall_pass"]:
        raise RuntimeError(
            f"--strict-grounding: {grounding_report['totals']['failed']} claim(s) failed grounding "
            f"verification -- see {args.out / 'grounding_report.json'}"
        )

    sc = manifest["spec_check"]
    in_spec = (sc["blog_within_spec"] and sc["social_within_spec"]
               and sc["youtube_sections_ok"] is not False and sc["infographic_sections_ok"] is not False)
    flags = "" if in_spec else " [spec_check: see manifest.json]"
    regenerated = {n: g["attempts"] for n, g in spec_gate.items() if g["attempts"] > 1}
    regen_note = f" (regenerated: {regenerated})" if regenerated else ""
    print(f"M3 repurpose ({manifest['mode']}): 4 assets -> {args.out} "
          f"(blog {blog_words}w, {social_posts} social posts, event_tag={manifest['event_tag']}){flags}{regen_note}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="M3: webinar transcript -> multi-format content package.")
    parser.add_argument("--transcript", type=Path, default=DEFAULT_TRANSCRIPT, help="Path to transcript.md")
    parser.add_argument("--event", type=Path, default=DEFAULT_EVENT, help="Path to event.json")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--live", action="store_true", help="Regenerate assets via claude -p instead of copying samples")
    parser.add_argument("--live-dry-run", dest="live_dry_run", action="store_true",
                         help="Build and save the exact --live prompts (extraction + all 4 assets) filled "
                              "with real transcript/event data, without calling claude -p. Zero network "
                              "calls -- use to verify the live path when auth is unavailable.")
    parser.add_argument("--allow-stale", action="store_true", dest="allow_stale",
                         help="Bypass the fingerprint check and replay sample_output "
                              "against the current transcript/event anyway.")
    parser.add_argument("--strict-grounding", action="store_true", dest="strict_grounding",
                         help="Fail the run (exit 1) if any generated claim fails grounding "
                              "verification, instead of the default: annotate the flagged "
                              "asset(s) and keep going. Use in CI / a publish gate.")
    args = parser.parse_args()

    try:
        return run(args)
    except RuntimeError as e:
        print(f"refusing to generate: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"refusing to generate: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
