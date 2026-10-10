from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from console.common import csv_response, get_actor, get_env
from console.schemas import BulkExport, BulkPlan, BulkTag, DeviceTags, FlowInputsUpdate
from console.state import SITES, iso, paginate, sort_items

router = APIRouter(prefix="/api/v1", tags=["2-4. Topology, Devices, Flows"])

DEVICE_COLS = ["id", "name", "site", "role", "vendor", "model", "firmware", "mgmt_ip", "state", "health",
               "pqc_capable", "migration_cost", "uptime_s", "degree", "flows_via", "flows_blocked_by_this"]


# ------------------------------ topology ------------------------------
@router.get("/topology", summary="Graph for the topology view (nodes + edges), filterable")
def topology(site: Optional[str] = None, state: Optional[str] = Query(None, description="Legacy|Hybrid"),
             health: Optional[str] = Query(None, description="healthy|degraded|unreachable"),
             role: Optional[str] = None, env=Depends(get_env)):
    env.tick()
    nodes = []
    for d, s in env.sw.items():
        if (site and s["site"] != site) or (state and env.state(d) != state) or \
           (health and s["health"] != health) or (role and s["role"] != role):
            continue
        nodes.append({"id": d, "name": s["name"], "site": s["site"], "role": s["role"], "state": env.state(d),
                      "health": s["health"], "is_core": d == env.core_id, "degree": env.G.degree[d]})
    ids = {n["id"] for n in nodes}
    edges = [{"source": u, "target": v, "kind": a.get("kind"), "utilization": a.get("utilization"),
              "protected": env.state(u) == "Hybrid" and env.state(v) == "Hybrid"}
             for u, v, a in env.G.edges(data=True) if u in ids and v in ids]
    return {"env": env.name, "core_id": env.core_id, "core_name": env.sw[env.core_id]["name"],
            "sites": list(SITES.values()), "nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}


@router.get("/topology/summary", summary="Per-site counts for the filter bar")
def topology_summary(env=Depends(get_env)):
    out = []
    for code, meta in SITES.items():
        ids = [d for d, s in env.sw.items() if s["site"] == code]
        out.append({**meta, "switches": len(ids), "hybrid": sum(env.state(d) == "Hybrid" for d in ids),
                    "degraded": sum(env.sw[d]["health"] == "degraded" for d in ids),
                    "unreachable": sum(env.sw[d]["health"] == "unreachable" for d in ids)})
    return {"sites": out, "core_id": env.core_id}


# ------------------------------ devices ------------------------------
def _filtered_devices(env, site, role, state, health, vendor, pqc_capable, q, sort, order):
    rows = []
    ql = q.lower() if q else None
    for d, s in env.sw.items():
        if (site and s["site"] != site) or (role and s["role"] != role) or (state and env.state(d) != state) \
           or (health and s["health"] != health) or (vendor and s["vendor"].lower() != vendor.lower()) \
           or (pqc_capable is not None and s["pqc_capable"] != pqc_capable):
            continue
        if ql and ql not in f"{d} {s['name']} {s['mgmt_ip']} {s['model']} {s['serial']}".lower():
            continue
        rows.append(env.switch_view(d))
    return sort_items(rows, sort or "name", order)


@router.get("/devices", summary="Inventory table: filter / sort / paginate")
def devices(site: Optional[str] = None, role: Optional[str] = None, state: Optional[str] = None,
            health: Optional[str] = None, vendor: Optional[str] = None, pqc_capable: Optional[bool] = None,
            q: Optional[str] = Query(None, description="matches id, name, IP, model, serial"),
            sort: Optional[str] = None, order: str = Query("asc", pattern="^(asc|desc)$"),
            page: int = 1, page_size: int = 25, env=Depends(get_env)):
    env.tick()
    rows = _filtered_devices(env, site, role, state, health, vendor, pqc_capable, q, sort, order)
    res = paginate(rows, page, page_size)
    res["facets"] = {"sites": list(SITES), "roles": ["core", "agg", "edge"], "states": ["Legacy", "Hybrid"],
                     "health": ["healthy", "degraded", "unreachable"],
                     "vendors": sorted({s["vendor"] for s in env.sw.values()})}
    return res


@router.get("/devices/export", summary="Export the filtered inventory (csv|json)")
def devices_export(format: str = Query("csv", pattern="^(csv|json)$"), site: Optional[str] = None,
                   role: Optional[str] = None, state: Optional[str] = None, health: Optional[str] = None,
                   vendor: Optional[str] = None, pqc_capable: Optional[bool] = None, q: Optional[str] = None,
                   sort: Optional[str] = None, order: str = "asc", env=Depends(get_env)):
    rows = _filtered_devices(env, site, role, state, health, vendor, pqc_capable, q, sort, order)
    if format == "json":
        return {"items": rows, "total": len(rows)}
    return csv_response(rows, DEVICE_COLS, f"qsmo-devices-{env.name}.csv")


@router.post("/devices/bulk/export", summary="Export selected devices")
def bulk_export(body: BulkExport, env=Depends(get_env)):
    rows = [env.switch_view(env.resolve(i)) for i in body.ids]
    if body.format == "json":
        return {"items": rows, "total": len(rows)}
    return csv_response(rows, DEVICE_COLS, f"qsmo-devices-selected-{env.name}.csv")


@router.post("/devices/bulk/tag", summary="Add tags to selected devices")
def bulk_tag(body: BulkTag, env=Depends(get_env), actor: str = Depends(get_actor)):
    out = []
    for i in body.ids:
        d = env.resolve(i)
        env.sw[d]["tags"] = sorted(set(env.sw[d]["tags"]) | set(body.tags))
        out.append({"id": d, "tags": env.sw[d]["tags"]})
    env.log("devices.tagged", actor, "success", details={"count": len(out), "tags": body.tags})
    return {"updated": out}


@router.post("/devices/bulk/plan", status_code=201, summary="Create a Draft plan (strategy=manual) from selected devices")
def bulk_plan(body: BulkPlan, env=Depends(get_env), actor: str = Depends(get_actor)):
    ids = [env.resolve(i) for i in body.ids]
    budget = sum(env.sw[d]["base_cost"] for d in ids) * 1.001
    return env.create_plan(body.name, "manual", budget, 1.0, ids, actor, body.notes)


@router.get("/devices/{device_id}", summary="Device summary tab")
def device(device_id: str, env=Depends(get_env)):
    env.tick()
    return env.switch_view(env.resolve(device_id), detail=True)


@router.patch("/devices/{device_id}", summary="Update device tags")
def device_tags(device_id: str, body: DeviceTags, env=Depends(get_env), actor: str = Depends(get_actor)):
    d = env.resolve(device_id)
    env.sw[d]["tags"] = sorted(set(body.tags))
    env.log("device.tagged", actor, "success", d, details={"tags": env.sw[d]["tags"]})
    return env.switch_view(d, detail=True)


@router.get("/devices/{device_id}/crypto", summary="Crypto tab: cipher suite, KEM, certificate")
def device_crypto(device_id: str, env=Depends(get_env)):
    return env.crypto_view(env.resolve(device_id))


@router.get("/devices/{device_id}/metrics", summary="Metrics tab: latency / failure time series")
def device_metrics(device_id: str, minutes: int = Query(180, ge=5, le=10080), step: int = Query(5, ge=1, le=240),
                   env=Depends(get_env)):
    env.tick()
    d = env.resolve(device_id)
    res = env.series(d, minutes, step)
    res["migrated_at"] = iso(env.sw[d]["migrated_at"]) if env.state(d) == "Hybrid" else None
    return res


@router.get("/devices/{device_id}/flows", summary="Flows tab: flows whose protection path includes this device")
def device_flows(device_id: str, env=Depends(get_env)):
    d = env.resolve(device_id)
    rows = [env.flow_view(f, brief=True) for f in env.flows if d in env.paths[f["name"]]]
    rows.sort(key=lambda r: -r["weight"])
    return {"switch_id": d, "items": rows, "total": len(rows)}


@router.get("/devices/{device_id}/history", summary="History tab: migration / rollback events for the device")
def device_history(device_id: str, page: int = 1, page_size: int = 25, env=Depends(get_env)):
    d = env.resolve(device_id)
    rows = [e for e in reversed(env.audit) if e["switch_id"] == d]
    return paginate(rows, page, page_size)


# ------------------------------ flows ------------------------------
def _flow_rows(env, site, service, protected, q, sort, order):
    hs = env.hybrid_set()
    rows = []
    for f in env.flows:
        prot = set(env.paths[f["name"]]) <= hs
        if (site and f["site"] != site) or (service and f["service"].lower() != service.lower()) \
           or (protected is not None and prot != protected):
            continue
        if q and q.lower() not in f"{f['id']} {f['name']} {f['service']}".lower():
            continue
        rows.append(env.flow_view(f, brief=True))
    return sort_items_default(rows, sort, order)


def sort_items_default(rows, sort, order):
    return sort_items(rows, sort or "weight", order if sort else "desc")


@router.get("/flows", summary="Flows table (weight, path length, blocking switches, status)")
def flows(site: Optional[str] = None, service: Optional[str] = None, protected: Optional[bool] = None,
          q: Optional[str] = None, sort: Optional[str] = None, order: str = Query("desc", pattern="^(asc|desc)$"),
          page: int = 1, page_size: int = 25, env=Depends(get_env)):
    res = paginate(_flow_rows(env, site, service, protected, q, sort, order), page, page_size)
    res["services"] = sorted({f["service"] for f in env.flows})
    return res


@router.get("/flows/summary", summary="Protection summary by service and site")
def flows_summary(env=Depends(get_env)):
    hs = env.hybrid_set()
    def agg(key):
        out = {}
        for f in env.flows:
            b = out.setdefault(f[key], {"flows": 0, "protected": 0, "weight": 0.0, "protected_weight": 0.0})
            prot = set(env.paths[f["name"]]) <= hs
            b["flows"] += 1; b["weight"] += f["weight"]
            if prot:
                b["protected"] += 1; b["protected_weight"] += f["weight"]
        return [{key: k, **{kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()},
                 "coverage_pct": pct_(v["protected_weight"], v["weight"])} for k, v in out.items()]
    return {"by_service": agg("service"), "by_site": agg("site"),
            "total_flows": len(env.flows), "protected": sum(set(env.paths[f["name"]]) <= hs for f in env.flows)}


def pct_(a, b):
    return round(100 * a / b, 2) if b else 0.0


@router.get("/flows/{flow_id}", summary="Flow detail: weight breakdown, path, blocking switches")
def flow(flow_id: str, env=Depends(get_env)):
    f = env.flow_by_id.get(flow_id) or next((x for x in env.flows if x["name"] == flow_id), None)
    if not f:
        raise HTTPException(404, f"flow '{flow_id}' not found")
    return env.flow_view(f)


@router.patch("/flows/{flow_id}", summary="Edit flow inputs (impact levels, bandwidth, delay budget); weights are recomputed")
def flow_update(flow_id: str, body: FlowInputsUpdate, env=Depends(get_env), actor: str = Depends(get_actor)):
    f = env.flow_by_id.get(flow_id)
    if not f:
        raise HTTPException(404, f"flow '{flow_id}' not found")
    changes = body.model_dump(exclude_none=True)
    f.update(changes)
    env.recompute_weights()
    env.log("flow.updated", actor, "success", details={"flow": flow_id, **changes})
    return env.flow_view(env.flow_by_id[flow_id])
