#!/usr/bin/env python3
"""Zero-cost, zero-network verification of repurpose.py's new spec gate.

Monkeypatches repurpose.call_claude so run_live()'s regenerate-on-fail loop
is exercised without spending a single real LLM call (claude -p or
OpenRouter). Proves:
  1. An over-length blog.md (1279w, mirroring the real shipped 1277w
     defect) regenerates with the SPECIFIC failure appended to the retry
     prompt, and succeeds once the mocked model "fixes" it.
  2. An asset that never fixes itself is retried exactly MAX_SPEC_ATTEMPTS
     (3) times, then ships anyway with within_spec=False recorded loudly
     in the gate result -- not silently, and not blocked either.
  3. youtube.md missing a required section triggers the same gate/retry
     path (not just the two numeric checks).
  4. offline mode (the real, unmocked code path) still populates the gate.

Prompts are routed to a fake response by their unique `# Prompt: <Name>`
heading (prompts/extraction.md / blog.md / youtube.md / infographic.md /
social.md each have one) -- robust regardless of how many times a prompt
is retried, unlike counting raw call-order.

Run: python3 test_spec_gate.py
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path("/Users/kiran/Desktop/Claude GOD/career/postevent-engine")
sys.path.insert(0, str(REPO_ROOT / "modules" / "m3-repurpose"))
import repurpose  # noqa: E402

EVENT = json.loads((REPO_ROOT / "data" / "incoming" / "event.json").read_text())
TRANSCRIPT = (REPO_ROOT / "data" / "incoming" / "transcript.md").read_text()

PASS_COUNT, FAIL_COUNT = 0, 0


def check(label, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {label}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {label}  {detail}")


def words(n):
    return "word " * n


def route(prompt: str) -> str:
    for marker, name in (
        ("# Prompt: Extraction Pass", "extraction"),
        ("# Prompt: Blog Draft", "blog.md"),
        ("# Prompt: YouTube Package", "youtube.md"),
        ("# Prompt: Infographic Outline", "infographic.md"),
        ("# Prompt: Social Post Package", "social.md"),
    ):
        if marker in prompt:
            return name
    raise AssertionError(f"un-routable prompt: {prompt[:80]!r}")


GOOD_YOUTUBE = "## Chapters\n00:00 Intro\n\n## Description\n" + words(140) + "\n\n## Thumbnail Brief\nx"
GOOD_INFOGRAPHIC = "## Headline Options\nx\n\n## Data Points\nx\n\n## Layout\nx"
GOOD_SOCIAL = "\n".join(f"### Post {n}\n**Platform:** LinkedIn\nbody [link]" for n in range(1, 9))


def scenario_blog_fixes_on_retry(tmp_out: Path):
    print("\n=== scenario 1: blog.md over cap (real shipped defect shape) -> fixes on attempt 2 ===")
    calls = []
    blog_attempts = {"n": 0}

    def fake_call_claude(prompt):
        calls.append(prompt)
        asset = route(prompt)
        if asset == "extraction":
            return '{"key_moments": [], "quotes": [], "data_points": []}'
        if asset == "blog.md":
            blog_attempts["n"] += 1
            if blog_attempts["n"] == 1:
                return "# Title\n\n" + words(1277)  # over cap, like the real defect
            return "# Title\n\n" + words(1000)  # fixed on regen
        if asset == "youtube.md":
            return GOOD_YOUTUBE
        if asset == "infographic.md":
            return GOOD_INFOGRAPHIC
        return GOOD_SOCIAL

    out_dir = tmp_out / "s1"
    out_dir.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(repurpose, "call_claude", side_effect=fake_call_claude):
        manifest = repurpose.run_live(TRANSCRIPT, EVENT, out_dir)

    gate = manifest["_spec_gate"]
    check("blog.md regenerated exactly once (attempts==2)", gate["blog.md"]["attempts"] == 2, gate["blog.md"])
    check("blog.md ends within_spec True after retry", gate["blog.md"]["within_spec"] is True, gate["blog.md"])
    check("blog.md on-disk word count is now in spec",
          800 <= repurpose.word_count((out_dir / "blog.md").read_text()) <= 1200)
    check("every other asset attempted exactly once (no cross-contamination)",
          all(gate[n]["attempts"] == 1 for n in ("youtube.md", "infographic.md", "social.md")),
          {n: gate[n]["attempts"] for n in ("youtube.md", "infographic.md", "social.md")})
    retry_prompts = [c for c in calls if "REGENERATION NOTE" in c]
    check("exactly one regeneration prompt sent", len(retry_prompts) == 1, len(retry_prompts))
    check("regeneration prompt names the SPECIFIC failure (1279 words, cut 79+)",
          bool(retry_prompts) and "1279 words" in retry_prompts[0] and "cut 79" in retry_prompts[0],
          retry_prompts[0][-300:] if retry_prompts else None)
    check("llm_calls_made counts every attempt (1 extraction + 2 blog + 3 others = 6)",
          manifest["llm_calls_made"] == 6, manifest["llm_calls_made"])


def scenario_never_fixes_ships_flagged(tmp_out: Path):
    print("\n=== scenario 2: blog.md never gets in spec -> ships after 3 attempts, flagged not blocked ===")

    def fake_call_claude(prompt):
        asset = route(prompt)
        if asset == "extraction":
            return '{"key_moments": [], "quotes": [], "data_points": []}'
        if asset == "blog.md":
            return "# Title\n\n" + words(1502)  # always over cap, no matter how many retries
        if asset == "youtube.md":
            return GOOD_YOUTUBE
        if asset == "infographic.md":
            return GOOD_INFOGRAPHIC
        return GOOD_SOCIAL

    out_dir = tmp_out / "s2"
    out_dir.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(repurpose, "call_claude", side_effect=fake_call_claude):
        manifest = repurpose.run_live(TRANSCRIPT, EVENT, out_dir)

    gate = manifest["_spec_gate"]
    check("blog.md attempted exactly MAX_SPEC_ATTEMPTS (3) times, not more",
          gate["blog.md"]["attempts"] == repurpose.MAX_SPEC_ATTEMPTS, gate["blog.md"])
    check("blog.md within_spec is False (never silently marked True)",
          gate["blog.md"]["within_spec"] is False, gate["blog.md"])
    check("blog.md detail names the exact final overage",
          "1504 words" in gate["blog.md"]["detail"] and "cut 304" in gate["blog.md"]["detail"],
          gate["blog.md"]["detail"])
    check("asset was still WRITTEN to disk despite failing (ship + loud flag, not a hard block)",
          (out_dir / "blog.md").exists())
    check("manifest[assets][blog.md].words reflects the actually-shipped (over-cap) text",
          manifest["assets"]["blog.md"]["words"] > 1200, manifest["assets"]["blog.md"]["words"])


def scenario_youtube_missing_section(tmp_out: Path):
    print("\n=== scenario 3: youtube.md missing '## Thumbnail Brief' -> gate catches required-section failure ===")
    yt_attempts = {"n": 0}

    def fake_call_claude(prompt):
        asset = route(prompt)
        if asset == "extraction":
            return '{"key_moments": [], "quotes": [], "data_points": []}'
        if asset == "blog.md":
            return "# Title\n\n" + words(1000)
        if asset == "youtube.md":
            yt_attempts["n"] += 1
            if yt_attempts["n"] == 1:
                return "## Chapters\n00:00 Intro\n\n## Description\n" + words(140)  # missing Thumbnail Brief
            return GOOD_YOUTUBE
        if asset == "infographic.md":
            return GOOD_INFOGRAPHIC
        return GOOD_SOCIAL

    out_dir = tmp_out / "s3"
    out_dir.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(repurpose, "call_claude", side_effect=fake_call_claude):
        manifest = repurpose.run_live(TRANSCRIPT, EVENT, out_dir)

    gate = manifest["_spec_gate"]
    check("youtube.md regenerated exactly once for the missing section", gate["youtube.md"]["attempts"] == 2, gate["youtube.md"])
    check("youtube.md ends within_spec True", gate["youtube.md"]["within_spec"] is True, gate["youtube.md"])


def offline_still_computes_gate(tmp_out: Path):
    print("\n=== scenario 4: offline mode (real code path, no mock) still populates _spec_gate ===")
    out_dir = tmp_out / "s4"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = repurpose.run_offline(EVENT, out_dir)
    gate = manifest["_spec_gate"]
    check("offline gate has all 4 assets", set(gate) == {"blog.md", "youtube.md", "infographic.md", "social.md"}, gate)
    check("offline sample_output is in spec (blog+social+sections all True)",
          all(g["within_spec"] for g in gate.values()), gate)
    check("offline attempts are always 1 (no LLM to retry against)",
          all(g["attempts"] == 1 for g in gate.values()), gate)


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp_out = Path(td)
        scenario_blog_fixes_on_retry(tmp_out)
        scenario_never_fixes_ships_flagged(tmp_out)
        scenario_youtube_missing_section(tmp_out)
        offline_still_computes_gate(tmp_out)

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
