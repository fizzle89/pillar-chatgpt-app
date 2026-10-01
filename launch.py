"""Pillar launch layer: template -> instance -> launch -> job, with approvals, budgets,
independent rubric review, versioned methods and silent-failure surfacing.
In-memory (resets on restart). The executor is the real public-site checker; costs are metered
internal units, not money."""
import hashlib, json, re, threading, time, uuid
from checker import check_site, CheckError

class LaunchError(Exception):
    pass

LOCK = threading.RLock()
def nid(p): return f"{p}_{uuid.uuid4().hex[:10]}"
def now(): return round(time.time(), 3)

# ---- versioned methods (immutable once published) ----
RUBRIC = {
    "R1": "Every requested URL has a result entry.",
    "R2": "Every result carries evidence: HTTP status and fetch time.",
    "R3": "No result promises rankings, citations or AI mentions.",
    "R4": "Failed fetches are reported as failed, never scored as passes.",
}
METHODS = {}   # slug -> [version dicts]
def publish_method(slug, title, rubric_ids, notes, cents_per_unit=1, max_units=5):
    with LOCK:
        for r in rubric_ids:
            if r not in RUBRIC: raise LaunchError(f"unknown rubric id {r}")
        vs = METHODS.setdefault(slug, [])
        m = {"slug": slug, "version": len(vs) + 1, "title": title, "rubric": list(rubric_ids),
             "notes": notes, "cents_per_unit": cents_per_unit, "max_units": max_units, "published_at": now()}
        vs.append(m)
        return dict(m)
def method(slug, version=None):
    vs = METHODS.get(slug)
    if not vs: raise LaunchError("unknown method")
    return vs[-1] if version is None else vs[version - 1]

TEMPLATES = {
    "ai-visibility": {
        "slug": "ai-visibility", "name": "AI visibility readiness",
        "method_slug": "ai-visibility-readiness",
        "allowed_scopes": ["public_web_read"],
        "runnable": True,
        "note": "Executor runs the real checker on public pages. Read-only.",
    },
    "real-estate-intake": {"slug": "real-estate-intake", "name": "Real estate intake (Strata)", "method_slug": None,
        "allowed_scopes": ["intake_form_read"], "runnable": False, "note": "No executor wired yet; cannot be launched."},
    "security-desk": {"slug": "security-desk", "name": "Security Desk pilot", "method_slug": None,
        "allowed_scopes": ["scoped_log_read"], "runnable": False, "note": "Pilot scope only; no executor wired."},
}
publish_method("ai-visibility-readiness", "AI visibility readiness check", ["R1", "R2", "R3"], "v1 baseline")
publish_method("ai-visibility-readiness", "AI visibility readiness check", ["R1", "R2", "R3", "R4"], "v2 adds failed-fetch honesty")

INSTANCES, JOBS, IDEM = {}, {}, {}
JOB_TIMEOUT_S = 300

def _event(job, kind, actor, detail=None):
    prev = job["events"][-1]["hash"] if job["events"] else "0" * 16
    e = {"at": now(), "kind": kind, "actor": actor, "detail": detail or {}}
    e["hash"] = hashlib.sha256((prev + json.dumps({k: e[k] for k in ("at", "kind", "actor", "detail")}, sort_keys=True)).encode()).hexdigest()[:16]
    job["events"].append(e)

def list_templates():
    out = []
    for t in TEMPLATES.values():
        d = dict(t)
        if t["method_slug"]:
            d["latest_method_version"] = method(t["method_slug"])["version"]
            d["rubric"] = {r: RUBRIC[r] for r in method(t["method_slug"])["rubric"]}
        out.append(d)
    return out

def create_instance(template_slug, name, owner, scopes=None, budget_cap_cents=50, approval_mode="ask_first", method_version=None):
    with LOCK:
        t = TEMPLATES.get(template_slug)
        if not t: raise LaunchError("unknown template")
        if not t["runnable"]: raise LaunchError("template has no executor yet; cannot create a launchable agent")
        scopes = scopes if scopes is not None else list(t["allowed_scopes"])
        extra = [s for s in scopes if s not in t["allowed_scopes"]]
        if extra: raise LaunchError(f"scopes not allowed for this template: {extra}")
        if approval_mode not in ("ask_first", "auto_safe"): raise LaunchError("approval_mode must be ask_first or auto_safe")
        if not (1 <= int(budget_cap_cents) <= 500): raise LaunchError("budget cap must be 1-500 metered cents")
        m = method(t["method_slug"], method_version)
        i = {"id": nid("agt"), "template": template_slug, "name": name[:80], "owner": owner, "scopes": scopes,
             "budget_cap_cents": int(budget_cap_cents), "approval_mode": approval_mode,
             "method_slug": m["slug"], "method_version": m["version"], "status": "draft", "created_at": now()}
        INSTANCES[i["id"]] = i
        return dict(i)

