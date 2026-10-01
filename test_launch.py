import time, unittest
import launch as L

def fake(u):
    if "bad" in u: return {"http_status": 503, "checked_url": u, "findings": [{"check": "Page loads", "pass": False}], "passed": 0, "total": 1}
    if "refuse" in u: raise L.CheckError("Only public internet sites can be checked.")
    return {"http_status": 200, "checked_url": u, "final_url": u, "findings": [{"check": "Title tag", "pass": True}, {"check": "Open Graph tags", "pass": False}], "passed": 1, "total": 2}

def wait(j):
    for _ in range(100):
        if L.JOBS[j]["status"] not in ("running",): return
        time.sleep(0.02)

class T(unittest.TestCase):
    def setUp(self):
        L._run_orig = L._run
        L.check_site_orig = L.check_site
        L.check_site = fake
        L._run.__defaults__ = (fake,)
    def tearDown(self):
        L.check_site = L.check_site_orig
        L._run.__defaults__ = (L.check_site_orig,)
    def mk(self, mode="ask_first", cap=50, ver=None):
        i = L.create_instance("ai-visibility", "t", "owner", budget_cap_cents=cap, approval_mode=mode, method_version=ver)
        L.launch_instance(i["id"], "owner"); return i
    def test_unrunnable_template_blocked(self):
        with self.assertRaises(L.LaunchError): L.create_instance("security-desk", "x", "o")
    def test_scope_minimal(self):
        with self.assertRaises(L.LaunchError): L.create_instance("ai-visibility", "x", "o", scopes=["public_web_read", "gmail_send"])
    def test_must_launch(self):
        i = L.create_instance("ai-visibility", "x", "o")
        with self.assertRaises(L.LaunchError): L.start_job(i["id"], ["a.com"], "mcp", "k1")
    def test_mcp_cannot_bypass_approval(self):
        i = self.mk()
        j = L.start_job(i["id"], ["a.com"], "mcp", "k2")
        self.assertEqual(j["status"], "awaiting_approval"); self.assertEqual(j["spent_cents"], 0)
        L.approve_job(j["id"], "owner"); wait(j["id"])
        self.assertEqual(L.JOBS[j["id"]]["status"], "accepted"); self.assertEqual(L.JOBS[j["id"]]["spent_cents"], 1)
    def test_reject(self):
        i = self.mk(); j = L.start_job(i["id"], ["a.com"], "mcp", "k3")
        self.assertEqual(L.reject_job(j["id"], "owner")["status"], "rejected")
    def test_idempotent(self):
        i = self.mk(); a = L.start_job(i["id"], ["a.com"], "api", "same"); b = L.start_job(i["id"], ["a.com"], "api", "same")
        self.assertEqual(a["id"], b["id"])
    def test_budget_block(self):
        i = self.mk(cap=1); j = L.start_job(i["id"], ["a.com", "b.com"], "api", "k4")
        self.assertEqual(j["status"], "blocked_budget")
    def test_silent_failure_not_success(self):
        i = self.mk("auto_safe"); j = L.start_job(i["id"], ["bad.com"], "api", "k5"); wait(j["id"])
        self.assertEqual(L.JOBS[j["id"]]["status"], "needs_attention"); self.assertIn("silent_failure_suspected", L.JOBS[j["id"]]["flags"])
    def test_refused_url_reported(self):
        i = self.mk("auto_safe"); j = L.start_job(i["id"], ["refuse.com", "a.com"], "api", "k6"); wait(j["id"])
        r = {x["url"]: x for x in L.JOBS[j["id"]]["output"]["results"]}
        self.assertEqual(r["refuse.com"]["status"], "refused"); self.assertEqual(L.JOBS[j["id"]]["status"], "accepted")
    def test_reviewer_catches_overclaim_and_v1_v2(self):
        out = {"results": [{"url": "a.com", "status": "ok", "passed": 1, "evidence": {"http_status": 200, "fetched_at": 1}, "x": "we guarantee ranking"}]}
        self.assertEqual(L.review(out, ["a.com"], 2)["failed"], ["R3"])
        bad = {"results": [{"url": "a.com", "status": "fetch_failed", "passed": 0, "evidence": {"http_status": 503, "fetched_at": 1}}]}
        self.assertEqual(L.review(bad, ["a.com"], 1)["failed"], [])
        self.assertEqual(L.review(bad, ["a.com"], 2)["failed"], ["R4"])
    def test_version_pinning_and_upgrade(self):
        i = self.mk(ver=1); self.assertEqual(i["method_version"], 1)
        j = L.start_job(i["id"], ["a.com"], "api", "k7"); self.assertEqual(j["method_version"], 1)
        L.approve_job(j["id"], "owner"); wait(j["id"])
        self.assertEqual(L.upgrade_instance(i["id"], 2, "owner")["method_version"], 2)
        self.assertEqual(L.INSTANCES[i["id"]]["method_version"], 2)
    def test_metrics_and_audit_chain(self):
        i = self.mk("auto_safe"); j = L.start_job(i["id"], ["a.com"], "api", "k8"); wait(j["id"])
        ev = L.job_events(j["id"]); self.assertGreaterEqual(len(ev), 4)
        self.assertIsNotNone(L.metrics()["cost_per_accepted_job_cents"])
if __name__ == "__main__":
    unittest.main()
