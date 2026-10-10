"""
QSMO Console API  -  run from the repo root:

    pip install -r console/requirements.txt
    uvicorn console.main:app --reload --port 8000        # docs at http://localhost:8000/docs

No authentication. Identity for the audit trail comes from the optional `X-Actor` header.
Every data endpoint takes `?env=prod|staging|lab` (default prod).
"""
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from console.common import get_actor, get_env
from console.routers import admin, insights, network, orchestrate, overview
from console.state import (ENV_CONFIG, GLOBAL, PLAN_STATUSES, ROLE_PERMISSIONS, SITES, STRATEGIES, Conflict, NotFound,
                           env_store, iso, now, paginate)

app = FastAPI(title="QSMO Console API", version="1.0.0",
              description="Quantum-Safe Control Plane Manager - REST API (no auth).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(NotFound)
async def _nf(_: Request, e: NotFound):
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(Conflict)
async def _cf(_: Request, e: Conflict):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(ValueError)
async def _ve(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=422)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


# ------------------------------ app shell ------------------------------
shell = "0. App shell"


@app.get("/api/v1/health", tags=[shell], summary="Liveness")
def health():
    return {"status": "ok", "time": iso(now())}


@app.get("/api/v1/meta", tags=[shell], summary="Environment / site switchers, enums, defaults")
def meta():
    envs = []
    for k, c in ENV_CONFIG.items():
        e = env_store(k)
        envs.append({"id": k, "label": c["label"], "switches": len(e.sw), "last_synced": iso(e.last_synced),
                     "open_alerts": sum(a["status"] == "open" for a in e.alerts.values())})
    return {"environments": envs, "default_env": "prod", "sites": list(SITES.values()), "strategies": STRATEGIES,
            "plan_statuses": PLAN_STATUSES, "roles": list(ROLE_PERMISSIONS), "server_time": iso(now()),
            "version": "1.0.0", "product": "QSMO Console - Quantum-Safe Control Plane Manager"}


@app.get("/api/v1/search", tags=[shell], summary="Global search: switches, flows, plans, change requests")
def search(q: str = Query(..., min_length=2), limit: int = Query(6, ge=1, le=25), env=Depends(get_env)):
    ql = q.lower()
    sw = [env.switch_view(d) for d, s in env.sw.items() if ql in f"{d} {s['name']} {s['mgmt_ip']} {s['serial']}".lower()][:limit]
    fl = [env.flow_view(f, brief=True) for f in env.flows if ql in f"{f['id']} {f['name']} {f['service']}".lower()][:limit]
    pl = [{"id": p["id"], "name": p["name"], "status": p["status"]} for p in env.plans.values() if ql in f"{p['id']} {p['name']}".lower()][:limit]
    cr = [{"id": c["id"], "title": c["title"], "status": c["status"]} for c in env.crs.values() if ql in f"{c['id']} {c['title']}".lower()][:limit]
    return {"query": q, "switches": sw, "flows": fl, "plans": pl, "change_requests": cr,
            "total": len(sw) + len(fl) + len(pl) + len(cr)}


@app.get("/api/v1/sync/status", tags=[shell], summary="'Last synced' indicator")
def sync_status(env=Depends(get_env)):
    age = (now() - env.last_synced).total_seconds()
    return {"env": env.name, "last_synced": iso(env.last_synced), "age_s": int(age),
            "stale": age > 300, "switches": len(env.sw), "links": env.G.number_of_edges()}


@app.post("/api/v1/sync", tags=[shell], summary="Refresh from the controller (LLDP re-discovery) - simulated here")
def sync(env=Depends(get_env), actor: str = Depends(get_actor)):
    env.tick()
    env.last_synced = now()
    env.log("topology.synced", actor, "info", details={"switches": len(env.sw), "links": env.G.number_of_edges()})
    return sync_status(env)


# ------------------------------ alerts (bell) ------------------------------
@app.get("/api/v1/alerts", tags=[shell], summary="Alerts list")
def alerts(severity: Optional[str] = None, status: Optional[str] = Query(None, description="open|acknowledged|resolved"),
           source: Optional[str] = None, page: int = 1, page_size: int = 25, env=Depends(get_env)):
    env.tick()
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    rows = [a for a in env.alerts.values() if (not severity or a["severity"] == severity)
            and (not status or a["status"] == status) and (not source or a["source"] == source)]
    rows.sort(key=lambda a: (order[a["severity"]], a["created_at"]), reverse=False)
    rows.sort(key=lambda a: a["created_at"], reverse=True)
    rows.sort(key=lambda a: order[a["severity"]])
    return paginate(rows, page, page_size)


@app.get("/api/v1/alerts/summary", tags=[shell], summary="Bell badge counts")
def alerts_summary(env=Depends(get_env)):
    env.tick()
    open_ = [a for a in env.alerts.values() if a["status"] == "open"]
    by = {}
    for a in open_:
        by[a["severity"]] = by.get(a["severity"], 0) + 1
    return {"open": len(open_), "acknowledged": sum(a["status"] == "acknowledged" for a in env.alerts.values()), "by_severity": by}


def _alert(env, aid):
    if aid not in env.alerts:
        raise NotFound(f"alert '{aid}' not found")
    return env.alerts[aid]


@app.post("/api/v1/alerts/{alert_id}/acknowledge", tags=[shell], summary="Acknowledge an alert")
def ack(alert_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    a = _alert(env, alert_id)
    if a["status"] == "resolved":
        raise Conflict("alert is already resolved")
    a.update(status="acknowledged", acknowledged_by=actor, acknowledged_at=iso(now()))
    return a


@app.post("/api/v1/alerts/{alert_id}/resolve", tags=[shell], summary="Resolve an alert")
def resolve(alert_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    a = _alert(env, alert_id)
    a.update(status="resolved", resolved_by=actor, resolved_at=iso(now()))
    return a


@app.post("/api/v1/alerts/bulk", tags=[shell], summary="Bulk acknowledge / resolve")
def alerts_bulk(ids: list[str], action: str = Query(..., pattern="^(acknowledge|resolve)$"),
                env=Depends(get_env), actor: str = Depends(get_actor)):
    out = []
    for i in ids:
        a = _alert(env, i)
        if action == "acknowledge" and a["status"] != "resolved":
            a.update(status="acknowledged", acknowledged_by=actor, acknowledged_at=iso(now()))
        elif action == "resolve":
            a.update(status="resolved", resolved_by=actor, resolved_at=iso(now()))
        out.append(a)
    return {"updated": out}


for r in (overview.router, network.router, orchestrate.plans_router, orchestrate.cr_router, orchestrate.mon_router,
          insights.cmp_router, insights.rep_router, admin.audit_router, admin.set_router, admin.user_router):
    app.include_router(r)
