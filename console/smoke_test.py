"""End-to-end smoke test:  python -m console.smoke_test   (no server needed)."""
import sys
import time

from fastapi.testclient import TestClient

from console.main import app

c = TestClient(app)
FAIL = []


def check(name, r, code=200, **expect):
    ok = r.status_code == code
    body = None
    if ok and r.headers.get("content-type", "").startswith("application/json") and r.content:
        body = r.json()
        for k, v in expect.items():
            if body.get(k) != v:
                ok = False
    print(("PASS " if ok else "FAIL ") + f"{r.request.method:6} {r.request.url.path:48} -> {r.status_code}" + ("" if ok else f"  {r.text[:200]}"))
    if not ok:
        FAIL.append(name)
    return body if body is not None else r


A = {"X-Actor": "arun.kumar@qsmo.example"}
ADM = {"X-Actor": "priya.raman@qsmo.example"}

# ---- shell
check("health", c.get("/api/v1/health"))
m = check("meta", c.get("/api/v1/meta"))
check("search", c.get("/api/v1/search", params={"q": "chn-core"}))
check("sync status", c.get("/api/v1/sync/status"))
check("sync", c.post("/api/v1/sync"))
al = check("alerts", c.get("/api/v1/alerts"))
check("alerts summary", c.get("/api/v1/alerts/summary"))
aid = al["items"][0]["id"]
check("ack", c.post(f"/api/v1/alerts/{aid}/acknowledge", headers=A), status="acknowledged")
check("resolve", c.post(f"/api/v1/alerts/{aid}/resolve", headers=A), status="resolved")
check("bulk", c.post("/api/v1/alerts/bulk", params={"action": "acknowledge"}, json=[al["items"][1]["id"]]))
check("bad env", c.get("/api/v1/overview", params={"env": "nope"}), 404)
for e in ("staging", "lab"):
    check(f"overview {e}", c.get("/api/v1/overview", params={"env": e}))

# ---- 1 overview
ov = check("overview", c.get("/api/v1/overview"))
print("   KPIs:", {k: ov["kpis"][k] for k in ("quantum_readiness_score_pct", "hybrid", "legacy", "rollback_rate_pct", "open_alerts")})
check("overview site", c.get("/api/v1/overview", params={"site": "blr"}))
check("trend", c.get("/api/v1/overview/coverage-trend", params={"days": 7}))
check("activity", c.get("/api/v1/overview/recent-activity"))

# ---- 2/3 topology + devices
t = check("topology", c.get("/api/v1/topology"))
check("topology filtered", c.get("/api/v1/topology", params={"site": "hyd", "state": "Legacy"}))
check("topo summary", c.get("/api/v1/topology/summary"))
d = check("devices", c.get("/api/v1/devices", params={"sort": "migration_cost", "order": "desc", "page_size": 10}))
check("devices filter", c.get("/api/v1/devices", params={"site": "chn", "role": "edge", "state": "Hybrid", "q": "edge-0"}))
check("devices export csv", c.get("/api/v1/devices/export", params={"site": "chn"}))
hy = next(x for x in d["items"] if x["state"] == "Hybrid") if any(x["state"] == "Hybrid" for x in d["items"]) else \
    c.get("/api/v1/devices", params={"state": "Hybrid"}).json()["items"][0]
did = hy["id"]
check("device", c.get(f"/api/v1/devices/{hy['name']}"))
check("crypto", c.get(f"/api/v1/devices/{did}/crypto"), quantum_safe_key_exchange=True)
check("metrics", c.get(f"/api/v1/devices/{did}/metrics", params={"minutes": 120, "step": 10}))
check("device flows", c.get(f"/api/v1/devices/{did}/flows"))
check("history", c.get(f"/api/v1/devices/{did}/history"))
check("tag", c.patch(f"/api/v1/devices/{did}", json={"tags": ["pci", "tier-1"]}, headers=A))
check("bulk export", c.post("/api/v1/devices/bulk/export", json={"ids": [did, hy["name"]]}))
check("bulk tag", c.post("/api/v1/devices/bulk/tag", json={"ids": [did], "tags": ["audited"]}, headers=A))
check("device 404", c.get("/api/v1/devices/nope"), 404)

