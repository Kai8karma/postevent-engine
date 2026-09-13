#!/usr/bin/env python3
"""Schema + grounding gate for an M2 run: python3 test_plan_schema.py <out_dir>"""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out/verify-w2/m2")
plan = json.loads((OUT / "dispatch_plan.json").read_text())
grounding = json.loads((OUT / "grounding.json").read_text())
calls = json.loads((OUT / "receipts" / "m2_llm_calls.json").read_text())

for key in ("run_id", "event_slug", "generated_at", "lane", "event_close_ts", "variants",
            "recipients", "approval", "counts"):
    assert key in plan, f"dispatch_plan.json missing {key}"
assert plan["lane"] in ("live", "offline"), plan["lane"]
assert set(plan["variants"]) == {"attendee", "no_show", "speaker"}, sorted(plan["variants"])
for seg, v in plan["variants"].items():
    for key in ("subject_a", "subject_b", "preheader", "body_md"):
        assert isinstance(v[key], str) and v[key].strip(), f"{seg}.{key}"
    assert isinstance(v["takeaways"], list) and v["takeaways"], f"{seg}.takeaways"
assert isinstance(plan["variants"]["speaker"]["snapshot"], dict), "speaker.snapshot"
assert plan["approval"] == {"status": "pending_human_approval", "approved_by": None,
                            "approved_at": None, "mode": None}, plan["approval"]
counts = plan["counts"]
assert set(counts) == {"attendee", "no_show", "speaker", "mailable", "suppressed"}, sorted(counts)
assert counts["mailable"] == len(plan["recipients"]) == counts["attendee"] + counts["no_show"] + counts["speaker"]
for r in plan["recipients"]:
    assert set(r) == {"email", "hubspot_contact_id", "segment", "firstname", "function",
                      "industry_bucket", "subject", "body_html", "body_text", "links", "utm",
                      "demo_redirect_to"}, sorted(r)
    assert r["segment"] in plan["variants"] and "@" in r["email"] and r["subject"].strip()
    assert r["body_html"].startswith("<") and "{{" not in r["body_html"] and "{{" not in r["body_text"]
    assert all(f"utm_{k}=" in r["links"]["recording"] for k in ("source", "medium", "campaign", "content"))
    assert r["utm"]["campaign"] == plan["event_slug"] and r["utm"]["content"].startswith(r["segment"])
assert sum(1 for r in plan["recipients"] if r["demo_redirect_to"]) == 2, "expect one demo redirect per bulk segment"
assert grounding["status"] == "pass" and grounding["failed"] == 0, grounding["status"]
assert grounding["checks_total"] > 0 and len(calls) <= 5, (grounding["checks_total"], len(calls))
print(f"PASS {OUT}: lane={plan['lane']} recipients={counts['mailable']} suppressed={counts['suppressed']} "
      f"grounding={grounding['passed']}/{grounding['checks_total']} llm_calls={len(calls)}")