def launch_instance(instance_id, approver):
    with LOCK:
        i = INSTANCES.get(instance_id)
        if not i: raise LaunchError("unknown instance")
        if i["status"] == "launched": return dict(i)
        i["status"], i["launched_by"], i["launched_at"] = "launched", approver, now()
        return dict(i)

def upgrade_instance(instance_id, version, approver):
    with LOCK:
        i = INSTANCES.get(instance_id)
        if not i: raise LaunchError("unknown instance")
        method(i["method_slug"], version)
        i["method_version"], i["upgraded_by"] = int(version), approver
        return dict(i)

def start_job(instance_id, urls, channel, idempotency_key):
    if not idempotency_key: raise LaunchError("idempotency_key required")
    with LOCK:
        key = (instance_id, idempotency_key)
        if key in IDEM: return view_job(IDEM[key])
        i = INSTANCES.get(instance_id)
        if not i: raise LaunchError("unknown instance")
        if i["status"] != "launched": raise LaunchError("agent is not launched; the owner must launch it first")
        m = method(i["method_slug"], i["method_version"])
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls: raise LaunchError("provide at least one URL")
        if len(urls) > m["max_units"]: raise LaunchError(f"at most {m['max_units']} URLs per job")
        est = len(urls) * m["cents_per_unit"]
        job = {"id": nid("job"), "instance_id": instance_id, "channel": channel, "input": {"urls": urls},
               "method_version": m["version"], "estimated_cents": est, "spent_cents": 0, "output": None,
               "review": None, "events": [], "created_at": now(), "updated_at": now(), "flags": []}
        JOBS[job["id"]] = job; IDEM[key] = job["id"]
        _event(job, "created", channel, {"urls": urls, "estimated_cents": est})
        if est > i["budget_cap_cents"]:
            job["status"] = "blocked_budget"; _event(job, "blocked", "policy", {"reason": "estimate exceeds budget cap", "cap": i["budget_cap_cents"]})
        elif i["approval_mode"] == "ask_first":
            # same gate on every channel, MCP included
            job["status"] = "awaiting_approval"; _event(job, "approval_requested", "policy", {"plan": f"fetch {len(urls)} public page(s) + robots.txt + llms.txt", "scopes": i["scopes"]})
        else:
            _begin(job)
        return view_job(job["id"])

def approve_job(job_id, approver, note=""):
    with LOCK:
        job = JOBS.get(job_id)
        if not job: raise LaunchError("unknown job")
        if job["status"] != "awaiting_approval": raise LaunchError(f"job is {job['status']}, not awaiting approval")
        _event(job, "approved", approver, {"note": note[:200]})
        _begin(job)
        return view_job(job_id)

def reject_job(job_id, approver, note=""):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "awaiting_approval": raise LaunchError("job is not awaiting approval")
        job["status"] = "rejected"; _event(job, "rejected", approver, {"note": note[:200]})
        return view_job(job_id)

def _begin(job):
    job["status"] = "running"; job["started_at"] = now(); _event(job, "started", "executor")
    threading.Thread(target=_run, args=(job["id"],), daemon=True).start()

