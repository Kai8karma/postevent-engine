#!/usr/bin/env python3
"""M3 -- Visual asset generation (thumbnail + infographic hero image).

Reads the live M3 outputs (youtube.md's Thumbnail Brief, infographic.md's
Data Points + Layout), builds an image-generation prompt for each, and
produces the two visual assets M3's spec calls for: a YouTube thumbnail
(16:9) and an infographic hero image (portrait/square).

THIS SCRIPT IS GENUINELY EXECUTABLE END TO END. It does not just print
prompts for a human/n8n node to act on -- it makes a real HTTP call to
OpenRouter's image-generation-capable chat completions endpoint
(https://openrouter.ai/api/v1/chat/completions, model
google/gemini-2.5-flash-image, modalities: ["image","text"]) using the same
OPENROUTER_API_KEY already wired for M3's --live text pipeline
(env var, else ~/.config/postevent/llm.env -- see repurpose.py's
_openrouter_key()). That is the "direct HTTP image API with an env-var key"
branch described in the module's build brief, chosen because it was
verified working in this environment; the Higgsfield MCP tool named in that
brief is not reachable from a plain script (MCP tools are only callable
from an interactive Claude session) and, separately, returned "Requires
basic plan or higher" (0 credits) when tried live -- see
out/live-proof-visuals/visuals_meta.json for that record.

Every run also writes image_prompts.json (the exact prompt payloads) next
to the generated PNGs, regardless of whether the API call succeeds, so the
prompts are inspectable even if the network/key is unavailable.

TEXT IS NEVER LEFT TO THE IMAGE MODEL. The prompts built below explicitly
tell the model to render pure imagery/composition -- no words, numerals, or
typography -- because gemini-2.5-flash-image reliably garbles baked-in text
(documented, not theoretical: out/live-proof-visuals/visuals_meta.json's
known_limitations_observed_on_visual_inspection recorded an illegible
thumbnail headline and three real typos -- "piseline", "AFORE", "REPUROSED"
-- from an earlier run that did ask it to draw text). Instead the exact
headline/stat copy is pulled straight from youtube.md's Thumbnail Brief /
infographic.md's Headline Options + Data Points (the same source the text
assets themselves cite) and composited on top with Pillow after the API
call returns -- deterministic, same text every run, zero typo risk
regardless of what the model does with the image. The thumbnail gets its
headline (top-third bar) and stat line (bottom-right badge) composited;
the infographic gets its headline (top banner) and full Data Points list
(bottom legend strip) -- see composite_thumbnail()/composite_infographic()
and README.md's "Visual assets" section for why per-panel geometric
matching to an AI-drawn chart was judged out of scope for a cheap overlay.
Pillow is optional tooling, soft-imported the same way tools/
render_visuals.py soft-imports playwright: no Pillow -> the raw AI image
ships uncropped/untouched with generation_meta.json's text_overlay.applied
= false and a reason, never a silent skip.

Fails loud on the generation call itself: a missing/empty API key, a
non-2xx response, an OpenRouter {"error": ...} payload, or a response with
no image data all raise and exit 1 -- no placeholder image or SVG is ever
substituted. The text-overlay step is a secondary enhancement on top of a
real image, so it degrades (loud stderr warning, recorded reason, ship the
raw image) rather than failing the whole run.

Usage:
    export OPENROUTER_API_KEY=...   # or set it in ~/.config/postevent/llm.env
    python3 gen_visuals.py --out ../../out/m3-visuals
    python3 gen_visuals.py --youtube out/live-proof/m3/youtube.md \
        --infographic out/live-proof/m3/infographic.md --out out/m3-visuals
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

# Optional tooling: deterministic text overlay needs Pillow. Soft-imported
# the same way tools/render_visuals.py soft-imports playwright -- missing
# Pillow degrades to "ship the raw AI image, no overlay" (see module
# docstring), it never blocks the API call that actually costs money.
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"
IMAGE_MODEL = "google/gemini-2.5-flash-image"

# Overlay palette -- matches tools/render_visuals.py's NAVY/AMBER template
# colors so AI-generated and template-rendered visuals read as one family.
NAVY_SCRIM = (11, 37, 69, 220)
AMBER = (232, 163, 61, 235)
WHITE = (255, 255, 255, 255)


def _openrouter_key() -> str:
    """OPENROUTER_API_KEY env var, else a KEY=VALUE line in
    ~/.config/postevent/llm.env. Never logged/printed. Mirrors
    repurpose.py's _openrouter_key() so both scripts read one config."""
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


