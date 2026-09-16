#!/usr/bin/env python3
"""M3 -- visual generation (real image model, or nothing).

Discovers an image-capable model on OpenRouter (GET /api/v1/models, filtered on
architecture.output_modalities containing "image"), then asks it for three
visuals built from this run's extraction + thumbnail brief:

    visuals/youtube-thumbnail.png   16:9
    visuals/social-square.png       1:1
    visuals/social-portrait.png     4:5

Every attempt is receipted to receipts/m3_images.json (model, full prompt,
latency, bytes, HTTP status). If the key has no credits (HTTP 402) or the model
returns no image, the file is SKIPPED and the failure is written into the
receipt. No placeholder, no CSS screenshot, no stock art is ever emitted as a
"generated visual" -- an absent file is the honest output.

Text is deliberately kept out of the generated image (image models garble
lettering); the exact headline/stat copy lives in youtube.md's Thumbnail Brief
for the designer or the overlay step downstream.

Usage:
    python3 gen_visuals.py --out out/m3 --extraction out/m3/extraction.json \
        --youtube out/m3/youtube.md --event data/incoming/event.json
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
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:                                     # crop to exact ratio is optional
    PIL_AVAILABLE = False

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CONFIG_PATH = Path.home() / ".config" / "postevent" / "llm.env"
PREFERRED_IMAGE_MODELS = ["google/gemini-2.5-flash-image", "google/gemini-2.5-flash-image-preview"]
HEADERS_EXTRA = {"HTTP-Referer": "https://kai8karma.github.io/agentkai/",
                 "X-Title": "Post-Event Engine -- M3 visuals"}

SPECS = [
    {"name": "youtube-thumbnail", "aspect": "16:9", "ratio": 16 / 9, "role": "thumbnail"},
    {"name": "social-square", "aspect": "1:1", "ratio": 1.0, "role": "social"},
    {"name": "social-portrait", "aspect": "4:5", "ratio": 4 / 5, "role": "social"},
]


def openrouter_key() -> str:
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


def discover_image_models(key: str) -> tuple:
    """Models whose architecture.output_modalities include "image". Returns
    (ordered_candidates, http_status, error)."""
    req = urllib.request.Request(OPENROUTER_MODELS_URL,
                                 headers={"Authorization": f"Bearer {key}", **HEADERS_EXTRA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return [], e.code, e.read().decode(errors="replace")[:200]
    except (urllib.error.URLError, TimeoutError) as e:
        return [], None, str(e)
    image_models = [m for m in data.get("data", [])
                    if "image" in ((m.get("architecture") or {}).get("output_modalities") or [])]

    def completion_price(m):
        try:
            price = float((m.get("pricing") or {}).get("completion", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        return price if price > 0 else 1.0        # -1 (auto-router) sorts last

    ids = [m["id"] for m in image_models]
    override = os.environ.get("OPENROUTER_IMAGE_MODEL", "").strip()
    preferred = ([override] if override else []) + PREFERRED_IMAGE_MODELS
    # Preference first, then cheapest-completion first: on a near-empty balance
    # the affordability preflight is what decides whether any image ships.
    rest = sorted((m for m in image_models if m["id"] not in preferred), key=completion_price)
    ordered = [m for m in preferred if m in ids] + [m["id"] for m in rest]
    return ordered, status, None


def section(md_text: str, heading: str) -> str:
    m = re.search(rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", md_text, re.M | re.S)
    return m.group(1).strip() if m else ""


def build_prompts(extraction: dict, youtube_md: str, event: dict) -> list:
    """One prompt per spec, grounded in this run's own extraction."""
    brief = section(youtube_md, "Thumbnail Brief")
    topics = [i.get("insight", "") for i in extraction.get("insights", [])[:3]]
    stats = [f"{d.get('value')} ({d.get('what')})" for d in extraction.get("data_points", [])[:3]]
    speakers = ", ".join(f"{s['name']}, {s['title']} at {s['company']}" for s in event.get("speakers", []))
    no_text = ("Render pure imagery only: absolutely no words, letters, numerals, captions, logos or "
               "typography anywhere in the frame (text is added separately downstream).")
    base = (f"Editorial illustration for a B2B webinar titled \"{event.get('event_name')}\", hosted by "
            f"{event.get('host_company')}. Speakers: {speakers}. The session covers: "
            + "; ".join(t for t in topics if t) + ". ")
    prompts = []
    for spec in SPECS:
        if spec["role"] == "thumbnail":
            body = (base + "Design a YouTube thumbnail background in a " + spec["aspect"] +
                    " aspect ratio. Art direction from the session's own thumbnail brief: " +
                    " ".join(brief.split())[:900] +
                    " Leave the upper-left third visually calm as clear space for a headline. ")
        else:
            body = (base + f"Design a social media card background in a {spec['aspect']} aspect ratio: "
                    "clean corporate abstract, deep navy and warm amber palette, subtle geometric "
                    "shapes suggesting people data and workflow automation, generous negative space "
                    "in the centre for copy to be placed later. Reference points from the session: "
                    + "; ".join(stats) + ". ")
        prompts.append({**spec, "prompt": body + no_text})
    return prompts


