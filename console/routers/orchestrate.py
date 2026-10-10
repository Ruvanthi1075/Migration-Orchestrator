from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from console.common import get_actor, get_env
from console.schemas import (CommentIn, ConfirmPlanRollback, CRCreate, Decision, DryRunRequest, PlanCreate,
                             PlanUpdate, PolicyUpdate, RejectRequest, RollbackRequest, SubmitRequest)
from console.state import (GLOBAL, PLAN_STATUSES, STRATEGIES, Conflict, iso, now, paginate, parse_dt, pct)

plans_router = APIRouter(prefix="/api/v1/plans", tags=["5. Migration Plans"])
cr_router = APIRouter(prefix="/api/v1/change-requests", tags=["6. Change Requests"])
mon_router = APIRouter(prefix="/api/v1/monitoring", tags=["7. Monitoring & Rollback"])


def resolve_budget(env, budget, budget_pct, device_ids=None, strategy=None):
    if budget:
        return budget
    if strategy == "manual":
        if not device_ids:
            raise ValueError("strategy=manual requires device_ids")
        return sum(env.sw[env.resolve(i)]["base_cost"] for i in device_ids) * 1.001
    return env.total_cost() * (budget_pct or GLOBAL.settings["default_budget_pct"]) / 100.0


def plan_brief(env, p):
    return {k: p[k] for k in ("id", "name", "strategy", "alpha", "budget", "status", "created_by", "created_at",
                              "projected_coverage_pct", "change_request_id")} | {
        "switch_count": sum(len(s["switches"]) for s in p["steps"]), "notes": p.get("notes")}


def plan_progress(env, p):
    ex = p.get("execution") or {}
    seq = ex.get("sequence", [])
    done = ex.get("applied", 0)
    res = ex.get("results", [])
    cur = env.sw[seq[done]]["name"] if p["status"] == "Running" and done < len(seq) else None
    return {"plan_id": p["id"], "status": p["status"], "total": len(seq), "completed": done,
            "percent": pct(done / len(seq)) if seq else 0.0, "succeeded": sum(r["outcome"] == "success" for r in res),
            "failed": sum(r["outcome"] == "failed" for r in res), "current_switch": cur,
            "eta_s": max(0, (len(seq) - done) * env.SECONDS_PER_SWITCH) if p["status"] == "Running" else 0,
            "started_at": ex.get("started_at"), "finished_at": ex.get("finished_at"),
            "results": res, "coverage_now_pct": pct(env.cov())}


# ------------------------------ plans ------------------------------
@plans_router.get("/strategies", summary="Strategies + statuses for the wizard")
def strategies(env=Depends(get_env)):
    return {"strategies": [{"id": k, "label": v} for k, v in STRATEGIES.items()], "statuses": PLAN_STATUSES,
            "defaults": {"alpha": GLOBAL.settings["default_alpha"], "budget_pct": GLOBAL.settings["default_budget_pct"]}}


@plans_router.get("/budget-info", summary="Cost units: total migration cost, per-role costs, program budget")
def budget_info(env=Depends(get_env)):
    by_role = {}
    for s in env.sw.values():
        r = by_role.setdefault(s["role"], {"count": 0, "cost": 0.0})
        r["count"] += 1; r["cost"] = round(r["cost"] + s["base_cost"], 3)
    return {"total_cost": round(env.total_cost(), 3), "program_budget": env.budget_total, "by_role": by_role,
            "legacy_remaining_cost": round(sum(env.sw[d]["base_cost"] for d in env.sw if env.state(d) != "Hybrid"), 3),
            "unit": "relative handshake-overhead cost (0.26 - 0.9 per switch)"}


@plans_router.post("/dry-run", summary="Wizard step 3: preview schedule, per-step gain and projected coverage curve")
def adhoc_dry_run(body: DryRunRequest, env=Depends(get_env)):
    b = resolve_budget(env, body.budget, body.budget_pct, body.device_ids, body.strategy)
    return env.dry_run(body.strategy, b, body.alpha, body.device_ids)