def _run(job_id, check=check_site):
    job = JOBS[job_id]; inst = INSTANCES[job["instance_id"]]; cap = inst["budget_cap_cents"]
    m = method(inst["method_slug"], job["method_version"])
    results = []
    try:
        for u in job["input"]["urls"]:
            with LOCK:
                if job["spent_cents"] + m["cents_per_unit"] > cap:
                    job["flags"].append("budget_stop"); _event(job, "budget_stop", "policy", {"at_url": u}); break
            fetched = now()
            try:
                r = check(u)
                res = {"url": u, "status": "ok" if r.get("http_status") == 200 else "fetch_failed", "http_status": r.get("http_status"),
                       "fetched_at": fetched, "passed": r.get("passed") if r.get("http_status") == 200 else None,
                       "total": r.get("total") if r.get("http_status") == 200 else None,
                       "fixes": [f["check"] for f in r["findings"] if not f["pass"]][:12], "evidence": {"checked_url": r.get("final_url") or r.get("checked_url"), "http_status": r.get("http_status"), "fetched_at": fetched}}
            except CheckError as e:
                res = {"url": u, "status": "refused", "error": str(e), "http_status": None, "fetched_at": fetched, "passed": None, "total": None, "fixes": [], "evidence": {"http_status": None, "fetched_at": fetched}}
            with LOCK:
                job["spent_cents"] += m["cents_per_unit"]
            results.append(res)
        with LOCK:
            job["output"] = {"results": results, "note": "Readiness checklist for public pages. Not rankings; no AI mention data."}
            _finish(job, m)
    except Exception as e:  # surfaced, never swallowed
        with LOCK:
            job["status"] = "failed"; job["flags"].append("executor_exception"); _event(job, "failed", "executor", {"error": type(e).__name__})

def review(output, urls, version):
    """Independent reviewer: sees only input + output + rubric, not executor internals."""
    failed = []
    res = (output or {}).get("results", [])
    if "R1" in _rubric_for(version) and {r["url"] for r in res} != set(urls): failed.append("R1")
    if "R2" in _rubric_for(version) and any(not (r["evidence"].get("fetched_at") and "http_status" in r["evidence"]) for r in res): failed.append("R2")
    text = json.dumps(output or {}).lower()
    if "R3" in _rubric_for(version) and re.search(r"guarantee|will rank|#1 |top ranking|ensure.*cited", text): failed.append("R3")
    if "R4" in _rubric_for(version) and any(r["status"] != "ok" and r.get("passed") is not None for r in res): failed.append("R4")
    return {"verdict": "accepted" if not failed else "needs_changes", "failed": failed, "checked": _rubric_for(version)}
def _rubric_for(version): return METHODS["ai-visibility-readiness"][version - 1]["rubric"]

def _finish(job, m):
    res = job["output"]["results"]
    # silent-failure surfacing: a "done" job with no usable evidence is flagged, never reported as success
    usable = [r for r in res if r["status"] == "ok"]
    if not res or not usable:
        job["flags"].append("silent_failure_suspected")
        _event(job, "flag", "monitor", {"reason": "no usable evidence in output"})
    if "budget_stop" in job["flags"] and len(res) < len(job["input"]["urls"]):
        job["flags"].append("partial_output")
    rv = review(job["output"], job["input"]["urls"], job["method_version"])
    job["review"] = rv; _event(job, "reviewed", "reviewer", rv)
    if "silent_failure_suspected" in job["flags"]:
        job["status"] = "needs_attention"
    elif rv["verdict"] == "accepted" and "partial_output" not in job["flags"]:
        job["status"] = "accepted"
    else:
        job["status"] = "needs_changes"
    job["updated_at"] = now(); _event(job, "finished", "executor", {"status": job["status"], "spent_cents": job["spent_cents"]})

def sweep():
    with LOCK:
        for j in JOBS.values():
            if j["status"] == "running" and now() - j.get("started_at", now()) > JOB_TIMEOUT_S:
                j["status"] = "needs_attention"; j["flags"].append("stalled"); _event(j, "flag", "monitor", {"reason": "running past timeout"})

def view_job(job_id):
    j = JOBS.get(job_id)
    if not j: raise LaunchError("unknown job")
    return {k: v for k, v in j.items() if k != "events"} | {"event_count": len(j["events"]), "last_event": j["events"][-1]["kind"] if j["events"] else None}

def job_events(job_id):
    j = JOBS.get(job_id)
    if not j: raise LaunchError("unknown job")
    return j["events"]

def list_jobs(instance_id=None):
    sweep()
    return [view_job(j["id"]) for j in JOBS.values() if not instance_id or j["instance_id"] == instance_id]

def metrics():
    sweep()
    st = {}
    for j in JOBS.values(): st[j["status"]] = st.get(j["status"], 0) + 1
    spent = sum(j["spent_cents"] for j in JOBS.values())
    acc = st.get("accepted", 0)
    return {"jobs_by_status": st, "metered_cents_spent": spent, "accepted_jobs": acc,
            "cost_per_accepted_job_cents": round(spent / acc, 2) if acc else None,
            "silent_failure_flags": sum(1 for j in JOBS.values() if "silent_failure_suspected" in j["flags"]),
            "note": "Metered units of the checker, not payments."}