def _extract_section(md_text: str, heading: str) -> str:
    """Return the body text under a '## <heading>' markdown section (up to
    the next '## ' heading or end of file)."""
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    lines = md_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"section '## {heading}' not found")
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


NO_TEXT_INSTRUCTION_THUMBNAIL = (
    "\n\nIMPORTANT: Do not render any text, words, letters, or numerals "
    "anywhere in the image -- pure photography/composition only. The "
    "headline and stat line above describe what a separate, deterministic "
    "text overlay will add afterward (see gen_visuals.py) -- leave the top "
    "third and bottom-right corner visually clean/uncluttered so that "
    "overlay reads legibly once composited on."
)

NO_TEXT_INSTRUCTION_INFOGRAPHIC = (
    "\n\nIMPORTANT: Do not render any text, words, numerals, or typography "
    "anywhere in the image -- pure flat-vector icons, panels, charts, and "
    "color only. Leave a clean, uncluttered band across the top (roughly "
    "the top 12%) and across the bottom (roughly the bottom 30%) -- a "
    "headline and a data-points legend will be composited into those bands "
    "afterward, deterministically (see gen_visuals.py)."
)


def build_thumbnail_prompt(youtube_md: str, event_tag: str) -> str:
    brief = _extract_section(youtube_md, "Thumbnail Brief")
    return (
        "YouTube thumbnail, 16:9 aspect ratio, high-contrast B2B webinar "
        "recap thumbnail. Follow this creative brief for composition and "
        "colors (its text-overlay copy is handled separately, not by you "
        "-- see the note below):\n\n"
        f"{brief}\n\n"
        f"Event tag: {event_tag}. Style: sharp modern sans-serif typography, "
        "high contrast, professional corporate webinar/podcast thumbnail "
        "aesthetic, crisp studio lighting, 4K quality."
        f"{NO_TEXT_INSTRUCTION_THUMBNAIL}"
    )


def build_infographic_prompt(infographic_md: str, event_tag: str) -> str:
    data_points = _extract_section(infographic_md, "Data Points")
    layout = _extract_section(infographic_md, "Layout")
    headlines = _extract_section(infographic_md, "Headline Options")
    headline = headlines.splitlines()[0].strip("- *")
    return (
        "Marketing infographic hero graphic, portrait or square, flat "
        "vector illustration style, clean modern B2B data-visualization "
        f"design themed around: \"{headline}\".\n\n"
        f"Data points this graphic represents (use only to guide icon "
        "choice, panel count, and relative proportions -- e.g. bar heights "
        f"-- not as text to render):\n{data_points}\n\n"
        f"Layout and color system to follow:\n{layout}\n\n"
        f"Event tag: {event_tag}. Style: crisp geometric icons, generous "
        "whitespace, grid-aligned panels, professional SaaS marketing "
        "infographic aesthetic, sharp vector shapes, high resolution."
        f"{NO_TEXT_INSTRUCTION_INFOGRAPHIC}"
    )


# --- Deterministic text overlay (see module docstring) -------------------