@plans_router.get("", summary="Plan list")
def list_plans(status: Optional[str] = None, strategy: Optional[str] = None, created_by: Optional[str] = None,
               q: Optional[str] = None, page: int = 1, page_size: int = 25, env=Depends(get_env)):
    for p in list(env.plans.values()):
        env.advance_plan(p)
    rows = [plan_brief(env, p) for p in env.plans.values()
            if (not status or p["status"] == status) and (not strategy or p["strategy"] == strategy)
            and (not created_by or p["created_by"] == created_by)
            and (not q or q.lower() in f"{p['id']} {p['name']}".lower())]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    res = paginate(rows, page, page_size)
    res["status_counts"] = {s: sum(1 for p in env.plans.values() if p["status"] == s) for s in PLAN_STATUSES}
    return res


@plans_router.post("", status_code=201, summary="Create a Draft plan (runs the dry-run and stores the schedule)")
def create_plan(body: PlanCreate, env=Depends(get_env), actor: str = Depends(get_actor)):
    b = resolve_budget(env, body.budget, body.budget_pct, body.device_ids, body.strategy)
    return env.create_plan(body.name, body.strategy, b, body.alpha, body.device_ids, actor, body.notes)


@plans_router.get("/{plan_id}", summary="Plan detail (steps, linked change request, progress)")
def get_plan(plan_id: str, env=Depends(get_env)):
    p = env.plan(plan_id)
    return {**p, "change_request": env.crs.get(p.get("change_request_id")), "progress": plan_progress(env, p) if p.get("execution") else None}


@plans_router.put("/{plan_id}", summary="Edit a Draft plan (re-runs the dry-run)")
def update_plan(plan_id: str, body: PlanUpdate, env=Depends(get_env), actor: str = Depends(get_actor)):
    p = env.plan(plan_id)
    if p["status"] != "Draft":
        raise Conflict(f"only Draft plans can be edited (this one is '{p['status']}')")
    ch = body.model_dump(exclude_none=True)
    strategy = ch.get("strategy", p["strategy"])
    device_ids = ch.get("device_ids", p["device_ids"])
    if "budget" in ch or "budget_pct" in ch or strategy == "manual":
        budget = resolve_budget(env, ch.get("budget"), ch.get("budget_pct"), device_ids, strategy)
    else:
        budget = p["budget"]
    dr = env.dry_run(strategy, budget, ch.get("alpha", p["alpha"]), device_ids)
    p.update(name=ch.get("name", p["name"]), strategy=strategy, budget=round(budget, 3), alpha=ch.get("alpha", p["alpha"]),
             device_ids=device_ids or [], notes=ch.get("notes", p.get("notes")), steps=dr["steps"],
             projected_coverage_pct=dr["projected_coverage_pct"])
    env.log("plan.updated", actor, "success", plan_id=plan_id, details=ch)
    return p