# ---- 4 flows
f = check("flows", c.get("/api/v1/flows", params={"protected": False, "page_size": 5}))
check("flows summary", c.get("/api/v1/flows/summary"))
fid = f["items"][0]["id"]
fd = check("flow", c.get(f"/api/v1/flows/{fid}"))
before = fd["weight"]
fd2 = check("flow patch", c.patch(f"/api/v1/flows/{fid}", json={"impact_confidentiality": "Low", "impact_integrity": "Low"}, headers=A))
print("   weight", before, "->", fd2["weight"])

# ---- 5 plans
check("strategies", c.get("/api/v1/plans/strategies"))
check("budget info", c.get("/api/v1/plans/budget-info"))
dr = check("dry-run", c.post("/api/v1/plans/dry-run", json={"strategy": "sgbm", "budget_pct": 20, "alpha": 1.0}))
print("   dry-run:", dr["start_coverage_pct"], "->", dr["projected_coverage_pct"], "steps", len(dr["steps"]))
check("dry-run bad", c.post("/api/v1/plans/dry-run", json={"strategy": "manual", "budget_pct": 10}), 422)
check("dry-run invalid", c.post("/api/v1/plans/dry-run", json={"strategy": "xx"}), 422)
lp = check("plans", c.get("/api/v1/plans"))
print("   statuses:", {k: v for k, v in lp["status_counts"].items() if v})
p = check("create plan", c.post("/api/v1/plans", json={"name": "Smoke wave", "strategy": "sgbm", "budget_pct": 12}, headers=A), 201)
pid = p["id"]
check("get plan", c.get(f"/api/v1/plans/{pid}"))
check("update plan", c.put(f"/api/v1/plans/{pid}", json={"budget_pct": 10, "name": "Smoke wave v2"}, headers=A))
check("plan dry", c.post(f"/api/v1/plans/{pid}/dry-run"))
check("execute draft", c.post(f"/api/v1/plans/{pid}/execute", headers=A), 409)
check("submit bad window", c.post(f"/api/v1/plans/{pid}/submit", headers=A, json={"maintenance_window": {"start": "2026-10-12T02:00:00Z", "end": "2026-10-12T01:00:00Z"}, "justification": "reduce HNDL risk", "rollback_plan": "BRD auto"}), 422)
cr = check("submit", c.post(f"/api/v1/plans/{pid}/submit", headers=A, json={"maintenance_window": {"start": "2026-10-12T02:00:00Z", "end": "2026-10-12T05:00:00Z"}, "justification": "Reduce harvest-now-decrypt-later exposure", "rollback_plan": "BRD auto rollback"}), 201)
cid = cr["id"]
# ---- 6 change requests
check("crs", c.get("/api/v1/change-requests", params={"status": "Pending"}))
check("cr get", c.get(f"/api/v1/change-requests/{cid}"))
check("cr comment", c.post(f"/api/v1/change-requests/{cid}/comments", json={"text": "Window ok with NOC?"}, headers=A), 201)
check("self approve", c.post(f"/api/v1/change-requests/{cid}/approve", headers=A), 409)
check("execute pending", c.post(f"/api/v1/plans/{pid}/execute", headers=A), 409)
check("approve", c.post(f"/api/v1/change-requests/{cid}/approve", json={"comment": "LGTM"}, headers=ADM), status="Approved")
check("approve again", c.post(f"/api/v1/change-requests/{cid}/approve", headers=ADM), 409)
ex = check("execute", c.post(f"/api/v1/plans/{pid}/execute", headers=A))
print("   started:", ex["total"], "switches; completed", ex["completed"])
check("execute twice", c.post(f"/api/v1/plans/{pid}/execute", headers=A), 409)
for _ in range(60):
    pr = c.get(f"/api/v1/plans/{pid}/progress").json()
    if pr["status"] != "Running":
        break
    time.sleep(1)