AFFORDABLE_RE = re.compile(r"can only afford (\d+)")
MIN_IMAGE_TOKENS = 900          # below this an image response cannot complete


def request_image(key: str, model: str, prompt: str, timeout: int, max_tokens: int = 3000) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        # Explicit cap: without it OpenRouter's affordability preflight compares
        # the balance against the model's implicit ceiling and 402s a call that
        # would actually have been cheap.
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(OPENROUTER_CHAT_URL, data=payload, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json", **HEADERS_EXTRA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, data = resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_status": e.code, "error": e.read().decode(errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "http_status": None, "error": str(e)[:200]}
    if isinstance(data, dict) and data.get("error"):
        err = data["error"] or {}
        return {"ok": False, "http_status": err.get("code") if isinstance(err.get("code"), int) else status,
                "error": str(err.get("message"))[:300]}
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        return {"ok": False, "http_status": status, "error": f"unexpected response shape: {e}"}
    images = msg.get("images") or []
    if not images:
        return {"ok": False, "http_status": status, "error": "model returned text only, no image"}
    url = images[0].get("image_url", {}).get("url", "")
    if not url.startswith("data:image/"):
        return {"ok": False, "http_status": status, "error": f"unexpected image payload: {url[:60]!r}"}
    return {"ok": True, "http_status": status, "bytes": base64.b64decode(url.split(",", 1)[1]),
            "generation_id": data.get("id"), "model_returned": data.get("model")}


def try_candidates(key: str, candidates: list, prompt: str, timeout: int) -> tuple:
    """Walk the discovered image models until one actually returns an image.
    A 402 costs nothing, so falling through is free; when the 402 names an
    affordable token ceiling, retry that model once inside it rather than
    giving up on a call that would have fitted."""
    attempts, last = [], {"ok": False, "http_status": None, "error": "no candidate model attempted"}
    for model in candidates[:5]:
        result = request_image(key, model, prompt, timeout)
        attempts.append({"model": model, "http_status": result.get("http_status"),
                         "ok": result["ok"], "error": str(result.get("error"))[:160] if not result["ok"] else None})
        if result["ok"]:
            return result, model, attempts
        afford = AFFORDABLE_RE.search(str(result.get("error", "")))
        if result.get("http_status") == 402 and afford and int(afford.group(1)) >= MIN_IMAGE_TOKENS:
            capped = int(afford.group(1))
            result = request_image(key, model, prompt, timeout, max_tokens=capped)
            attempts.append({"model": model, "max_tokens": capped, "http_status": result.get("http_status"),
                             "ok": result["ok"], "error": str(result.get("error"))[:160] if not result["ok"] else None})
            if result["ok"]:
                return result, model, attempts
        last = result
    return last, candidates[0] if candidates else None, attempts


def crop_to_ratio(raw: bytes, ratio: float) -> tuple:
    """Centre-crop the real generated image to the exact aspect the channel needs.
    Returns (bytes, note). Never fabricates pixels -- it only removes them."""
    if not PIL_AVAILABLE:
        return raw, "Pillow unavailable -- shipped at the model's own aspect ratio"
    try:
        img = Image.open(BytesIO(raw))
        w, h = img.size
        if abs((w / h) - ratio) < 0.02:
            return raw, None
        if (w / h) > ratio:
            new_w = int(round(h * ratio))
            box = ((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h)
        else:
            new_h = int(round(w / ratio))
            box = (0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h)
        buf = BytesIO()
        img.crop(box).save(buf, format="PNG")
        return buf.getvalue(), f"centre-cropped {w}x{h} -> {box[2] - box[0]}x{box[3] - box[1]}"
    except Exception as e:                                # noqa: BLE001 -- never lose a real image
        return raw, f"crop skipped ({type(e).__name__}: {e})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate M3 visuals with a real image model.")
    ap.add_argument("--out", type=Path, required=True, help="M3 run directory (visuals/ and receipts/)")
    ap.add_argument("--extraction", type=Path, required=True)
    ap.add_argument("--youtube", type=Path, required=True)
    ap.add_argument("--event", type=Path, required=True)
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("IMAGE_DEADLINE_S", "180")))
    ap.add_argument("--dry-run", action="store_true", help="Write the prompts into the receipt, call nothing")
    args = ap.parse_args()

    extraction = json.loads(args.extraction.read_text()) if args.extraction.exists() else {}
    youtube_md = args.youtube.read_text() if args.youtube.exists() else ""
    event = json.loads(args.event.read_text())
    prompts = build_prompts(extraction, youtube_md, event)

    receipt = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "provider": "openrouter", "model": None, "discovery": {}, "images": [], "notes": []}
    receipts_dir = args.out / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / "m3_images.json"

    def finish(code: int) -> int:
        receipt_path.write_text(json.dumps(receipt, indent=2))
        ok = sum(1 for i in receipt["images"] if i.get("ok"))
        print(f"[images] {ok}/{len(prompts)} generated -> {args.out / 'visuals'} (receipt: {receipt_path})")
        for note in receipt["notes"]:
            print(f"[images] {note}", file=sys.stderr)
        return code

    if args.dry_run:
        receipt["images"] = [{"name": p["name"], "aspect": p["aspect"], "prompt": p["prompt"],
                              "ok": False, "error": "--dry-run: no call made"} for p in prompts]
        receipt["notes"].append("--dry-run: prompts only, zero network calls")
        return finish(0)

    key = openrouter_key()
    if not key:
        receipt["notes"].append("no OPENROUTER_API_KEY -- image generation skipped, no placeholder written")
        return finish(1)

    candidates, status, err = discover_image_models(key)
    receipt["discovery"] = {"endpoint": OPENROUTER_MODELS_URL, "http_status": status,
                            "image_capable_models": len(candidates), "top_candidates": candidates[:5],
                            "error": err}
    if not candidates:
        receipt["notes"].append(f"no image-capable model discoverable (HTTP {status}, {err}) -- "
                                "image generation skipped, no placeholder written")
        return finish(1)
    receipt["model"] = candidates[0]
    print(f"[images] {len(candidates)} image-capable model(s) discovered; trying {candidates[:5]}")

    visuals_dir = args.out / "visuals"
    visuals_dir.mkdir(parents=True, exist_ok=True)
    for spec in prompts:
        started = time.monotonic()
        result, model, attempts = try_candidates(key, candidates, spec["prompt"], args.timeout)
        latency = int((time.monotonic() - started) * 1000)
        row = {"name": spec["name"], "aspect": spec["aspect"], "model": model, "prompt": spec["prompt"],
               "latency_ms": latency, "http_status": result.get("http_status"), "ok": result["ok"],
               "attempts": attempts}
        if result["ok"]:
            data, note = crop_to_ratio(result["bytes"], spec["ratio"])
            path = visuals_dir / f"{spec['name']}.png"
            path.write_bytes(data)
            row.update({"file": f"visuals/{path.name}", "bytes": len(data),
                        "generation_id": result.get("generation_id"),
                        "model_returned": result.get("model_returned"), "post_process": note})
            print(f"[images] {spec['name']}.png {len(data) / 1000:.0f} kB in {latency} ms")
        else:
            row["error"] = result.get("error")
            row["bytes"] = 0
            note = (f"{spec['name']}: image model returned HTTP {result.get('http_status')} "
                    f"({str(result.get('error'))[:160]}) -- file skipped, no placeholder emitted")
            receipt["notes"].append(note)
            print(f"[images] {note}", file=sys.stderr)
        receipt["images"].append(row)

    generated = sum(1 for i in receipt["images"] if i["ok"])
    if generated == 0:
        receipt["notes"].append("no visuals shipped this run -- see per-image errors above")
    return finish(0 if generated else 1)


if __name__ == "__main__":
    sys.exit(main())
