from fastapi import APIRouter, Depends, Query

from console.common import get_env
from console.state import GLOBAL, iso, now, pct

router = APIRouter(prefix="/api/v1/overview", tags=["1. Overview"])


def rollback_rate(env):
    ok = sum(1 for e in env.audit if e["action"] == "migration.success")
    rev = sum(1 for e in env.audit if e["action"] == "migration.reverted")
    return (rev / (ok + rev)) if (ok + rev) else 0.0


def coverage_for(env, site=None):
    """Weighted coverage (fraction) restricted to flows anchored in `site`."""
    hs = env.hybrid_set()
    flows = [f for f in env.flows if not site or f["site"] == site]
    tot = sum(f["weight"] for f in flows)
    if not tot:
        return 0.0
    return sum(f["weight"] for f in flows if set(env.paths[f["name"]]) <= hs) / tot


@router.get("", summary="Dashboard payload: KPIs, trend, at-risk flows, activity")
def overview(site: str = Query(None), env=Depends(get_env)):
    env.tick()
    sws = [s for d, s in env.sw.items() if not site or s["site"] == site]
    ids = [s["id"] for s in sws]
    hybrid = [d for d in ids if env.state(d) == "Hybrid"]
    degraded = [d for d in ids if env.sw[d]["health"] == "degraded"]
    consumed = sum(env.sw[d]["base_cost"] for d in hybrid)
    open_alerts = [a for a in env.alerts.values() if a["status"] == "open"]
    sev = {}
    for a in open_alerts:
        sev[a["severity"]] = sev.get(a["severity"], 0) + 1
    flows = [f for f in env.flows if not site or f["site"] == site]
    at_risk = sorted([f for f in flows if set(env.paths[f["name"]]) - env.hybrid_set()], key=lambda f: -f["weight"])[:5]
    ok_pending = [c for c in env.crs.values() if c["status"] == "Pending"]
    return {
        "env": env.name, "site": site, "generated_at": iso(now()), "last_synced": iso(env.last_synced),
        "kpis": {
            "quantum_readiness_score_pct": pct(coverage_for(env, site)),
            "switches_total": len(ids), "hybrid": len(hybrid), "legacy": len(ids) - len(hybrid),
            "hybrid_pct": pct(len(hybrid) / len(ids)) if ids else 0.0,
            "degraded": len(degraded),
            "budget": {"total": env.budget_total, "consumed": round(consumed, 3),
                       "remaining": round(max(env.budget_total - consumed, 0), 3),
                       "consumed_pct": pct(consumed / env.budget_total) if env.budget_total else 0.0},
            "rollback_rate_pct": pct(rollback_rate(env)),
            "open_alerts": len(open_alerts), "open_alerts_by_severity": sev,
            "pending_change_requests": len(ok_pending),
            "flows_protected": sum(1 for f in flows if set(env.paths[f["name"]]) <= env.hybrid_set()),
            "flows_total": len(flows),
        },
        "state_breakdown": [{"state": "Hybrid", "count": len(hybrid) - len([d for d in hybrid if d in degraded])},
                            {"state": "Degraded", "count": len(degraded)},
                            {"state": "Legacy", "count": len(ids) - len(hybrid)}],
        "by_site": [{"site": c, "name": m["name"],
                     "coverage_pct": pct(coverage_for(env, c)),
                     "hybrid": sum(1 for d, s in env.sw.items() if s["site"] == c and env.state(d) == "Hybrid"),
                     "total": sum(1 for s in env.sw.values() if s["site"] == c)} for c, m in __import__("console.state", fromlist=["SITES"]).SITES.items()],
        "coverage_trend_30d": env.trend,
        "top_at_risk_flows": [env.flow_view(f, brief=True) for f in at_risk],
        "recent_activity": list(reversed(env.audit[-10:])),
    }


@router.get("/coverage-trend", summary="Daily weighted coverage for the last N days (max 30)")
def trend(days: int = Query(30, ge=2, le=30), env=Depends(get_env)):
    return {"env": env.name, "points": env.trend[-(days + 1):]}


@router.get("/recent-activity", summary="Latest audit events")
def recent(limit: int = Query(10, ge=1, le=100), env=Depends(get_env)):
    return {"items": list(reversed(env.audit[-limit:]))}