check("progress", c.get(f"/api/v1/plans/{pid}/progress"))
print("   final:", pr["status"], pr["succeeded"], "ok", pr["failed"], "failed; coverage", pr["coverage_now_pct"])
check("cr implemented", c.get(f"/api/v1/change-requests/{cid}"), status="Implemented")
# reject + cancel flows on another plan
p2 = c.post("/api/v1/plans", json={"name": "Reject me", "strategy": "random", "budget_pct": 5}, headers=A).json()
c.post(f"/api/v1/plans/{p2['id']}/submit", headers=A, json={"maintenance_window": {"start": "2026-10-13T02:00:00Z", "end": "2026-10-13T04:00:00Z"}, "justification": "test rejection path", "rollback_plan": "n/a ok"})
cr2 = c.get("/api/v1/change-requests", params={"q": "Reject me"}).json()["items"][0]
check("reject", c.post(f"/api/v1/change-requests/{cr2['id']}/reject", json={"comment": "Window clashes with freeze"}, headers=ADM), status="Rejected")
check("plan back to draft", c.get(f"/api/v1/plans/{p2['id']}"), status="Draft")
check("delete draft", c.delete(f"/api/v1/plans/{p2['id']}", headers=A), 204)
check("delete completed", c.delete(f"/api/v1/plans/{pid}", headers=A), 409)
check("cancel", c.post(f"/api/v1/plans/{pid}/cancel", headers=A), 409)
bp = check("bulk plan", c.post("/api/v1/devices/bulk/plan", json={"ids": [x["id"] for x in c.get("/api/v1/devices", params={"state": "Legacy", "role": "edge", "pqc_capable": True}).json()["items"][:3]], "name": "Manual trio"}, headers=A), 201)
print("   manual plan steps:", len(bp["steps"]))
check("plan rollback bad confirm", c.post(f"/api/v1/plans/{pid}/rollback", json={"confirm_plan_id": "x", "reason": "testing rollback"}, headers=A), 422)
rb = check("plan rollback", c.post(f"/api/v1/plans/{pid}/rollback", json={"confirm_plan_id": pid, "reason": "testing rollback path"}, headers=A))
print("   reverted:", len(rb["reverted"]), "coverage now", rb["coverage_now_pct"])

# ---- 7 monitoring
check("mon summary", c.get("/api/v1/monitoring/summary"))
lv = check("mon live", c.get("/api/v1/monitoring/live", params={"minutes": 30}))
print("   series:", [(s["name"], len(s["points"])) for s in lv["series"]])
dg = check("degraded", c.get("/api/v1/monitoring/degraded"))
print("   degraded:", [(x["name"], x["degraded_count"], x["seconds_until_recheck"]) for x in dg["items"]])
victim = dg["items"][0]
check("recheck", c.post(f"/api/v1/monitoring/devices/{victim['id']}/recheck", headers=A))
check("rollback bad confirm", c.post(f"/api/v1/monitoring/devices/{victim['id']}/rollback", json={"confirm_switch_id": "wrong", "reason": "manual test"}, headers=A), 422)
check("manual rollback", c.post(f"/api/v1/monitoring/devices/{victim['name']}/rollback", json={"confirm_switch_id": victim["name"], "reason": "Customer-impacting latency"}, headers=A))
check("rollback twice", c.post(f"/api/v1/monitoring/devices/{victim['name']}/rollback", json={"confirm_switch_id": victim["name"], "reason": "again again"}, headers=A), 409)
h2 = c.get("/api/v1/devices", params={"state": "Hybrid", "health": "healthy"}).json()["items"][0]
check("simulate degradation", c.post(f"/api/v1/monitoring/devices/{h2['id']}/simulate-degradation", params={"retries_used": 3}, headers=A))
r = check("recheck auto-rollback", c.post(f"/api/v1/monitoring/devices/{h2['id']}/recheck", headers=A))
print("   auto-rolled-back:", r["switch"]["state"] == "Legacy", "still_degraded:", r["still_degraded"])
h3 = c.get("/api/v1/devices", params={"state": "Hybrid", "health": "healthy"}).json()["items"][0]
c.post(f"/api/v1/monitoring/devices/{h3['id']}/simulate-degradation", headers=A)
check("clear", c.post(f"/api/v1/monitoring/devices/{h3['id']}/clear", headers=A))
check("policy get", c.get("/api/v1/monitoring/policy"))
check("policy put", c.put("/api/v1/monitoring/policy", json={"tau_latency_ms": 12.5, "kappa_retries": 4}, headers=ADM), tau_latency_ms=12.5, kappa_retries=4)
check("policy invalid", c.put("/api/v1/monitoring/policy", json={"kappa_retries": 0}), 422)

