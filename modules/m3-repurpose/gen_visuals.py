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

Fails loud: a missing/empty API key, a non-2xx response, an OpenRouter
{"error": ...} payload, or a response with no image data all raise and
exit 1 -- no placeholder image or SVG is ever substituted.

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
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"
IMAGE_MODEL = "google/gemini-2.5-flash-image"


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


def build_thumbnail_prompt(youtube_md: str, event_tag: str) -> str:
    brief = _extract_section(youtube_md, "Thumbnail Brief")
    return (
        "YouTube thumbnail, 16:9 aspect ratio, high-contrast B2B webinar "
        "recap thumbnail. Follow this creative brief exactly (composition, "
        "text overlay copy, and colors):\n\n"
        f"{brief}\n\n"
        f"Event tag: {event_tag}. Style: sharp modern sans-serif typography, "
        "high contrast, professional corporate webinar/podcast thumbnail "
        "aesthetic, crisp studio lighting, 4K quality."
    )


def build_infographic_prompt(infographic_md: str, event_tag: str) -> str:
    data_points = _extract_section(infographic_md, "Data Points")
    layout = _extract_section(infographic_md, "Layout")
    headlines = _extract_section(infographic_md, "Headline Options")
    headline = headlines.splitlines()[0].strip("- *")
    return (
        "Marketing infographic hero graphic, portrait or square, flat "
        "vector illustration style, clean modern B2B data-visualization "
        f"design. Headline: \"{headline}\".\n\n"
        f"Data points to depict exactly (do not invent numbers):\n{data_points}\n\n"
        f"Layout and color system to follow:\n{layout}\n\n"
        f"Event tag: {event_tag}. Style: crisp geometric icons, generous "
        "whitespace, grid-aligned panels, professional SaaS marketing "
        "infographic aesthetic, sharp vector shapes, legible bold "
        "sans-serif numerals, high resolution."
    )


def call_openrouter_image(key: str, prompt: str) -> dict:
    payload = json.dumps({
        "model": IMAGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
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
        out_path = out_dir / spec["output_filename"]
        out_path.write_bytes(result["bytes"])
        meta["generations"][asset] = {
            "status": "ok",
            "filename": spec["output_filename"],
            "generation_id": result["generation_id"],
            "model_returned": result["model_returned"],
            "usage": result["usage"],
            "bytes": len(result["bytes"]),
            "seconds": round(dt, 2),
        }
        print(f"  ok -> {out_path} ({len(result['bytes'])} bytes, {dt:.1f}s, id={result['generation_id']})")

    (out_dir / "generation_meta.json").write_text(json.dumps(meta, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