# PIL's bundled default font (used via ImageFont.load_default(size=...), no
# extra font file/dependency needed) doesn't cover every Unicode glyph --
# verified by rendering each candidate and diffing against a known-missing
# glyph's bitmap (a real test, not a guess): em/en dashes, bullet, left/
# right arrows, the multiplication sign, and the approx sign all render as
# tofu boxes; curly quotes/ellipsis/degree/plus-minus render fine. The
# multiplication-sign and approx-sign entries were found against a real
# --live generation (out/live-proof/m3/youtube.md and infographic.md wrote
# "3.2× Reply Boost" / "≈2x" -- ×/≈, not "x"/"~") --
# sanitized to plain ASCII before any draw call rather than shipping
# visibly broken glyphs.
_UNICODE_ASCII_MAP = {
    "—": "-", "–": "-",           # em dash, en dash (tofu)
    "→": "->", "←": "<-",          # arrows (tofu)
    "×": "x", "≈": "~",             # multiplication sign, approx sign (tofu) -- real --live output uses both
    "•": "-",                            # bullet (tofu)
}


def _ascii_safe(text: str) -> str:
    """Explicit map first (documented, evidence-based -- see
    _UNICODE_ASCII_MAP), then a general safety net for whatever a live LLM
    generation invents next: a real --live run also produced a narrow
    no-break space (U+202F, "4 hours") and a non-breaking hyphen (U+2011,
    "follow-up") -- both tofu, neither worth a one-off map entry. Unicode
    category covers those generically (Zs -> space, Pd -> hyphen);
    anything else gets NFKD-decomposed to strip accents (e.g. "e" for an
    accented e) and, failing that, dropped rather than shipped as a tofu
    box -- a missing character is less jarring than a visible glyph-error
    square on a marketing asset."""
    for u, a in _UNICODE_ASCII_MAP.items():
        text = text.replace(u, a)
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category == "Zs":
            out.append(" ")
        elif category == "Pd":
            out.append("-")
        else:
            out.append("".join(c for c in unicodedata.normalize("NFKD", ch) if ord(c) < 128))
    return "".join(out)


def _extract_quoted(text: str, n: int) -> list:
    """Straight "..." and curly "..." quoted spans, in the order they
    appear -- a real --live generation quoted its Thumbnail Brief headline
    with curly quotes (out/live-proof/m3/youtube.md), which a straight-
    quote-only regex silently missed entirely (headline came back None)."""
    spans = [(m.start(), m.group(1)) for m in re.finditer(r'"([^"]+)"', text)]
    spans += [(m.start(), m.group(1)) for m in re.finditer(r'“([^”]+)”', text)]
    spans.sort(key=lambda t: t[0])
    return [s for _, s in spans][:n]


def parse_thumbnail_overlay(youtube_md: str) -> tuple:
    """(headline, stat_line) pulled verbatim from the Thumbnail Brief's
    '**Text overlay**' item -- prompts/youtube.md's contract requires the
    exact headline (and any stat line) quoted there. Bounded by the *next*
    '**Label**:' item, found generically rather than assuming one bullet
    style: the checked-in sample_output fixture writes each item as
    '- **Label:** ...' on one line, but a real --live generation wrote bare
    '**Label**:' headers (no leading '- ') with the item's own '- ' sub-
    bullets nested underneath and a blank line before the next header --
    matching only the fixture's separator missed the boundary entirely on
    that real output. Returns (None, "") if no quoted text is found in the
    Text overlay item at all (e.g. a live model that didn't follow the
    contract) -- callers degrade to no-overlay rather than composite a
    guess."""
    brief = _extract_section(youtube_md, "Thumbnail Brief")
    labels = list(re.finditer(r"\*\*([A-Za-z][A-Za-z /]*)\*\*:?", brief))
    overlay_text = brief
    for i, label in enumerate(labels):
        if label.group(1).strip().lower() == "text overlay":
            end = labels[i + 1].start() if i + 1 < len(labels) else len(brief)
            overlay_text = brief[label.end():end]
            break
    quoted = _extract_quoted(overlay_text, 2)
    if not quoted:
        return None, ""
    return quoted[0], (quoted[1] if len(quoted) > 1 else "")


