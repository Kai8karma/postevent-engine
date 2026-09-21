#!/usr/bin/env python3
"""M4 dashboard tests -- stdlib unittest, offline only.

    python3 modules/m4-dashboard/test_dashboard.py

Every test runs in the offline or --live-dry-run lane. `urllib.request.urlopen` is
replaced module-wide with a raiser, so any test that tried to touch the network would
fail instead of quietly hitting HubSpot or OpenRouter.
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR))

import dashboard  # noqa: E402

_REAL_URLOPEN = urllib.request.urlopen


def setUpModule():
    def _no_network(*args, **kwargs):
        raise AssertionError("network call attempted in an offline test")
    urllib.request.urlopen = _no_network


def tearDownModule():
    urllib.request.urlopen = _REAL_URLOPEN


def run_cli(*argv):
    """Run dashboard.main() in-process; returns (exit_code, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = dashboard.main(list(argv))
    return code, buf.getvalue()


def last_summary(stdout: str, phase: str) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.startswith(f"M4 {phase} (")]
    return lines[-1] if lines else ""


def contact(email, *, attendance="attended", stage="", history=None, opens=0, clicks=0,
            pageviews=0, form_fills=0, company=None, cid=None, last_engaged=None):
    return {
        "id": cid or email, "email": email, "firstname": "", "lastname": "",
        "company": company if company is not None else dashboard.display_company(
            dashboard.domain_of(email)),
        "domain": dashboard.domain_of(email), "jobtitle": "", "country": "",
        "lifecyclestage": stage, "lifecycle_history": history or [], "icp_tier": "",
        "region": "", "owner": "", "attendance_status": attendance,
        "postevent_event": "slug-under-test",
        "engagement": {"opens": opens, "clicks": clicks, "pageviews": pageviews,
                       "form_fills": form_fills, "last_engaged": last_engaged,
                       "source": "fixture"},
    }


def snapshot_of(contacts):
    return {"event_slug": "slug-under-test", "pulled_at": "2026-09-12T00:00:00",
            "lane": "offline", "method": "offline", "contacts": contacts,
            "companies": dashboard.companies_from_contacts(contacts),
            "email_engagements": [], "events": [], "notes": []}


