#!/usr/bin/env python3
"""Render M3 visual assets (YouTube thumbnail, LinkedIn quote card) from the
thumbnail brief in sample_output/youtube.md — HTML/CSS → PNG via headless
Chromium. Optional tooling: needs `pip install playwright && playwright install
chromium`. The core offline lane never imports this.

Usage: python3 modules/m3-repurpose/tools/render_visuals.py [--out DIR]
Production lane swaps this for an image-generation API call with the same
brief text as the prompt (see prompts/ and README.md).
"""
import argparse
import sys
from pathlib import Path

NAVY, AMBER, WHITE = "#0B2545", "#E8A33D", "#FFFFFF"

THUMBNAIL = f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;width:1280px;height:720px;background:{NAVY};font-family:-apple-system,Inter,Helvetica,Arial,sans-serif;color:{WHITE};overflow:hidden}}
  .frame{{position:absolute;inset:18px;border:3px solid {AMBER};border-radius:6px}}
  .bar{{position:absolute;top:58px;left:60px;right:60px;background:#071A33;padding:22px 28px;border-left:10px solid {AMBER}}}
  .h1{{font-size:74px;font-weight:900;letter-spacing:-1px;line-height:1}}
  .stat{{position:absolute;right:70px;bottom:62px;font-size:34px;font-weight:700;color:{AMBER}}}
  .people{{position:absolute;left:0;right:0;top:250px;display:flex;justify-content:center;gap:90px}}
  .p{{width:190px;height:300px;position:relative}}
  .head{{width:110px;height:110px;border-radius:55px;background:#1E3A5F;margin:0 auto;border:4px solid #2B4C78}}
  .body{{width:190px;height:170px;background:#1E3A5F;border-radius:95px 95px 14px 14px;margin-top:-14px;border:4px solid #2B4C78}}
  .name{{position:absolute;bottom:-46px;left:-30px;right:-30px;text-align:center;font-size:22px;color:#C9D6E8;font-weight:600}}
  .center .head,.center .body{{background:#24496F}}
  .logo{{position:absolute;left:60px;bottom:58px;display:flex;align-items:center;gap:12px;font-size:22px;font-weight:700;color:#C9D6E8}}
  .logo i{{display:inline-block;width:26px;height:26px;background:{AMBER};border-radius:6px}}
</style></head><body>
<div class="frame"></div>
<div class="bar"><div class="h1">YOUR LEADS ARE DYING IN 4 HOURS</div></div>
<div class="people">
  <div class="p"><div class="head"></div><div class="body"></div><div class="name">Daniel Kim</div></div>
  <div class="p center"><div class="head"></div><div class="body"></div><div class="name">Priya Nair</div></div>
  <div class="p"><div class="head"></div><div class="body"></div><div class="name">Sara Alvarez</div></div>
</div>
<div class="logo"><i></i>ACME Revenue Cloud</div>
<div class="stat">3.2x reply rate — real data.</div>
</body></html>"""

QUOTE_CARD = f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;width:1080px;height:1080px;background:{NAVY};font-family:Georgia,'Times New Roman',serif;color:{WHITE};overflow:hidden}}
  .frame{{position:absolute;inset:40px;border:3px solid {AMBER}}}
  .q{{position:absolute;left:110px;right:110px;top:230px;font-size:58px;line-height:1.25;font-weight:500}}
  .q:before{{content:"\\201C";color:{AMBER};font-size:150px;position:absolute;left:-70px;top:-60px;line-height:1}}
  .who{{position:absolute;left:110px;right:110px;top:690px;font-family:-apple-system,Inter,Helvetica,Arial,sans-serif;font-size:26px;color:{AMBER};font-weight:600}}
  .who small{{display:block;color:#C9D6E8;font-weight:400;font-size:22px;margin-top:6px}}
  .cap{{position:absolute;right:110px;bottom:90px;font-family:-apple-system,Inter,Helvetica,Arial,sans-serif;font-size:20px;color:#C9D6E8}}
</style></head><body>
<div class="frame"></div>
<div class="q">Follow-up within 4 hours lifts reply rate 3.2x. By 48 hours you're back to baseline.</div>
<div class="who">Daniel Kim<small>Head of Demand Gen, Northwind Analytics</small></div>
<div class="cap">Pipeline After the Webinar · ACME Revenue Cloud</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "sample_output" / "visuals"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — `pip install playwright && playwright install chromium`", file=sys.stderr)
        sys.exit(1)
    jobs = [("youtube-thumbnail.png", THUMBNAIL, 1280, 720), ("quote-card-linkedin.png", QUOTE_CARD, 1080, 1080)]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, html, w, h in jobs:
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            page.set_content(html)
            page.screenshot(path=str(out / name), full_page=False)
            page.close()
            print(f"wrote {out / name}")
        browser.close()


if __name__ == "__main__":
    main()