def parse_infographic_overlay(infographic_md: str) -> tuple:
    """(headline, [legend lines]) -- headline is Headline Options' first
    choice; legend lines are the Data Points list, both list-marker- and
    quote-agnostic (numbering/quotes stripped). prompts/infographic.md's
    Data Points contract is `**<number>** -- <what> *<citation>*` with NO
    numbered-list prefix mandated -- a real --live generation confirmed
    this (out/live-proof/m3/infographic.md has zero "1. "-style prefixes;
    only sample_output/infographic.md's fixture happens to add them).
    Matching on "starts with **" after stripping an *optional* leading
    list marker (numbered or "- ") handles both."""
    list_marker = re.compile(r"^(?:\d+\.|-)\s*")
    headlines = _extract_section(infographic_md, "Headline Options")
    headline = list_marker.sub("", headlines.splitlines()[0]).strip().strip('"“”')
    data_points = _extract_section(infographic_md, "Data Points")
    lines = []
    for raw in data_points.splitlines():
        raw = list_marker.sub("", raw.strip())
        if not raw.startswith("**"):
            continue
        body = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)         # unbold **stat**
        body = re.sub(r"\*[^*]*\*\s*$", "", body).strip()  # drop trailing *citation*
        if len(body) > 78:
            body = body[:75].rstrip() + "..."
        lines.append(body)
    return headline, lines