@plans_router.delete("/{plan_id}", status_code=204, summary="Delete a Draft / Cancelled plan")
def delete_plan(plan_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    p = env.plan(plan_id)
    if p["status"] not in ("Draft", "Cancelled"):
        raise Conflict(f"cannot delete a '{p['status']}' plan")
    del env.plans[plan_id]
    env.log("plan.deleted", actor, "success", plan_id=plan_id)


@plans_router.post("/{plan_id}/dry-run", summary="Re-run the dry-run against the current network state")
def plan_dry_run(plan_id: str, env=Depends(get_env)):
    p = env.plan(plan_id)
    dr = env.dry_run(p["strategy"], p["budget"], p["alpha"], p["device_ids"])
    if p["status"] == "Draft":
        p["steps"], p["projected_coverage_pct"] = dr["steps"], dr["projected_coverage_pct"]
    return dr


@plans_router.post("/{plan_id}/submit", status_code=201, summary="Wizard step 5: submit -> creates a Change Request (Pending approval)")
def submit_plan(plan_id: str, body: SubmitRequest, env=Depends(get_env), actor: str = Depends(get_actor)):
    _check_window(body.maintenance_window.start, body.maintenance_window.end)
    return env.submit_plan(plan_id, actor, body.maintenance_window.model_dump(), body.justification, body.rollback_plan)


@plans_router.post("/{plan_id}/execute", summary="Start execution (needs an Approved change request)")
def execute_plan(plan_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        p = env.plan(plan_id)
        if p["status"] != "Approved":
            raise Conflict(f"plan is '{p['status']}'; it must be 'Approved' to execute")
        cr = env.crs.get(p["change_request_id"])
        if GLOBAL.settings["require_change_request"] and (not cr or cr["status"] != "Approved"):
            raise Conflict("an approved change request is required")
        if any(x["status"] == "Running" for x in env.plans.values()):
            raise Conflict("another plan is already running in this environment")
        seq = [d for st in p["steps"] for d in st["switches"] if env.state(d) != "Hybrid"]
        if not seq:
            raise Conflict("nothing to migrate: all switches in this plan are already Hybrid")
        p["status"] = "Running"
        p["execution"] = {"started_at": iso(now()), "finished_at": None, "executed_by": actor, "sequence": seq,
                          "applied": 0, "results": [], "failed_count": 0}
        env.log("plan.started", actor, "info", plan_id=plan_id, details={"switches": len(seq)})
        env.advance_plan(p)
        return plan_progress(env, p)


@plans_router.get("/{plan_id}/progress", summary="Live progress (poll every 1-2 s): GME success/fail per switch")
def progress(plan_id: str, env=Depends(get_env)):
    p = env.plan(plan_id)
    if not p.get("execution"):
        raise Conflict(f"plan '{plan_id}' has not been executed")
    return plan_progress(env, p)


@plans_router.post("/{plan_id}/cancel", summary="Cancel a plan (stops a running one after the current switch)")
def cancel_plan(plan_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        p = env.plan(plan_id)
        if p["status"] not in ("Draft", "Pending approval", "Approved", "Running"):
            raise Conflict(f"cannot cancel a '{p['status']}' plan")
        if p["status"] == "Running":
            p["execution"]["finished_at"] = iso(now())
        cr = env.crs.get(p.get("change_request_id"))
        if cr and cr["status"] in ("Pending", "Approved"):
            cr["status"] = "Cancelled"
        p["status"] = "Cancelled"
        env.log("plan.cancelled", actor, "info", plan_id=plan_id)
        return plan_brief(env, p)


@plans_router.post("/{plan_id}/rollback", summary="Roll back every switch a Completed plan migrated (typed confirmation)")
def rollback_plan(plan_id: str, body: ConfirmPlanRollback, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        p = env.plan(plan_id)
        if body.confirm_plan_id != plan_id:
            raise HTTPException(422, "confirmation does not match the plan id")
        if p["status"] not in ("Completed", "Failed"):
            raise Conflict(f"only Completed plans can be rolled back (this one is '{p['status']}')")
        reverted = []
        for d in (p.get("execution") or {}).get("sequence", []):
            if env.state(d) == "Hybrid":
                env.revert(d, actor, body.reason, plan_id=plan_id)
                reverted.append(env.sw[d]["name"])
        p["status"] = "Rolled back"
        env.log("plan.rolled_back", actor, "reverted", plan_id=plan_id, reason=body.reason, details={"switches": reverted})
        return {"plan_id": plan_id, "status": p["status"], "reverted": reverted, "coverage_now_pct": pct(env.cov())}


def _check_window(start, end):
    try:
        s, e = parse_dt(start), parse_dt(end)
    except ValueError:
        raise HTTPException(422, "maintenance_window.start/end must be ISO-8601 timestamps")
    if e <= s:
        raise HTTPException(422, "maintenance window end must be after start")


# ------------------------------ change requests ------------------------------
@cr_router.get("", summary="Change request list")
def list_crs(status: Optional[str] = None, risk_level: Optional[str] = None, requester: Optional[str] = None,
             q: Optional[str] = None, page: int = 1, page_size: int = 25, env=Depends(get_env)):
    rows = [c for c in env.crs.values() if (not status or c["status"] == status)
            and (not risk_level or c["risk_level"] == risk_level) and (not requester or c["requester"] == requester)
            and (not q or q.lower() in f"{c['id']} {c['title']}".lower())]
    rows.sort(key=lambda c: c["created_at"], reverse=True)
    res = paginate(rows, page, page_size)
    res["status_counts"] = {s: sum(1 for c in env.crs.values() if c["status"] == s)
                            for s in ("Pending", "Approved", "Rejected", "Implemented", "Cancelled")}
    return res


@cr_router.post("", status_code=201, summary="Create a change request for a Draft plan (same as plan submit)")
def create_cr(body: CRCreate, env=Depends(get_env), actor: str = Depends(get_actor)):
    _check_window(body.maintenance_window.start, body.maintenance_window.end)
    return env.submit_plan(body.plan_id, actor, body.maintenance_window.model_dump(), body.justification, body.rollback_plan)


@cr_router.get("/{cr_id}", summary="Change request detail (+ linked plan summary)")
def get_cr(cr_id: str, env=Depends(get_env)):
    c = env.cr(cr_id)
    p = env.plans.get(c["plan_id"])
    return {**c, "plan": plan_brief(env, p) if p else None}


@cr_router.post("/{cr_id}/approve", summary="Approve (self-approval blocked unless disabled in settings)")
def approve(cr_id: str, body: Decision = Decision(), env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        c = env.cr(cr_id)
        if c["status"] != "Pending":
            raise Conflict(f"change request is '{c['status']}', not Pending")
        if GLOBAL.settings["block_self_approval"] and actor == c["requester"]:
            raise Conflict("self-approval is not allowed: a different person must approve (set X-Actor)")
        c.update(status="Approved", approver=actor, decided_at=iso(now()))
        c["history"].append({"at": iso(now()), "by": actor, "event": "approved", "comment": body.comment})
        p = env.plans.get(c["plan_id"])
        if p and p["status"] == "Pending approval":
            p["status"] = "Approved"
        env.log("change_request.approved", actor, "success", plan_id=c["plan_id"], details={"change_request": cr_id, "comment": body.comment})
        return c


@cr_router.post("/{cr_id}/reject", summary="Reject (plan returns to Draft)")
def reject(cr_id: str, body: RejectRequest, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        c = env.cr(cr_id)
        if c["status"] != "Pending":
            raise Conflict(f"change request is '{c['status']}', not Pending")
        c.update(status="Rejected", approver=actor, decided_at=iso(now()))
        c["history"].append({"at": iso(now()), "by": actor, "event": "rejected", "comment": body.comment})
        p = env.plans.get(c["plan_id"])
        if p and p["status"] == "Pending approval":
            p["status"], p["change_request_id"] = "Draft", None
        env.log("change_request.rejected", actor, "failed", plan_id=c["plan_id"], reason=body.comment, details={"change_request": cr_id})
        return c


@cr_router.post("/{cr_id}/cancel", summary="Withdraw a Pending / Approved change request")
def cancel_cr(cr_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        c = env.cr(cr_id)
        if c["status"] not in ("Pending", "Approved"):
            raise Conflict(f"cannot cancel a '{c['status']}' change request")
        c["status"] = "Cancelled"
        c["history"].append({"at": iso(now()), "by": actor, "event": "cancelled"})
        p = env.plans.get(c["plan_id"])
        if p and p["status"] in ("Pending approval", "Approved"):
            p["status"], p["change_request_id"] = "Draft", None
        env.log("change_request.cancelled", actor, "info", plan_id=c["plan_id"], details={"change_request": cr_id})
        return c


@cr_router.post("/{cr_id}/comments", status_code=201, summary="Add a comment")
def comment(cr_id: str, body: CommentIn, env=Depends(get_env), actor: str = Depends(get_actor)):
    c = env.cr(cr_id)
    item = {"at": iso(now()), "by": actor, "text": body.text}
    c["comments"].append(item)
    return item


# ------------------------------ monitoring & rollback ------------------------------
def _degraded_row(env, d):
    s, pol = env.sw[d], env.policy
    last = env.series(d, 3, 1)["points"][-1]
    nxt = s["next_recheck_at"]
    return {"id": d, "name": s["name"], "site": s["site"], "role": s["role"], "degraded_count": s["degraded_count"],
            "retries_left": max(pol["kappa_retries"] - s["degraded_count"], 0), "kappa": pol["kappa_retries"],
            "degraded_since": iso(s["degraded_since"]), "next_recheck_at": iso(nxt),
            "seconds_until_recheck": max(0, int((nxt - now()).total_seconds())) if nxt else None,
            "latency_ms": last["latency_ms"], "latency_threshold_ms": round(s["baseline"]["latency_ms"] + pol["tau_latency_ms"], 2),
            "failure_rate": last["failure_rate"], "failure_threshold": round(s["baseline"]["failure_rate"] + pol["tau_failure"], 4)}


@mon_router.get("/summary", summary="Header cards for the monitoring page")
def mon_summary(env=Depends(get_env)):
    env.tick()
    hyb = env.hybrid_set()
    return {"monitored": len(hyb), "degraded": sum(env.sw[d]["health"] == "degraded" for d in hyb),
            "stable": sum(env.sw[d]["health"] != "degraded" for d in hyb), "policy": env.policy,
            "rollbacks_total": sum(e["action"] == "migration.reverted" for e in env.audit),
            "auto_rollback_enabled": GLOBAL.settings["auto_rollback_enabled"]}


@mon_router.get("/live", summary="Charts: latency/failure series with baseline and tau thresholds")
def live(switch_ids: Optional[str] = Query(None, description="comma-separated ids/names; default = degraded + 3 healthy Hybrid"),
         minutes: int = Query(60, ge=5, le=1440), step: int = Query(1, ge=1, le=60), env=Depends(get_env)):
    env.tick()
    if switch_ids:
        ids = [env.resolve(x.strip()) for x in switch_ids.split(",") if x.strip()]
    else:
        hyb = sorted(env.hybrid_set(), key=lambda d: env.sw[d]["name"])
        ids = [d for d in hyb if env.sw[d]["health"] == "degraded"] + [d for d in hyb if env.sw[d]["health"] != "degraded"][:3]
    return {"policy": env.policy, "minutes": minutes, "step": step, "series": [env.series(d, minutes, step) for d in ids]}


@mon_router.get("/degraded", summary="Degraded queue with retry counters and recheck countdown")
def degraded(env=Depends(get_env)):
    env.tick()
    rows = [_degraded_row(env, d) for d in env.sw if env.state(d) == "Hybrid" and env.sw[d]["health"] == "degraded"]
    rows.sort(key=lambda r: r["retries_left"])
    return {"items": rows, "total": len(rows), "server_time": iso(now())}


@mon_router.post("/devices/{device_id}/recheck", summary="Force an immediate recheck (may trigger auto-rollback at kappa)")
def recheck(device_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    d = env.resolve(device_id)
    s = env.sw[d]
    if env.state(d) != "Hybrid" or s["health"] != "degraded":
        raise Conflict(f"{s['name']} is not in the degraded queue")
    from datetime import timedelta
    s["next_recheck_at"] = now() - timedelta(seconds=1)
    env.tick()
    env.log("monitor.recheck", actor, "info", d)
    return {"switch": env.switch_view(d), "still_degraded": env.state(d) == "Hybrid" and s["health"] == "degraded"}


@mon_router.post("/devices/{device_id}/rollback", summary="Manual rollback to Legacy (typed switch ID confirmation + reason)")
def manual_rollback(device_id: str, body: RollbackRequest, env=Depends(get_env), actor: str = Depends(get_actor)):
    with env.lock:
        d = env.resolve(device_id)
        s = env.sw[d]
        if body.confirm_switch_id not in (s["name"], d):
            raise HTTPException(422, f"confirmation must equal the switch name '{s['name']}' or its dpid")
        if env.state(d) != "Hybrid":
            raise Conflict(f"{s['name']} is already Legacy")
        env.revert(d, actor, body.reason)
        return {"switch": env.switch_view(d), "coverage_now_pct": pct(env.cov())}


@mon_router.post("/devices/{device_id}/clear", summary="Mark a degraded switch stable again (operator override)")
def clear(device_id: str, env=Depends(get_env), actor: str = Depends(get_actor)):
    d = env.resolve(device_id)
    s = env.sw[d]
    s.update(health="healthy", degraded_count=0, degraded_since=None, next_recheck_at=None)
    env.log("monitor.cleared", actor, "info", d)
    return env.switch_view(d)


@mon_router.post("/devices/{device_id}/simulate-degradation", summary="DEMO helper: push a Hybrid switch into the degraded queue")
def simulate(device_id: str, retries_used: int = Query(0, ge=0, le=20), env=Depends(get_env), actor: str = Depends(get_actor)):
    d = env.resolve(device_id)
    env.degrade(d, retries_used)
    env.log("monitor.simulated_degradation", actor, "info", d)
    return _degraded_row(env, d)


@mon_router.get("/policy", summary="Threshold policy: tau_L, tau_F, kappa, delta_t, delta_c")
def get_policy(env=Depends(get_env)):
    return env.policy


@mon_router.put("/policy", summary="Update threshold policy (partial update)")
def put_policy(body: PolicyUpdate, env=Depends(get_env), actor: str = Depends(get_actor)):
    ch = body.model_dump(exclude_none=True)
    diff = {k: [env.policy[k], v] for k, v in ch.items() if env.policy[k] != v}
    env.policy.update(ch)
    env.log("policy.updated", actor, "success", details=diff)
    return env.policy