# ---- 8 comparison
tl = check("compare tls", c.get("/api/v1/comparison/tls", params={"hours": 24}))
print("   latency mean legacy/hybrid:", tl["legacy"]["handshake_latency_ms"]["mean"], tl["hybrid"]["handshake_latency_ms"]["mean"], "| p99", tl["legacy"]["handshake_latency_ms"]["p99"], tl["hybrid"]["handshake_latency_ms"]["p99"], "| delta", tl["delta"]["latency_mean"])
check("compare devices", c.get("/api/v1/comparison/devices"))
cs = check("compare strategies", c.get("/api/v1/comparison/strategies", params={"budget_pct": 25}))
print("   strategies:", [(x["strategy"], x["coverage_pct"]) for x in cs["rows"]])
t0 = time.time(); cv = check("coverage curve", c.get("/api/v1/comparison/coverage-curve")); print("   curve secs", round(time.time() - t0, 1), [(s["strategy"], s["coverage_pct"][5]) for s in cv["series"]])

# ---- 9 reports
cp = check("compliance", c.get("/api/v1/reports/compliance"))
print("   ", cp["summary"]["statement"])
for fmt in ("pdf", "csv", "json"):
    r = check(f"export {fmt}", c.get("/api/v1/reports/compliance/export", params={"format": fmt, "site": "chn"}, headers=A))
    print("   bytes", len(r.content) if hasattr(r, "content") else len(str(r)))
assert c.get("/api/v1/reports/compliance/export", params={"format": "pdf"}).content[:4] == b"%PDF"
g = check("generate", c.post("/api/v1/reports/generate", json={"format": "pdf", "title": "Board pack"}, headers=A), 201)
check("download", c.get(g["download_url"]))
check("history", c.get("/api/v1/reports/history"))
check("schedules", c.get("/api/v1/reports/schedules"))
s = check("schedule create", c.post("/api/v1/reports/schedules", json={"name": "Weekly NOC", "frequency": "weekly", "time": "07:30", "recipients": ["noc@qsmo.example"]}, headers=A), 201)
check("schedule update", c.put(f"/api/v1/reports/schedules/{s['id']}", json={"name": "Weekly NOC", "frequency": "daily", "time": "08:00", "recipients": ["noc@qsmo.example"], "enabled": False}))
check("schedule run", c.post(f"/api/v1/reports/schedules/{s['id']}/run", headers=A), 201)
check("schedule bad time", c.post("/api/v1/reports/schedules", json={"name": "bad", "frequency": "daily", "time": "25:99", "recipients": ["a@b.co"]}), 422)
check("schedule delete", c.delete(f"/api/v1/reports/schedules/{s['id']}"), 204)