def _wrap_text(draw, text: str, font, max_width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_text(draw, text: str, max_width: int, max_height: int, max_lines: int,
              start_size: int, min_size: int = 20) -> tuple:
    """Largest font size (stepping down from start_size by 4) whose
    word-wrapped text fits within max_lines and max_height. Bottoms out at
    min_size and truncates to max_lines rather than raising."""
    size = start_size
    font, lines = ImageFont.load_default(size=min_size), [text]
    while size >= min_size:
        font = ImageFont.load_default(size=size)
        lines = _wrap_text(draw, text, font, max_width)
        line_h = draw.textbbox((0, 0), "Ag", font=font)[3] * 1.25
        if len(lines) <= max_lines and line_h * len(lines) <= max_height:
            return font, lines
        size -= 4
    return font, lines[:max_lines]


def composite_thumbnail(png_bytes: bytes, headline: str, stat_line: str) -> bytes:
    """Center-crop the model's native square output to true 16:9, then
    overlay the headline (top-third navy bar -- matches the Thumbnail
    Brief's own placement spec) and stat line (bottom-right amber badge)
    with Pillow. Deterministic: same text every run, regardless of what
    the model drew."""
    headline, stat_line = _ascii_safe(headline), _ascii_safe(stat_line)
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    target_h = round(w * 9 / 16)
    if target_h <= h:
        top = (h - target_h) // 2
        img = img.crop((0, top, w, top + target_h))
    else:
        target_w = round(h * 16 / 9)
        left = (w - target_w) // 2
        img = img.crop((left, 0, left + target_w, h))
    img = img.convert("RGBA")
    W, H = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    bar_h = round(H * 0.30)
    draw.rectangle([0, 0, W, bar_h], fill=NAVY_SCRIM)
    font, lines = _fit_text(draw, headline, round(W * 0.9), round(bar_h * 0.85), 2, start_size=64)
    line_h = draw.textbbox((0, 0), "Ag", font=font)[3] * 1.3
    y = (bar_h - line_h * len(lines)) / 2
    for line in lines:
        draw.text((W * 0.05, y), line, font=font, fill=WHITE)
        y += line_h

    if stat_line:
        sfont = ImageFont.load_default(size=round(H * 0.04))
        bbox = draw.textbbox((0, 0), stat_line, font=sfont)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 16
        x1, y1 = W - tw - pad * 2 - 24, H - th - pad * 2 - 24
        draw.rectangle([x1, y1, W - 24, H - 24], fill=AMBER)
        draw.text((x1 + pad, y1 + pad - bbox[1]), stat_line, font=sfont, fill=(11, 37, 69, 255))

    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def composite_infographic(png_bytes: bytes, headline: str, data_lines: list) -> bytes:
    """Overlay a headline banner (top) and a Data Points legend strip
    (bottom) on the model's native square output with Pillow -- same
    determinism rationale as composite_thumbnail(). Per-panel geometric
    matching to whatever chart shapes the model drew was judged out of
    scope for a cheap overlay (README.md's "Visual assets" section); the
    legend guarantees every number and label is exactly what
    infographic.md says, at the cost of not aligning to individual
    AI-drawn panels."""
    headline = _ascii_safe(headline)
    data_lines = [_ascii_safe(line) for line in data_lines]
    img = Image.open(BytesIO(png_bytes)).convert("RGBA")
    W, H = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    bar_h = round(H * 0.12)
    draw.rectangle([0, 0, W, bar_h], fill=NAVY_SCRIM)
    font, lines = _fit_text(draw, headline, round(W * 0.92), round(bar_h * 0.85), 2, start_size=44)
    line_h = draw.textbbox((0, 0), "Ag", font=font)[3] * 1.3
    y = (bar_h - line_h * len(lines)) / 2
    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(((W - tw) / 2, y), line, font=font, fill=WHITE)
        y += line_h

    if data_lines:
        legend_h = round(H * 0.30)
        draw.rectangle([0, H - legend_h, W, H], fill=(255, 255, 255, 230))
        item_font = ImageFont.load_default(size=max(16, round(legend_h / (len(data_lines) + 1) * 0.6)))
        item_h = draw.textbbox((0, 0), "Ag", font=item_font)[3] * 1.35
        y = H - legend_h + 14
        for line in data_lines:
            draw.text((24, y), f"- {line}", font=item_font, fill=(11, 37, 69, 255))
            y += item_h

    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def call_openrouter_image(key: str, prompt: str) -> dict:
    payload = json.dumps({
        "model": IMAGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        # Explicit cap, not a real limit -- a real image generation uses
        # ~1.3-1.6K tokens (verified: out/live-proof-visuals/visuals_meta.json
        # and a fresh probe both landed at ~1300-1310). Without this,
        # OpenRouter's free-tier affordability preflight compares the
        # remaining balance against the *model's* much larger implicit
        # max_tokens ceiling and 402s even though the actual call would
        # have been cheap -- verified live: the same request 402'd
        # ("requested up to 29075 tokens, can only afford 4240") until this
        # cap was added, then succeeded for real.
        "max_tokens": 3000,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://kai8karma.github.io/agentkai/",
            "X-Title": "Post-Event Engine -- M3 visuals",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"openrouter image call failed: HTTP {e.code} {e.read().decode(errors='replace')[:500]}") from e
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"openrouter image call failed: {data['error']}")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"openrouter: unexpected response shape: {e}") from e
    images = msg.get("images") or []
    if not images:
        raise RuntimeError("openrouter: response had no images (model returned text only)")
    img_url = images[0]["image_url"]["url"]
    if not img_url.startswith("data:image/png;base64,"):
        raise RuntimeError(f"openrouter: unexpected image_url format: {img_url[:60]!r}")
    raw = base64.b64decode(img_url.split(",", 1)[1])
    return {
        "bytes": raw,
        "generation_id": data.get("id"),
        "model_returned": data.get("model"),
        "usage": data.get("usage"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--youtube", default=str(REPO_ROOT / "out" / "live-proof" / "m3" / "youtube.md"))
    ap.add_argument("--infographic", default=str(REPO_ROOT / "out" / "live-proof" / "m3" / "infographic.md"))
    ap.add_argument("--out", required=True, help="output directory for PNGs + image_prompts.json")
    ap.add_argument("--event", default="acmerevenue-2026-07-20")
    ap.add_argument("--dry-run", action="store_true", help="write image_prompts.json only, never call the API")
    args = ap.parse_args()

    youtube_path = Path(args.youtube)
    infographic_path = Path(args.infographic)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not youtube_path.exists():
        print(f"error: --youtube not found: {youtube_path}", file=sys.stderr)
        return 1
    if not infographic_path.exists():
        print(f"error: --infographic not found: {infographic_path}", file=sys.stderr)
        return 1

    youtube_md = youtube_path.read_text()
    infographic_md = infographic_path.read_text()

    prompts = {
        "youtube_thumbnail": {
            "prompt": build_thumbnail_prompt(youtube_md, args.event),
            "aspect": "16:9",
            "output_filename": "youtube-thumbnail.png",
        },
        "infographic_hero": {
            "prompt": build_infographic_prompt(infographic_md, args.event),
            "aspect": "portrait/square",
            "output_filename": "infographic-hero.png",
        },
    }
    prompts_path = out_dir / "image_prompts.json"
    prompts_path.write_text(json.dumps({
        "event": args.event,
        "model": IMAGE_MODEL,
        "endpoint": OPENROUTER_URL,
        "source_files": [str(youtube_path), str(infographic_path)],
        "reviewer_command": (
            f"OPENROUTER_API_KEY=... python3 {Path(__file__).name} "
            f"--youtube {youtube_path} --infographic {infographic_path} --out {out_dir}"
        ),
        "prompts": prompts,
    }, indent=2))
    print(f"wrote {prompts_path}")

    if args.dry_run:
        print("dry-run: skipping the API call")
        return 0

    key = _openrouter_key()
    if not key:
        print(
            "error: OPENROUTER_API_KEY not set (env var or ~/.config/postevent/llm.env) "
            "-- cannot call the image API. Prompts were still written above.",
            file=sys.stderr,
        )
        return 1

    # Overlay text is parsed once, up front, from the same youtube_md/
    # infographic_md the prompts themselves were built from -- see module
    # docstring "TEXT IS NEVER LEFT TO THE IMAGE MODEL".
    thumb_headline, thumb_stat = parse_thumbnail_overlay(youtube_md)
    info_headline, info_lines = parse_infographic_overlay(infographic_md)

    meta = {"event": args.event, "model": IMAGE_MODEL, "generations": {}}
    failed = False
    for asset, spec in prompts.items():
        print(f"generating {asset} ...")
        t0 = time.time()
        try:
            result = call_openrouter_image(key, spec["prompt"])
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            meta["generations"][asset] = {"status": "failed", "error": str(e)}
            failed = True
            continue
        dt = time.time() - t0
        raw_bytes = result["bytes"]

        # Composite deterministic text on top (secondary enhancement --
        # degrades on any problem rather than failing a real, already-paid-
        # for image generation).
        final_bytes = raw_bytes
        text_overlay = {"applied": False, "reason_skipped": None}
        if not PIL_AVAILABLE:
            text_overlay["reason_skipped"] = "Pillow not installed"
        else:
            try:
                if asset == "youtube_thumbnail":
                    if not thumb_headline:
                        text_overlay["reason_skipped"] = "could not parse a quoted headline from Thumbnail Brief's Text overlay bullet"
                    else:
                        final_bytes = composite_thumbnail(raw_bytes, thumb_headline, thumb_stat)
                        text_overlay = {"applied": True, "headline": thumb_headline, "stat_line": thumb_stat}
                elif asset == "infographic_hero":
                    final_bytes = composite_infographic(raw_bytes, info_headline, info_lines)
                    text_overlay = {"applied": True, "headline": info_headline, "data_points": len(info_lines)}
            except Exception as e:
                print(f"  WARNING: text overlay failed for {asset}: {e} -- shipping the raw AI image without overlay", file=sys.stderr)
                final_bytes = raw_bytes
                text_overlay = {"applied": False, "reason_skipped": str(e)}

        out_path = out_dir / spec["output_filename"]
        out_path.write_bytes(final_bytes)
        meta["generations"][asset] = {
            "status": "ok",
            "filename": spec["output_filename"],
            "generation_id": result["generation_id"],
            "model_returned": result["model_returned"],
            "usage": result["usage"],
            "bytes": len(final_bytes),
            "raw_bytes": len(raw_bytes),
            "seconds": round(dt, 2),
            "text_overlay": text_overlay,
        }
        overlay_note = "applied" if text_overlay["applied"] else f"skipped: {text_overlay.get('reason_skipped')}"
        print(f"  ok -> {out_path} ({len(final_bytes)} bytes, {dt:.1f}s, id={result['generation_id']}, text_overlay={overlay_note})")

    (out_dir / "generation_meta.json").write_text(json.dumps(meta, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
