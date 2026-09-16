#!/usr/bin/env python3
"""Zero-cost, zero-network verification of repurpose.py's spec gate.

Monkeypatches repurpose.call_llm so run_live()'s regenerate-on-fail loop is
exercised without spending a real LLM call. Proves:
  1. An over-length blog.md regenerates with the SPECIFIC failure appended to the
     retry prompt, and succeeds once the mocked model fixes it.
  2. An asset that never gets in spec FAILS THE RUN (blog.md is a hard gate).
  3. youtube.md missing a required section triggers the same gate/retry path.
  4. offline replay (the real, unmocked code path) still computes the gate.

Updated for the W3 contract (docs/module-api.md M3), which changed three things
this file used to assert:
  - the chokepoint is call_llm(prompt, purpose, ledger) -- budget-counted and
    receipted -- not the old call_claude(prompt);
  - MAX_SPEC_ATTEMPTS is 2 (first attempt + one corrective retry), not 3,
    because the run-wide LLM budget is 8 calls;
  - an out-of-spec blog.md now RAISES instead of shipping flagged. "Ship it
    flagged" was the v1 behaviour the reviewer rejected: an 1,800-word draft in
    a folder labelled "800-1200 words" is a broken deliverable, not a warning.

Run: python3 test_spec_gate.py
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "m3-repurpose"))
import repurpose  # noqa: E402

EVENT = json.loads((REPO_ROOT / "data" / "incoming" / "event.json").read_text())
TRANSCRIPT = (REPO_ROOT / "data" / "incoming" / "transcript.md").read_text()
TURNS = repurpose.verify_grounding.parse_transcript(TRANSCRIPT)
LONG_TURNS = [t for t in TURNS if len(t["text"]) > 200]

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


def verbatim(turn, n=14, skip=3):
    """A contiguous span of a real transcript turn -- the extraction verifier
    drops anything that is not actually in the transcript, so the fixture has
    to be real too."""
    return " ".join(turn["text"].split()[skip:skip + n]).strip(" ,.")


GOOD_EXTRACTION = json.dumps({
    "moments": [{"start": t["ts"], "end": TURNS[min(i + 2, len(TURNS) - 1)]["ts"],
                 "title": f"Moment {i}", "speaker": t["speaker"], "why": "stands alone",
                 "clip_worthiness": 0.8} for i, t in enumerate(LONG_TURNS[:7])],
    "insights": [{"insight": f"Insight {i}", "speaker": t["speaker"], "timestamp": t["ts"],
                  "so_what": "matters"} for i, t in enumerate(LONG_TURNS[:6])],
    "quotes": [{"quote": verbatim(t), "speaker": t["speaker"], "timestamp": t["ts"], "topic": "t"}
               for t in LONG_TURNS[:9]],
    "data_points": [{"value": v, "what": "measure", "speaker": t["speaker"], "timestamp": t["ts"]}
                    for v, t in zip(["1,800 employees", "10 countries", "100 a month", "10 months",
                                     "1,000 employees", "50 people"], LONG_TURNS)],
})
GOOD_YOUTUBE = ("## Chapters\n00:00 Intro\n11:37 Governance\n23:52 Resistance\n\n## Description\n"
                + words(140) + "\n\n## Thumbnail Brief\nx")
GOOD_INFOGRAPHIC = ("## Headline Options\nx\n\n## Data Points\n"
                    + "\n".join(f"**{n} stat** — what it measures *Q Hamirani, HighLevel [00:17]*"
                                for n in range(1, 7))
                    + "\n\n## Layout\nx")
HOOKS = ["contrarian stat", "story", "listicle", "quote card", "question", "hot take",
         "data viz callout", "speaker spotlight"]
GOOD_SOCIAL = "\n".join(
    f"### Post {n}\n**Platform:** {'LinkedIn' if n <= 5 else 'X'}\n**Hook style:** {HOOKS[n - 1]}\n"
    f"**Post:** body [link]" for n in range(1, 9))


def make_fake(responses):
    """responses: asset name -> callable(attempt_index) -> text."""
    calls, counts = [], {}

    def fake(prompt, purpose, ledger, max_tokens=8000):
        ledger.spend(purpose)
        ledger.record(model="mock", purpose=purpose, latency_ms=1, http_status=200, parse_ok=True)
        calls.append(prompt)
        asset = route(prompt)
        counts[asset] = counts.get(asset, 0) + 1
        return responses[asset](counts[asset])
    return fake, calls, counts


def scenario_blog_fixes_on_retry(tmp_out: Path):
    print("\n=== scenario 1: blog.md over cap -> fixes on the one corrective retry ===")
    fake, calls, _ = make_fake({
        "extraction": lambda n: GOOD_EXTRACTION,
        "blog.md": lambda n: "# Title\n\n" + words(1277 if n == 1 else 1000),
        "youtube.md": lambda n: GOOD_YOUTUBE,
        "infographic.md": lambda n: GOOD_INFOGRAPHIC,
        "social.md": lambda n: GOOD_SOCIAL,
    })
    out_dir = tmp_out / "s1"
    out_dir.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(repurpose, "call_llm", side_effect=fake):
        manifest = repurpose.run_live(TRANSCRIPT, EVENT, out_dir)

    gate = manifest["_spec_gate"]
    check("blog.md regenerated exactly once (attempts==2)", gate["blog.md"]["attempts"] == 2, gate["blog.md"])
    check("blog.md ends within_spec True after the retry", gate["blog.md"]["within_spec"] is True, gate["blog.md"])
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
    check("the whole run stayed inside the 8-call budget",
          manifest["llm_calls_made"] <= repurpose.LLM_CALL_BUDGET, manifest["llm_calls_made"])
    check("extraction.json kept only transcript-verified quotes",
          all(q["match_ratio"] >= repurpose.verify_grounding.QUOTE_RATIO_THRESHOLD
              for q in json.loads((out_dir / "extraction.json").read_text())["quotes"]))


def scenario_never_fixes_fails_the_run(tmp_out: Path):
    print("\n=== scenario 2: blog.md never gets in spec -> the run FAILS (hard gate) ===")
    fake, _, counts = make_fake({
        "extraction": lambda n: GOOD_EXTRACTION,
        "blog.md": lambda n: "# Title\n\n" + words(1502),
        "youtube.md": lambda n: GOOD_YOUTUBE,
        "infographic.md": lambda n: GOOD_INFOGRAPHIC,
        "social.md": lambda n: GOOD_SOCIAL,
    })
    out_dir = tmp_out / "s2"
    out_dir.mkdir(parents=True, exist_ok=True)
    raised = None
    with mock.patch.object(repurpose, "call_llm", side_effect=fake):
        try:
            repurpose.run_live(TRANSCRIPT, EVENT, out_dir)
        except RuntimeError as e:
            raised = e

    check("run raised instead of shipping an out-of-spec blog", raised is not None)
    check("the error names the word count and the gate",
          raised is not None and "1504 words" in str(raised) and "hard spec gate" in str(raised), str(raised))
    check("blog.md was attempted exactly MAX_SPEC_ATTEMPTS (2) times, not more",
          counts["blog.md"] == repurpose.MAX_SPEC_ATTEMPTS, counts)
    check("the rejected draft is still on disk for inspection, marked REJECTED",
          (out_dir / "blog.md").exists() and "live-REJECTED" in (out_dir / "blog.md").read_text())


def scenario_youtube_missing_section(tmp_out: Path):
    print("\n=== scenario 3: youtube.md missing '## Thumbnail Brief' -> gate catches it ===")
    fake, _, _ = make_fake({
        "extraction": lambda n: GOOD_EXTRACTION,
        "blog.md": lambda n: "# Title\n\n" + words(1000),
        "youtube.md": lambda n: ("## Chapters\n00:00 Intro\n11:37 Two\n23:52 Three\n\n## Description\n"
                                 + words(140)) if n == 1 else GOOD_YOUTUBE,
        "infographic.md": lambda n: GOOD_INFOGRAPHIC,
        "social.md": lambda n: GOOD_SOCIAL,
    })
    out_dir = tmp_out / "s3"
    out_dir.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(repurpose, "call_llm", side_effect=fake):
        manifest = repurpose.run_live(TRANSCRIPT, EVENT, out_dir)
    gate = manifest["_spec_gate"]
    check("youtube.md regenerated once for the missing section", gate["youtube.md"]["attempts"] == 2, gate["youtube.md"])
    check("youtube.md ends within_spec True", gate["youtube.md"]["within_spec"] is True, gate["youtube.md"])
    check("a chapter timestamp that is not in the transcript is rejected",
          repurpose.check_asset_spec("youtube.md", "## Chapters\n00:00 a\n07:59 b\n23:52 c\n\n"
                                     "## Description\nx\n\n## Thumbnail Brief\nx",
                                     repurpose.transcript_timestamps(TRANSCRIPT, EVENT))[0] is False)
    check("social.md with a duplicated hook style is rejected",
          repurpose.check_asset_spec("social.md", GOOD_SOCIAL.replace("**Hook style:** story",
                                                                      "**Hook style:** contrarian stat"))[0] is False)


def offline_still_computes_gate(tmp_out: Path):
    print("\n=== scenario 4: offline replay (real code path, no mock) still populates _spec_gate ===")
    if not repurpose.FINGERPRINT_PATH.exists():
        check("sample_output present to replay", False, "run live once to populate sample_output/")
        return
    out_dir = tmp_out / "s4"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = repurpose.run_offline(EVENT, out_dir)
    gate = manifest["_spec_gate"]
    check("offline gate has all 4 assets", set(gate) == set(repurpose.ASSETS), gate)
    check("cached sample_output is in spec (blog+social+sections all True)",
          all(g["within_spec"] for g in gate.values()), gate)
    check("offline attempts are 0 (no LLM was called to produce this replay)",
          all(g["attempts"] == 0 for g in gate.values()), gate)
    check("offline makes zero LLM calls", manifest["llm_calls_made"] == 0, manifest["llm_calls_made"])


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp_out = Path(td)
        scenario_blog_fixes_on_retry(tmp_out)
        scenario_never_fixes_fails_the_run(tmp_out)
        scenario_youtube_missing_section(tmp_out)
        offline_still_computes_gate(tmp_out)

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