class OfflineRun(unittest.TestCase):
    """One offline `all` run shared by the snapshot / analysis / render assertions."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.code, cls.stdout = run_cli("all", "--out", str(cls.out), "--offline")
        cls.snapshot = json.loads((cls.out / "snapshot.json").read_text())
        cls.analysis = json.loads((cls.out / "analysis.json").read_text())
        cls.data = json.loads((cls.out / "dashboard_data.json").read_text())
        cls.html = (cls.out / "index.html").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_code_and_files(self):
        self.assertEqual(self.code, 0, self.stdout)
        for name in ("snapshot.json", "analysis.json", "dashboard_data.json", "index.html",
                     "receipts/m4_seed.json", "receipts/m4_hubspot_sync.json",
                     "receipts/m4_llm_calls.json"):
            self.assertTrue((self.out / name).exists(), f"{name} not written")

    def test_summary_line_per_phase(self):
        for phase, lane in (("seed", "offline"), ("sync", "offline"), ("analyze", "rules"),
                            ("render", "offline")):
            line = last_summary(self.stdout, phase)
            self.assertRegex(line, rf"^M4 {phase} \({lane}\): .+ -> {re.escape(str(self.out))}$")

    def test_seed_receipt_shape(self):
        receipt = json.loads((self.out / "receipts" / "m4_seed.json").read_text())
        self.assertLessEqual({"event_slug", "lane", "method", "contacts_matched", "events_written",
                              "lifecycle_updates", "errors", "timestamps"}, set(receipt))
        self.assertEqual(receipt["method"], "offline")
        self.assertEqual(receipt["lane"], "offline")
        self.assertEqual(receipt["source"], "seeded")
        self.assertEqual(receipt["errors"], [])
        fixture = json.loads(dashboard.ENGAGEMENT_FIXTURE.read_text())
        self.assertEqual(receipt["events_written"], len(fixture["events"]))
        self.assertEqual(receipt["lifecycle_updates"], len(dashboard.final_stages(fixture)))

    def test_snapshot_shape_from_fixtures(self):
        snap = self.snapshot
        self.assertEqual(snap["lane"], "offline")
        self.assertEqual(snap["method"], "offline")
        self.assertEqual(snap["event_slug"], "darwinbox-ai-in-hr-2026-08-13")
        self.assertTrue(snap["contacts"] and snap["companies"])
        required = {"id", "email", "firstname", "lastname", "company", "domain", "jobtitle",
                    "lifecyclestage", "lifecycle_history", "icp_tier", "region", "owner",
                    "postevent_event", "engagement"}
        for c in snap["contacts"]:
            self.assertLessEqual(required, set(c), c["email"])
            self.assertEqual(c["engagement"]["source"], "fixture")
            self.assertEqual(c["postevent_event"], snap["event_slug"])
            for h in c["lifecycle_history"]:
                self.assertEqual(set(h), {"stage", "ts", "source"})
                self.assertIn(h["source"], ("hubspot_history", "seeded"))
        ids = {c["id"] for c in snap["contacts"]}
        for co in snap["companies"]:
            self.assertLessEqual({"id", "name", "domain", "contact_ids"}, set(co))
            self.assertTrue(set(co["contact_ids"]) <= ids)
        self.assertTrue(all(e["source"] == "fixture" for e in snap["events"]))
        self.assertEqual(snap["email_engagements"], [])

    def test_analysis_is_rules_lane_without_a_key(self):
        self.assertEqual(self.analysis["lane"], "rules")
        self.assertEqual(self.analysis["narrative_source"], "rules")
        self.assertIsNone(self.analysis["llm"])
        self.assertIsNone(self.analysis["model"])
        self.assertIn("deterministic", self.analysis)
        self.assertIn(str(self.analysis["deterministic"]["attendee_to_mql"]["attendees"]),
                      self.analysis["movement_narrative"])
        receipt = json.loads((self.out / "receipts" / "m4_llm_calls.json").read_text())
        self.assertEqual(receipt["completions"], 0)
        self.assertEqual(receipt["calls"], [])

    def test_render_contains_every_widget_and_the_source_badge(self):
        for widget in ("kpi-grid", "funnel-svg", "funnel-side", "accounts-table", "accounts-tbody",
                       "contacts-table", "contacts-tbody", "committee-grid", "movement-grid",
                       "sparkline-svg", "anomaly-threshold-note", "anomaly-grid", "narrative-body",
                       "narrative-badge", "narrative-badge-text", "validator-panel",
                       "source-footer", "event-name", "engagement-source", "data-lane"):
            self.assertIn(f'id="{widget}"', self.html, f"widget {widget} missing from index.html")
        self.assertIn("source-badge", self.html)
        self.assertIn("window.NARRATIVE_ENDPOINT", self.html)
        self.assertIn("?refresh=1", self.html)
        self.assertIn("25000", self.html)          # 25s refresh timeout
        self.assertNotIn("http://", self.html.split("<script")[0])   # no external assets

    def test_narrative_endpoint_survives_the_api_injection(self):
        """api/server.py injects `<script>window.NARRATIVE_ENDPOINT = "/narrative/<id>";</script>`
        immediately before the page's FIRST <script tag, so that tag must be the
        endpoint default and it must not clobber an injected value."""
        first = self.html.lower().find("<script")
        self.assertNotEqual(first, -1)
        self.assertIn('id="narrative-endpoint"', self.html[first:first + 40])
        self.assertIn("window.NARRATIVE_ENDPOINT = window.NARRATIVE_ENDPOINT || null",
                      self.html[first:first + 200])
        injected = (self.html[:first] + '<script>window.NARRATIVE_ENDPOINT = "/narrative/r1";</script>'
                    + self.html[first:])
        self.assertLess(injected.index('"/narrative/r1"'), injected.index('id="narrative-endpoint"'))

    def test_rules_lane_still_scores_lead_interest(self):
        det = self.analysis["deterministic"]
        self.assertTrue(det["interest_scores"])
        self.assertEqual(max(r["score"] for r in det["interest_scores"]), 100)
        self.assertTrue(all(r["source"] == "rules" and r["evidence"] for r in det["interest_scores"]))
        top = self.data["top_contacts"][0]
        self.assertEqual(top["rules_score"], 100)
        self.assertIsNone(top["llm_score"])

    def test_render_numbers_equal_dashboard_data(self):
        blob = re.search(r'<script type="application/json" id="dashboard-data">(.*?)</script>',
                         self.html, re.S).group(1)
        embedded = json.loads(blob.replace("<\\/", "</"))
        self.assertEqual(embedded, self.data)

    def test_dashboard_numbers_reconcile_to_snapshot(self):
        det = self.analysis["deterministic"]
        k = self.data["kpis"]
        self.assertEqual(k["contacts"], len(self.snapshot["contacts"]))
        self.assertEqual(k["companies"], len(self.snapshot["companies"]))
        self.assertEqual(k["email_engagements"], len(self.snapshot["email_engagements"]))
        self.assertEqual(k["mqls"], det["attendee_to_mql"]["mqls"])
        self.assertEqual(k["attendees"], det["attendee_to_mql"]["attendees"])
        self.assertEqual(k["mql_rate"], det["mql_rate"])
        self.assertEqual(k["mql_rate_pct"], round(det["mql_rate"] * 100, 1))
        self.assertEqual(k["movement_30d"], det["movement"]["30d"]["transitions"])
        self.assertEqual(k["committees"], len(self.data["committees"]))
        self.assertEqual(k["anomalies"], len(self.data["anomalies"]))
        self.assertEqual(k["engaged_contacts"],
                         sum(1 for c in self.snapshot["contacts"]
                             if dashboard.engagement_total(c["engagement"]) > 0))
        self.assertEqual(self.data["narrative"]["source"], "rules")
        self.assertEqual(self.data["lane"], {"data": "offline", "narrative": "rules",
                                             "engagement": "fixture"})


class MovementMath(unittest.TestCase):
    AS_OF = datetime(2026, 9, 12, 12, 0, 0)

    def history_contact(self):
        def at(**kw):
            return dashboard.iso(self.AS_OF - timedelta(**kw))
        return {
            "email": "mover@example.com", "company": "Example",
            "lifecycle_history": [
                {"stage": "lead", "ts": at(days=1), "source": "seeded"},
                {"stage": "marketingqualifiedlead", "ts": at(days=7), "source": "hubspot_history"},
                {"stage": "salesqualifiedlead", "ts": at(days=7, seconds=1), "source": "seeded"},
                {"stage": "opportunity", "ts": at(days=14), "source": "seeded"},
                {"stage": "customer", "ts": at(days=30, seconds=1), "source": "seeded"},
            ],
        }

    def test_windows_count_transitions_in_range(self):
        windows, rows = dashboard.movement_windows([self.history_contact()], self.AS_OF)
        self.assertEqual(windows["7d"]["transitions"], 2)     # -1d and exactly -7d
        self.assertEqual(windows["14d"]["transitions"], 4)    # + -7d-1s and exactly -14d
        self.assertEqual(windows["30d"]["transitions"], 4)    # -30d-1s falls outside
        self.assertEqual(len(rows), 5)

    def test_boundary_day_is_inside_the_window(self):
        windows, _ = dashboard.movement_windows([self.history_contact()], self.AS_OF)
        self.assertIn("marketingqualifiedlead", windows["7d"]["by_stage"])   # exactly on the cutoff
        self.assertNotIn("customer", windows["30d"]["by_stage"])             # one second outside

    def test_source_mix_is_stated_per_window(self):
        windows, _ = dashboard.movement_windows([self.history_contact()], self.AS_OF)
        self.assertEqual(windows["7d"]["source_mix"], {"seeded": 1, "hubspot_history": 1})
        self.assertEqual(windows["30d"]["source_mix"], {"seeded": 3, "hubspot_history": 1})
        self.assertEqual(windows["7d"]["contacts"], 1)

    def test_empty_history_is_zero_not_an_error(self):
        windows, rows = dashboard.movement_windows(
            [{"email": "quiet@example.com", "company": "", "lifecycle_history": []}], self.AS_OF)
        self.assertEqual(rows, [])
        self.assertEqual(windows["30d"], {"transitions": 0, "contacts": 0, "by_stage": {},
                                          "source_mix": {}, "dates": {},
                                          "disclosure": "No stage transitions in this window.",
                                          "window_start": dashboard.iso(self.AS_OF - timedelta(days=30)),
                                          "window_end": dashboard.iso(self.AS_OF)})


class CommitteeRule(unittest.TestCase):
    def test_two_engaged_contacts_at_one_company_is_a_committee(self):
        contacts = [
            contact("a@committee.com", clicks=2, company="Committee Co"),
            contact("b@committee.com", form_fills=1, company="Committee Co"),
            contact("c@single.com", clicks=3, company="Single Co"),
            contact("d@single.com", company="Single Co"),               # present, not engaged
            contact("e@gmail.com", clicks=9),                           # freemail: no account
        ]
        det = dashboard.deterministic_analysis(snapshot_of(contacts))
        self.assertEqual([c["company"] for c in det["committees"]], ["Committee Co"])
        self.assertEqual(sorted(det["committees"][0]["contacts"]),
                         ["a@committee.com", "b@committee.com"])
        single = next(a for a in det["top_accounts"] if a["company"] == "Single Co")
        self.assertEqual((single["engaged_contacts"], single["known_contacts"]), (1, 2))
        self.assertNotIn("gmail.com", [a["domain"] for a in det["top_accounts"]])

    def test_committee_threshold_is_the_documented_two(self):
        self.assertEqual(dashboard.COMMITTEE_MIN_CONTACTS, 2)


class MqlRate(unittest.TestCase):
    def test_rate_counts_attendees_only(self):
        contacts = [
            contact("one@x.com", stage="marketingqualifiedlead"),
            contact("two@x.com", stage="salesqualifiedlead"),
            contact("three@x.com", stage="lead"),
            contact("four@x.com", stage="subscriber"),
            contact("five@y.com", attendance="no_show", stage="opportunity"),   # not an attendee
        ]
        det = dashboard.deterministic_analysis(snapshot_of(contacts))
        self.assertEqual(det["attendee_to_mql"], {"attendees": 4, "mqls": 2, "sqls": 1})
        self.assertEqual(det["mql_rate"], 0.5)

    def test_no_attendees_is_zero_not_a_crash(self):
        det = dashboard.deterministic_analysis(snapshot_of([]))
        self.assertEqual(det["mql_rate"], 0.0)
        self.assertEqual(det["attendee_to_mql"], {"attendees": 0, "mqls": 0, "sqls": 0})


class Validator(unittest.TestCase):
    def det(self):
        contacts = [
            contact("top@acct-one.com", form_fills=3, clicks=2, stage="marketingqualifiedlead",
                    company="Acct One"),
            contact("second@acct-one.com", clicks=2, company="Acct One"),
            contact("third@acct-two.com", pageviews=2, company="Acct Two"),
            contact("fourth@acct-two.com", opens=2, company="Acct Two"),
        ]
        return dashboard.deterministic_analysis(snapshot_of(contacts))

    def test_agreement_is_recorded_when_the_model_matches(self):
        det = self.det()
        llm = {"top_contacts": [c["email"] for c in det["top_contacts"][:5]],
               "top_accounts": [a["company"] for a in det["top_accounts"][:5]],
               "committees": [{"company": c["company"]} for c in det["committees"]],
               "anomalies": [{"contact": a["contact"]} for a in det["anomalies"]]}
        result = dashboard.build_validator(det, llm)
        self.assertEqual(result["disagreements"], [])
        self.assertGreaterEqual(result["agreements"], 3)

    def test_planted_disagreement_is_flagged_not_overwritten(self):
        det = self.det()
        llm = {
            "top_contacts": ["fourth@acct-two.com"],                 # wrong order on purpose
            "top_accounts": [a["company"] for a in det["top_accounts"][:5]],
            "committees": [{"company": "Acct One"}, {"company": "A Company Not In The Snapshot"}],
            "movement_counts": {"7d": 99},
            "interest_scores": [{"contact_id": "ghost@nowhere.com", "score": 90}],
        }
        result = dashboard.build_validator(det, llm)
        fields = {d["field"] for d in result["disagreements"]}
        self.assertIn("top_contacts_top5", fields)
        self.assertIn("committee_companies", fields)
        self.assertIn("movement_7d", fields)
        self.assertIn("interest_scores_ungrounded", fields)
        movement = next(d for d in result["disagreements"] if d["field"] == "movement_7d")
        self.assertEqual(movement["deterministic"], det["movement"]["7d"]["transitions"])
        self.assertEqual(movement["llm"], 99)
        # the deterministic block is untouched by what the model claimed
        self.assertEqual([c["email"] for c in det["top_contacts"]][0], "top@acct-one.com")

    def test_silent_fields_are_neither_agreement_nor_disagreement(self):
        result = dashboard.build_validator(self.det(), {})
        self.assertEqual(result, {"agreements": 0, "disagreements": []})


class DryRun(unittest.TestCase):
    def test_prints_requests_and_prompts_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("all", "--out", tmp, "--live-dry-run")
        self.assertEqual(code, 0, out)
        requests = [ln for ln in out.splitlines() if ln.startswith("HUBSPOT ")]
        prompts = [ln for ln in out.splitlines() if ln.startswith("=== PROMPT ")]
        self.assertGreaterEqual(len(requests), 1)
        self.assertGreaterEqual(len(prompts), 1)
        for line in requests:
            self.assertRegex(line, r"^HUBSPOT \d+: (POST|GET|PATCH) https://api\.hubapi\.com/\S+ "
                                   r"\(\d+ body bytes\) -- .+$")
        for phase in ("seed", "sync", "analyze", "render"):
            self.assertRegex(last_summary(out, phase), rf"^M4 {phase} \(live-dry-run\): .+ -> ")
        self.assertIn("0 sent", out)

    def test_writes_no_snapshot_analysis_or_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_cli("all", "--out", tmp, "--live-dry-run")
            out = Path(tmp)
            for name in ("snapshot.json", "analysis.json", "dashboard_data.json", "index.html",
                         "receipts/m4_seed.json"):
                self.assertFalse((out / name).exists(),
                                 f"{name} must not exist after a dry run -- it never came from the portal")
            plan = json.loads((out / "receipts" / "m4_dry_run.json").read_text())
            self.assertEqual(plan["lane"], "live-dry-run")
            self.assertTrue(plan["hubspot_requests"] and plan["prompts"])


class CliGuards(unittest.TestCase):
    def test_offline_and_dry_run_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli("sync", "--out", tmp, "--offline", "--live-dry-run")
        self.assertEqual(code, 2)

    def test_analyze_without_a_snapshot_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_cli("analyze", "--out", tmp, "--offline")
        self.assertEqual(code, 1)
        self.assertIn("snapshot.json", err.getvalue())

    def test_event_tag_overrides_the_event_json_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("sync", "--out", tmp, "--offline", "--event-tag", "other-event-2026")
            self.assertEqual(code, 0, out)
            snap = json.loads((Path(tmp) / "snapshot.json").read_text())
        self.assertEqual(snap["event_slug"], "other-event-2026")
        self.assertTrue(all(c["postevent_event"] == "other-event-2026" for c in snap["contacts"]))


class AnomalyFence(unittest.TestCase):
    def test_threshold_moves_with_the_distribution(self):
        quiet = [contact(f"q{i}@x.com", opens=1) for i in range(20)]
        loud = contact("loud@x.com", opens=20, clicks=20, form_fills=5)
        det = dashboard.deterministic_analysis(snapshot_of(quiet + [loud]))
        flagged = {a["contact"] for a in det["anomalies"]}
        self.assertIn("loud@x.com", flagged)
        self.assertNotIn("q0@x.com", flagged)
        self.assertGreater(det["anomaly_detection"]["threshold"], 1)
        for a in det["anomalies"]:
            self.assertTrue(a["evidence_rows"], "every anomaly must carry its evidence rows")
            self.assertLessEqual({"contact", "metric", "value", "threshold", "evidence_rows"}, set(a))

    def test_high_engagement_without_stage_movement_is_called_out(self):
        det = dashboard.deterministic_analysis(snapshot_of(
            [contact(f"q{i}@x.com", opens=1) for i in range(20)]
            + [contact("stalled@x.com", clicks=12, form_fills=4)]))
        metrics = {(a["contact"], a["metric"]) for a in det["anomalies"]}
        self.assertIn(("stalled@x.com", "stalled_high_engagement"), metrics)


# ---------------------------------------------------------------------------
# live-path coverage without a network: a fake portal + a fake model
# ---------------------------------------------------------------------------

class FakePortal(dashboard.HubSpotClient):
    """Answers HubSpotClient's HTTP layer from memory: the real paging, batching and
    parsing code above it runs untouched, and no socket is ever opened. The live
    seed/sync paths are therefore exercised exactly as written."""

    def __init__(self, event_definition_status=403):
        super().__init__("test-token-not-a-real-secret")
        self.event_definition_status = event_definition_status
        self.requests = []
        self.contacts = {
            "301": {"email": "dsmith@chewy.com", "firstname": "David", "lastname": "Smith",
                    "company": "Chewy", "jobtitle": "TA Manager", "country": "US",
                    "lifecyclestage": "marketingqualifiedlead", "icp_tier": "tier1",
                    "attendance_status": "attended", "hubspot_owner_id": "77"},
            "302": {"email": "ejohnson@chewy.com", "firstname": "Erin", "lastname": "Johnson",
                    "company": "Chewy", "jobtitle": "HRBP", "country": "US",
                    "lifecyclestage": "lead", "icp_tier": "tier2",
                    "attendance_status": "attended", "hubspot_owner_id": "77"},
            "303": {"email": "anjalik@infosys.com", "firstname": "Anjali", "lastname": "K",
                    "company": "Infosys", "jobtitle": "CHRO", "country": "IN",
                    "lifecyclestage": "lead", "icp_tier": "tier1",
                    "attendance_status": "no_show", "hubspot_owner_id": ""},
        }
        self.history = {"301": [{"value": "marketingqualifiedlead", "timestamp": "2026-09-10T11:00:00Z"},
                                {"value": "lead", "timestamp": "2026-09-01T11:00:00Z"}],
                        "302": [{"value": "lead", "timestamp": "2026-09-08T11:00:00Z"}],
                        "303": [{"value": "lead", "timestamp": "2026-08-18T11:00:00Z"}]}
        self.updates = []

    def call(self, method, path, body=None, timeout=30, _retried=False):
        self.requests.append((method, path, body))
        self.calls.append({"method": method, "url": path, "status": 200})
        if path == "/crm/v3/objects/contacts/search":
            props = body["properties"]
            return 200, {"results": [
                {"id": cid, "properties": {k: row.get(k, "") for k in props}}
                for cid, row in sorted(self.contacts.items())]}, ""
        if path == "/events/v3/event-definitions":
            return self.event_definition_status, None, "MISSING_SCOPES"
        if path == "/events/v3/send":
            return 204, {}, ""
        if path == "/crm/v3/properties/contacts":
            return 201, {}, ""
        if path == "/crm/v3/objects/contacts/batch/update":
            self.updates.append(body["inputs"])
            for row in body["inputs"]:
                self.contacts[row["id"]].update(row["properties"])
            return 200, {"results": body["inputs"]}, ""
        if path == "/crm/v3/objects/contacts/batch/read":
            props = body["properties"]
            return 200, {"results": [
                {"id": i["id"],
                 "properties": {k: self.contacts[i["id"]].get(k, "") for k in props},
                 "propertiesWithHistory": {"lifecyclestage": self.history.get(i["id"], [])}}
                for i in body["inputs"]]}, ""
        if path == "/crm/v4/associations/contacts/companies/batch/read":
            mapping = {"301": "900", "302": "900", "303": "901"}
            return 200, {"results": [{"from": {"id": i["id"]},
                                      "to": [{"toObjectId": mapping[i["id"]]}]}
                                     for i in body["inputs"] if i["id"] in mapping]}, ""
        if path == "/crm/v3/objects/companies/batch/read":
            rows = {"900": {"name": "Chewy", "domain": "chewy.com"},
                    "901": {"name": "Infosys", "domain": "infosys.com"}}
            return 200, {"results": [{"id": i["id"], "properties": rows[i["id"]]}
                                     for i in body["inputs"]]}, ""
        if path == "/crm/v4/associations/contacts/emails/batch/read":
            return 200, {"results": [{"from": {"id": "301"}, "to": [{"toObjectId": "5001"}]}]}, ""
        if path == "/crm/v3/objects/emails/batch/read":
            return 200, {"results": [{"id": "5001", "properties": {
                "hs_email_subject": "Thanks for joining", "hs_timestamp": "2026-08-14T09:00:00Z"}}]}, ""
        raise AssertionError(f"unexpected request: {method} {path}")


class LivePathAgainstAFakePortal(unittest.TestCase):
    def setUp(self):
        self.portal = FakePortal()
        self._real_client = dashboard.HubSpotClient
        self._real_token = dashboard.resolve_hubspot_token
        dashboard.HubSpotClient = lambda token: self.portal
        dashboard.resolve_hubspot_token = lambda: "test-token-not-a-real-secret"

    def tearDown(self):
        dashboard.HubSpotClient = self._real_client
        dashboard.resolve_hubspot_token = self._real_token

    def test_seed_falls_back_to_counter_properties_on_403_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("seed", "--out", tmp, "--event-tag", "darwinbox-ai-in-hr-2026-08-13")
            self.assertEqual(code, 0, out)
            receipt = json.loads((Path(tmp) / "receipts" / "m4_seed.json").read_text())
            first = json.dumps(self.portal.updates)
            self.portal.updates.clear()
            code2, _ = run_cli("seed", "--out", tmp, "--event-tag", "darwinbox-ai-in-hr-2026-08-13")
        self.assertEqual(code2, 0)
        self.assertEqual(receipt["method"], "contact_properties")
        self.assertEqual(receipt["lane"], "live")
        self.assertEqual(receipt["source"], "seeded")
        self.assertEqual(receipt["contacts_matched"], 3)
        self.assertGreater(receipt["events_written"], 0)
        self.assertEqual(receipt["errors"], [])
        self.assertEqual(first, json.dumps(self.portal.updates), "re-seeding must SET the same values")
        definition = [r for r in self.portal.requests if r[1] == "/events/v3/event-definitions"]
        self.assertEqual(len(definition), 2, "the event definition is tried once per run")
        created = {r[2]["name"] for r in self.portal.requests if r[1] == "/crm/v3/properties/contacts"}
        self.assertEqual(created, set(dashboard.COUNTER_PROPERTIES))
        for chunk in json.loads(first):
            self.assertLessEqual(len(chunk), 100)
        props = json.loads(first)[0][0]["properties"]
        self.assertIn("postevent_opens", props)
        self.assertIn("lifecyclestage", props)

    def test_custom_events_lane_when_the_portal_allows_event_definitions(self):
        self.portal.event_definition_status = 201
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("seed", "--out", tmp)
            self.assertEqual(code, 0, out)
            receipt = json.loads((Path(tmp) / "receipts" / "m4_seed.json").read_text())
        self.assertEqual(receipt["method"], "custom_events")
        sends = [r for r in self.portal.requests if r[1] == "/events/v3/send"]
        self.assertEqual(receipt["events_written"], len(sends))
        self.assertGreater(len(sends), 0)
        self.assertTrue(all(r[2]["occurredAt"].endswith("Z") for r in sends))
        self.assertEqual({r[2]["properties"]["event_slug"] for r in sends},
                         {"darwinbox-ai-in-hr-2026-08-13"})
        # no counter properties are created or written in this lane
        self.assertFalse([r for r in self.portal.requests if r[1] == "/crm/v3/properties/contacts"])
        for chunk in self.portal.updates:
            for row in chunk:
                self.assertEqual(set(row["properties"]), {"lifecyclestage"})

    def test_sync_reads_history_companies_and_email_engagements(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("seed", "--out", tmp)
            self.assertEqual(code, 0, out)
            code, out = run_cli("sync", "--out", tmp)
            self.assertEqual(code, 0, out)
            snap = json.loads((Path(tmp) / "snapshot.json").read_text())
            receipt = json.loads((Path(tmp) / "receipts" / "m4_hubspot_sync.json").read_text())
        self.assertEqual(snap["lane"], "live")
        self.assertEqual(snap["method"], "contact_properties")
        self.assertEqual(len(snap["contacts"]), 3)
        david = next(c for c in snap["contacts"] if c["email"] == "dsmith@chewy.com")
        self.assertEqual(david["engagement"]["source"], "seeded")
        self.assertGreater(david["engagement"]["opens"] + david["engagement"]["clicks"], 0)
        self.assertEqual([h["source"] for h in david["lifecycle_history"]],
                         ["hubspot_history"] * len(david["lifecycle_history"]))
        self.assertEqual([h["stage"] for h in david["lifecycle_history"]],
                         ["lead", "marketingqualifiedlead"])          # ascending
        self.assertEqual(david["region"], "NA")                        # from country via icp.yaml
        self.assertEqual({c["name"] for c in snap["companies"]}, {"Chewy", "Infosys"})
        self.assertEqual(snap["email_engagements"][0]["source"], "hubspot")
        self.assertEqual(snap["email_engagements"][0]["contact_id"], "301")
        urls = [e["url"] for e in receipt["endpoints"]]
        self.assertTrue(any("propertiesWithHistory" in (e.get("note") or "") for e in receipt["endpoints"]))
        self.assertTrue(any("associations/contacts/emails" in u for u in urls))
        self.assertEqual(receipt["totals"]["contacts"], 3)

    def test_analyze_live_records_model_validator_and_narrative(self):
        replies = {
            "anomalies": {"anomalies": [{"contact": "dsmith@chewy.com",
                                         "metric": "form_fills", "value": 3,
                                         "rationale": "pricing intent",
                                         "evidence": ["form_fills=3"]}]},
            "interest_scores": {"interest_scores": [{"contact_id": "dsmith@chewy.com",
                                                     "score": 88, "rationale": "pricing form",
                                                     "evidence": ["form_fills=3"]}],
                                "top_contacts": ["ejohnson@chewy.com"]},
            "movement_narrative": {"narrative": "Movement concentrated at Chewy this week.\n\n"
                                                "Nothing moved at Infosys.",
                                   "counts": {"7d": 999}},
            "committees": {"committees": [{"company": "Chewy", "contacts": ["dsmith@chewy.com"],
                                           "why": "two roles, one account"}],
                           "top_accounts": ["Chewy"]},
        }
        calls = []

        def fake_call_llm(prompt, purpose, ledger, max_tokens=4000):
            calls.append((purpose, len(prompt)))
            ledger.record(model="fake/model", purpose=purpose, attempt=1, ok=True,
                          prompt_chars=len(prompt),
                          tokens={"prompt": 100, "completion": 50, "total": 150},
                          latency_ms=12, http_status=200, chars=64)
            return json.dumps(replies[purpose]), "fake/model"

        real_call, real_key = dashboard.call_llm, dashboard.openrouter_key
        dashboard.call_llm = fake_call_llm
        dashboard.openrouter_key = lambda: "test-key-not-a-real-secret"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_cli("seed", "--out", tmp)
                run_cli("sync", "--out", tmp)
                code, out = run_cli("analyze", "--out", tmp)
                self.assertEqual(code, 0, out)
                analysis = json.loads((Path(tmp) / "analysis.json").read_text())
                ledger = json.loads((Path(tmp) / "receipts" / "m4_llm_calls.json").read_text())
                rcode, rout = run_cli("render", "--out", tmp)
                data = json.loads((Path(tmp) / "dashboard_data.json").read_text())
                html = (Path(tmp) / "index.html").read_text()
        finally:
            dashboard.call_llm, dashboard.openrouter_key = real_call, real_key

        self.assertEqual([c[0] for c in calls],
                         ["anomalies", "interest_scores", "movement_narrative", "committees"])
        self.assertEqual(analysis["lane"], "live")
        self.assertEqual(analysis["narrative_source"], "live")
        self.assertEqual(analysis["model"], "fake/model")
        self.assertIn("Chewy", analysis["movement_narrative"])
        self.assertEqual(ledger["completions"], 4)
        self.assertEqual(ledger["calls"][0]["tokens"]["total"], 150)
        self.assertIn("latency_ms", ledger["calls"][0])
        disagreements = {d["field"] for d in analysis["llm"]["validator"]["disagreements"]}
        self.assertIn("movement_7d", disagreements)          # the model claimed 999
        self.assertIn("top_contacts_top5", disagreements)
        self.assertEqual(analysis["deterministic"]["movement"]["7d"]["transitions"],
                         next(d["deterministic"] for d in analysis["llm"]["validator"]["disagreements"]
                              if d["field"] == "movement_7d"))
        self.assertEqual(rcode, 0, rout)
        self.assertEqual(data["narrative"]["source"], "live")
        self.assertEqual(data["lane"], {"data": "live", "narrative": "live", "engagement": "seeded"})
        chewy = next(c for c in data["committees"] if c["company"] == "Chewy")
        self.assertEqual((chewy["why"], chewy["why_source"]), ("two roles, one account", "llm"))
        david = next(c for c in data["top_contacts"] if c["email"] == "dsmith@chewy.com")
        self.assertEqual(david["llm_score"], 88)
        self.assertIn("live", html)
        self.assertIn("Movement concentrated at Chewy", html)

    def test_live_lane_without_a_token_fails_loud(self):
        dashboard.resolve_hubspot_token = lambda: None
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(err):
                code, _ = run_cli("sync", "--out", tmp)
        self.assertEqual(code, 1)
        self.assertIn("HUBSPOT_TOKEN", err.getvalue())


class SeededMovementIsDisclosed(unittest.TestCase):
    """A stage change the seed phase wrote lands in HubSpot's property history seconds
    later. It must not be presented as organic movement."""

    def test_rows_at_or_after_the_seed_run_are_labelled_seeded(self):
        contacts = [{"email": "a@b.com", "company": "B", "lifecycle_history": [
            {"stage": "lead", "ts": "2026-08-30T10:00:00", "source": "hubspot_history"},
            {"stage": "marketingqualifiedlead", "ts": "2026-09-21T15:11:40", "source": "seeded"},
        ]}]
        as_of = dashboard.parse_ts("2026-09-21T16:00:00")
        windows, _rows = dashboard.movement_windows(contacts, as_of)
        self.assertEqual(windows["7d"]["source_mix"], {"seeded": 1})
        self.assertEqual(windows["30d"]["source_mix"], {"hubspot_history": 1, "seeded": 1})
        self.assertIn("seed phase", windows["7d"]["disclosure"])

    def test_disclosure_flags_transitions_bunched_on_one_day(self):
        d = dashboard.movement_disclosure(40, {"hubspot_history": 40}, {"2026-09-21": 39,
                                                                        "2026-09-13": 1})
        self.assertIn("2026-09-21", d)
        self.assertIn("developer test portal", d)

    def test_clean_history_says_so_without_a_warning(self):
        d = dashboard.movement_disclosure(6, {"hubspot_history": 6},
                                          {"2026-09-01": 2, "2026-09-08": 2, "2026-09-15": 2})
        self.assertIn("own property history", d)
        self.assertNotIn("seed phase", d)

    def test_empty_window_is_stated_plainly(self):
        self.assertEqual(dashboard.movement_disclosure(0, {}, {}),
                         "No stage transitions in this window.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
