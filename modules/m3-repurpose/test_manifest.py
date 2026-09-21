#!/usr/bin/env python3
"""Validate an M3 manifest.json against docs/module-api.md's M3 contract.

Usage: python3 test_manifest.py <run_dir>   (default: out/verify-w3/m3)
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "out" / "verify-w3" / "m3"
M = json.loads((RUN / "manifest.json").read_text())
# Every value any current code path emits. "html_render" was a retired template
# fallback: nothing writes it now, so accepting it would let a CSS render pass as
# a generated image.
SOURCES = {"llm", "image_model", "ffmpeg", "sarvam"}
KINDS = {"blog", "youtube", "infographic", "social", "extraction", "visual", "clip", "caption",
         "manifest", "report"}
failures = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


check("top-level keys present", {"event_slug", "generated_at", "lane", "models", "files",
                                 "shared_drive"} <= set(M))
check("lane is live or offline", M["lane"] in ("live", "offline"))
check("models names a text model", bool(M["models"].get("text")))
check("files is a non-empty list", isinstance(M["files"], list) and M["files"])
check("every file entry is complete", all({"name", "kind", "bytes", "source", "grounded"} <= set(f)
                                          for f in M["files"]))
check("every source is a declared provenance", all(f["source"] in SOURCES for f in M["files"]))
check("every kind is a declared kind", all(f["kind"] in KINDS for f in M["files"]))
check("every listed file exists on disk with the stated size",
      all((RUN / f["name"]).exists() and (RUN / f["name"]).stat().st_size == f["bytes"] for f in M["files"]))
check("the four channel assets are listed", {"blog", "youtube", "infographic", "social"}
      <= {f["kind"] for f in M["files"]})
check("text assets carry a grounded flag", all(isinstance(f["grounded"], bool) for f in M["files"]
                                               if f["kind"] in ("blog", "youtube", "infographic", "social")))
check("no visual claims a source it did not come from",
      all(f["source"] in ("image_model", "ffmpeg") for f in M["files"] if f["kind"] == "visual"))
check("shared_drive is present and empty until record",
      M["shared_drive"].get("files") == [] and not M["shared_drive"].get("folder_url"))
check("llm receipt is declared and on disk",
      (RUN / M["llm"]["receipt"]).exists() and M["llm"]["calls_made"] <= M["llm"]["budget"])

print(f"\ntest_manifest: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