# ---- 10 audit
au = check("audit", c.get("/api/v1/audit", params={"action": "migration.*", "page_size": 5}))
print("   audit total", au["total"], "rollback rate", au["rollback_rate_pct"])
check("audit filters", c.get("/api/v1/audit", params={"outcome": "reverted", "from": "2026-01-01T00:00:00Z"}))
check("audit facets", c.get("/api/v1/audit/facets"))
check("audit export", c.get("/api/v1/audit/export"))
check("audit event", c.get(f"/api/v1/audit/{au['items'][0]['id']}"))
check("audit verify", c.get("/api/v1/audit/verify"), valid=True)
check("audit immutable POST", c.post("/api/v1/audit", json={}), 405)
check("audit immutable DELETE", c.delete(f"/api/v1/audit/{au['items'][0]['id']}"), 405)

# ---- 11 settings
check("settings", c.get("/api/v1/settings"))
check("settings put", c.put("/api/v1/settings", json={"default_budget_pct": 30}, headers=ADM), default_budget_pct=30)
check("channels", c.get("/api/v1/settings/channels"))
ch = check("channel add", c.post("/api/v1/settings/channels", json={"type": "slack", "name": "#netsec", "target": "https://hooks.slack.com/services/AAA/BBB/CCC123456"}, headers=ADM), 201)
check("channel test", c.post(f"/api/v1/settings/channels/{ch['id']}/test"))
check("channel update", c.put(f"/api/v1/settings/channels/{ch['id']}", json={"type": "slack", "name": "#netsec-2", "target": "https://hooks.slack.com/services/AAA/BBB/CCC123456", "enabled": False}))
check("channel delete", c.delete(f"/api/v1/settings/channels/{ch['id']}"), 204)
k = check("key create", c.post("/api/v1/settings/api-keys", json={"name": "CI exporter", "scopes": ["devices:read"]}, headers=ADM), 201)
assert k["secret"].startswith("qsmo_live_") and "hash" not in k
check("keys", c.get("/api/v1/settings/api-keys"))
check("key revoke", c.delete(f"/api/v1/settings/api-keys/{k['id']}"), revoked=True)
check("sso", c.get("/api/v1/settings/sso"))
check("sso put", c.put("/api/v1/settings/sso", json={"enabled": True, "issuer_url": "https://login.example.com", "client_id": "qsmo", "client_secret": "s3cret"}, headers=ADM), client_secret_set=True)

# ---- 12 users
check("roles", c.get("/api/v1/roles"))
check("me", c.get("/api/v1/me", headers={"X-Actor": "karthik.v@qsmo.example"}), role="Auditor")
check("users", c.get("/api/v1/users", params={"role": "Operator"}))
u = check("user create", c.post("/api/v1/users", json={"name": "Sneha R", "email": "sneha.r@qsmo.example", "role": "Operator", "team": "NOC"}, headers=ADM), 201)
check("user dup", c.post("/api/v1/users", json={"name": "Sneha R", "email": "sneha.r@qsmo.example"}), 409)
check("user bad email", c.post("/api/v1/users", json={"name": "X Y", "email": "nope"}), 422)
check("user get", c.get(f"/api/v1/users/{u['id']}"))
check("user update", c.put(f"/api/v1/users/{u['id']}", json={"role": "Auditor"}, headers=ADM), role="Auditor")
check("user delete", c.delete(f"/api/v1/users/{u['id']}", headers=ADM), 204)
for uid in ("usr-001", "usr-005"):
    r = c.put(f"/api/v1/users/{uid}", json={"status": "disabled"})
print("   last-admin guard ->", r.status_code, r.json().get("detail"))
assert r.status_code == 409
check("audit verify end", c.get("/api/v1/audit/verify"), valid=True)

paths = {p for p in app.openapi()["paths"]}
ops = sum(len(v) for v in app.openapi()["paths"].values())
print(f"\n{len(paths)} paths / {ops} operations; failures: {FAIL or 'none'}")
sys.exit(1 if FAIL else 0)
