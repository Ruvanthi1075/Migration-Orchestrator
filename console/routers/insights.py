import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from console.common import csv_response, get_actor, get_env
from console.routers.overview import coverage_for, rollback_rate
from console.schemas import ReportRequest, ScheduleIn
from console.state import (GLOBAL, HYBRID_CPU_MS, HYBRID_HS_BYTES, HYBRID_LAT_MS, LEGACY_CPU_MS, LEGACY_HS_BYTES,
                           LEGACY_LAT_MS, SITES, STRATEGIES, env_store, iso, now, paginate, pct, percentile, sort_items)
import random as _r

cmp_router = APIRouter(prefix="/api/v1/comparison", tags=["8. Comparison"])
rep_router = APIRouter(prefix="/api/v1/reports", tags=["9. Reports"])


# ------------------------------ comparison ------------------------------
def _stats(vals, nd=3):
    if not vals:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None}
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), nd), "p50": percentile(vals, 50),
            "p95": percentile(vals, 95), "p99": percentile(vals, 99)}


def _hist(vals, lo=8, hi=44, w=2):
    bins = [{"from": b, "to": b + w, "count": 0} for b in range(lo, hi, w)]
    for v in vals:
        for b in bins:
            if b["from"] <= v < b["to"]:
                b["count"] += 1
                break
    return bins


@cmp_router.get("/tls", summary="Legacy vs Hybrid: latency, failure rate, handshake size, CPU (mean + p50/p95/p99)")
def tls(hours: int = Query(24, ge=1, le=168), site: Optional[str] = None, env=Depends(get_env)):
    step = 15 if hours <= 48 else 60
    groups = {"legacy": [], "hybrid": []}
    for d, s in env.sw.items():
        if (site and s["site"] != site) or s["health"] == "unreachable":
            continue
        groups["hybrid" if env.state(d) == "Hybrid" else "legacy"].append(d)
    out = {}
    for g, ids in groups.items():
        lat, fr = [], []
        for d in ids:
            pts = env.series(d, hours * 60, step)["points"]
            lat += [p["latency_ms"] for p in pts]
            fr += [p["failure_rate"] for p in pts]
        hs = [(env.sw[d]["post"] or env.sw[d]["baseline"])["overhead_bytes"] if g == "hybrid" else env.sw[d]["baseline"]["overhead_bytes"] for d in ids]
        cpu = [_r.Random(f"cpu:{g}:{d}").gauss(HYBRID_CPU_MS if g == "hybrid" else LEGACY_CPU_MS, 0.1) for d in ids]
        out[g] = {"switches": len(ids), "handshake_latency_ms": _stats(lat, 2), "failure_rate": _stats(fr, 4),
                  "handshake_bytes": {"mean": round(sum(hs) / len(hs)) if hs else None,
                                      "min": min(hs) if hs else None, "max": max(hs) if hs else None},
                  "cpu_ms_per_handshake": {"mean": round(sum(cpu) / len(cpu), 3) if cpu else None},
                  "latency_histogram": _hist(lat)}
    L, H = out["legacy"], out["hybrid"]
    def delta(a, b):
        return None if a in (None, 0) or b is None else {"abs": round(b - a, 3), "pct": round(100 * (b - a) / a, 1)}
    out["delta"] = {
        "latency_mean": delta(L["handshake_latency_ms"]["mean"], H["handshake_latency_ms"]["mean"]),
        "latency_p95": delta(L["handshake_latency_ms"]["p95"], H["handshake_latency_ms"]["p95"]),
        "latency_p99": delta(L["handshake_latency_ms"]["p99"], H["handshake_latency_ms"]["p99"]),
        "failure_rate_mean": delta(L["failure_rate"]["mean"], H["failure_rate"]["mean"]),
        "handshake_bytes": delta(L["handshake_bytes"]["mean"], H["handshake_bytes"]["mean"]),
        "cpu": delta(L["cpu_ms_per_handshake"]["mean"], H["cpu_ms_per_handshake"]["mean"])}
    out["reference"] = {"paper_legacy_latency_ms": LEGACY_LAT_MS, "paper_hybrid_latency_ms": HYBRID_LAT_MS,
                        "nominal_handshake_bytes": {"legacy": LEGACY_HS_BYTES, "hybrid": HYBRID_HS_BYTES}}
    out.update(env=env.name, site=site, window_hours=hours, bucket_minutes=step)
    return out


@cmp_router.get("/devices", summary="Per-device before/after deltas (Hybrid switches)")
def devices(site: Optional[str] = None, sort: str = "latency_delta_pct", order: str = "desc",
            page: int = 1, page_size: int = 25, env=Depends(get_env)):
    rows = []
    for d in env.hybrid_set():
        s = env.sw[d]
        if site and s["site"] != site:
            continue
        b, p = s["baseline"], s["post"] or s["baseline"]
        rows.append({"id": d, "name": s["name"], "site": s["site"], "role": s["role"], "migrated_at": iso(s["migrated_at"]),
                     "baseline_latency_ms": b["latency_ms"], "post_latency_ms": p["latency_ms"],
                     "latency_delta_ms": round(p["latency_ms"] - b["latency_ms"], 2),
                     "latency_delta_pct": round(100 * (p["latency_ms"] - b["latency_ms"]) / b["latency_ms"], 1),
                     "baseline_failure_rate": b["failure_rate"], "post_failure_rate": p["failure_rate"],
                     "baseline_bytes": b["overhead_bytes"], "post_bytes": p["overhead_bytes"],
                     "bytes_delta": p["overhead_bytes"] - b["overhead_bytes"], "health": s["health"]})
    return paginate(sort_items(rows, sort, order), page, page_size)


def _strategy_row(env, strategy, budget, alpha):
    dr = env.dry_run(strategy, budget, alpha) if strategy != "manual" else None
    return {"strategy": strategy, "label": STRATEGIES[strategy], "switches_migrated": dr["switch_count"],
            "budget_spent": dr["budget_spent"], "coverage_pct": dr["projected_coverage_pct"],
            "coverage_per_cost": round(dr["projected_coverage_pct"] / dr["budget_spent"], 2) if dr["budget_spent"] else 0.0,
            "predicted_failures": dr["predicted_failures"], "steps": len(dr["steps"])}


@cmp_router.get("/strategies", summary="Table-I style comparison of all strategies at one budget")
def strategies(budget_pct: float = Query(25, gt=0, le=100), alpha: float = Query(1.0, gt=0, le=3), env=Depends(get_env)):
    budget = env.total_cost() * budget_pct / 100
    rows = [_strategy_row(env, s, budget, alpha) for s in ("sgbm", "simple_greedy", "random", "sequential")]
    return {"env": env.name, "budget": round(budget, 3), "budget_pct": budget_pct, "alpha": alpha,
            "start_coverage_pct": pct(env.cov()), "rows": rows}


@cmp_router.get("/coverage-curve", summary="Coverage vs budget curves for every strategy (Fig.-2 style)")
def curve(points: int = Query(10, ge=3, le=40), alpha: float = Query(1.0, gt=0, le=3), env=Depends(get_env)):
    key = ("curve", frozenset(env.hybrid_set()), points, alpha)
    if key in env._cache:
        return env._cache[key]
    remaining = sum(env.sw[d]["base_cost"] for d in env.sw if env.state(d) != "Hybrid")
    budgets = [round(remaining * i / points, 3) for i in range(points + 1)]
    start = pct(env.cov())

    def prefix(steps):
        out, cum, cov = [], 0.0, start
        seq = [(0.0, start)] + [((cum := cum + s["cost"]), s["coverage_after_pct"]) for s in steps]
        res = []
        for b in budgets:
            c = start
            for cost, cv in seq:
                if cost <= b + 1e-9:
                    c = cv
            res.append(c)
        return res

    series = {"sgbm": prefix(env.sgbm_steps(env.G.copy(), remaining + 1, alpha)),
              "simple_greedy": prefix(env.baseline_steps("simple_greedy", remaining + 1)),
              "sequential": prefix(env.baseline_steps("sequential", remaining + 1))}
    rnd = [prefix(env.baseline_steps("random", remaining + 1, rng=_r.Random(i))) for i in range(8)]
    series["random"] = [round(sum(col) / len(col), 2) for col in zip(*rnd)]
    res = {"env": env.name, "alpha": alpha, "budgets": budgets, "series": [
        {"strategy": k, "label": STRATEGIES[k], "coverage_pct": v} for k, v in series.items()],
        "note": "Each strategy is simulated once with the full remaining budget and read off at every budget (random = mean of 8 seeds)."}
    env._cache[key] = res
    return res


# ------------------------------ reports ------------------------------
def build_compliance(env, site=None):
    hs = env.hybrid_set()
    flows = [f for f in env.flows if not site or f["site"] == site]
    prot = [f for f in flows if set(env.paths[f["name"]]) <= hs]
    tot_bw = sum(f["bandwidth_mbps"] for f in flows) or 1
    safe_bw = sum(f["bandwidth_mbps"] for f in prot)
    sws = [s for s in env.sw.values() if not site or s["site"] == site]
    blockers = [s for s in sws if not s["pqc_capable"] and env.state(s["id"]) != "Hybrid"]

    def group(key):
        out = []
        for k in sorted({f[key] for f in flows}):
            fs = [f for f in flows if f[key] == k]
            p = [f for f in fs if set(env.paths[f["name"]]) <= hs]
            out.append({key: k, "flows": len(fs), "protected": len(p),
                        "quantum_safe_traffic_pct": pct(sum(f["bandwidth_mbps"] for f in p) / (sum(f["bandwidth_mbps"] for f in fs) or 1)),
                        "weighted_coverage_pct": pct(sum(f["weight"] for f in p) / (sum(f["weight"] for f in fs) or 1))})
        return out
    recs = []
    if blockers:
        recs.append(f"Upgrade firmware on {len(blockers)} device(s) that cannot negotiate ML-KEM/ML-DSA: " + ", ".join(s["name"] for s in blockers[:6]) + ("..." if len(blockers) > 6 else ""))
    top = sorted([f for f in flows if f not in prot], key=lambda f: -f["weight"])[:3]
    if top:
        recs.append("Prioritise the highest-weight unprotected flows: " + ", ".join(f["name"] for f in top))
    if any(s["health"] == "degraded" for s in sws):
        recs.append("Investigate degraded Hybrid devices before the next migration wave.")
    return {
        "title": "Control-Plane Quantum-Safe Compliance Report", "env": env.name, "site": site, "generated_at": iso(now()),
        "summary": {"quantum_safe_traffic_pct": pct(safe_bw / tot_bw), "weighted_coverage_pct": pct(coverage_for(env, site)),
                    "flows_total": len(flows), "flows_protected": len(prot), "switches_total": len(sws),
                    "switches_hybrid": sum(env.state(s["id"]) == "Hybrid" for s in sws),
                    "switches_not_pqc_capable": len(blockers), "rollback_rate_pct": pct(rollback_rate(env)),
                    "statement": f"{pct(safe_bw / tot_bw)}% of control-plane traffic is protected end-to-end by hybrid post-quantum TLS."},
        "algorithms": {"key_exchange": "X25519MLKEM768 (FIPS 203 ML-KEM-768 + X25519)", "authentication": "ML-DSA-65 (FIPS 204)",
                       "legacy": "X25519 / ECDSA-P256"},
        "by_site": group("site"), "by_service": group("service"),
        "unprotected_flows": [env.flow_view(f, brief=True) for f in sorted((f for f in flows if f not in prot), key=lambda f: -f["weight"])[:15]],
        "recommendations": recs, "trend_30d": env.trend,
    }


def render_pdf(rep):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=rep["title"])
    st = getSampleStyleSheet()
    sm = rep["summary"]

    def table(rows, widths=None):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")])]))
        return t
    el = [Paragraph(rep["title"], st["Title"]),
          Paragraph(f"Environment: <b>{rep['env']}</b> &nbsp; Scope: <b>{rep['site'] or 'all sites'}</b> &nbsp; Generated: {rep['generated_at']}", st["Normal"]),
          Spacer(1, 8), Paragraph(sm["statement"], st["Heading3"]),
          table([["Metric", "Value"], ["Quantum-safe traffic", f"{sm['quantum_safe_traffic_pct']}%"],
                 ["Weighted coverage C", f"{sm['weighted_coverage_pct']}%"],
                 ["Flows protected", f"{sm['flows_protected']} / {sm['flows_total']}"],
                 ["Switches Hybrid", f"{sm['switches_hybrid']} / {sm['switches_total']}"],
                 ["Devices not PQC-capable", sm["switches_not_pqc_capable"]], ["Rollback rate", f"{sm['rollback_rate_pct']}%"]],
                [90 * mm, 60 * mm]),
          Spacer(1, 8), Paragraph("Algorithms", st["Heading3"]),
          Paragraph(f"Key exchange: {rep['algorithms']['key_exchange']}<br/>Authentication: {rep['algorithms']['authentication']}", st["Normal"]),
          Spacer(1, 8), Paragraph("Coverage by site", st["Heading3"]),
          table([["Site", "Flows", "Protected", "Quantum-safe traffic", "Weighted coverage"]] +
                [[r["site"], r["flows"], r["protected"], f"{r['quantum_safe_traffic_pct']}%", f"{r['weighted_coverage_pct']}%"] for r in rep["by_site"]]),
          Spacer(1, 8), Paragraph("Coverage by service", st["Heading3"]),
          table([["Service", "Flows", "Protected", "Quantum-safe traffic"]] +
                [[r["service"], r["flows"], r["protected"], f"{r['quantum_safe_traffic_pct']}%"] for r in rep["by_service"]]),
          Spacer(1, 8), Paragraph("Top unprotected flows", st["Heading3"]),
          table([["Flow", "Service", "Weight", "Blocking switches"]] +
                [[r["name"], r["service"], r["weight"], ", ".join(r["blocking_switches"][:3])] for r in rep["unprotected_flows"][:12]])]
    if rep["recommendations"]:
        el += [Spacer(1, 8), Paragraph("Recommendations", st["Heading3"])] + [Paragraph("- " + r, st["Normal"]) for r in rep["recommendations"]]
    doc.build(el)
    return buf.getvalue()


def render_csv(rep):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value"])
    for k, v in rep["summary"].items():
        w.writerow(["summary", k, v])
    for r in rep["by_site"]:
        w.writerow(["by_site", r["site"], f"flows={r['flows']};protected={r['protected']};safe_traffic_pct={r['quantum_safe_traffic_pct']};coverage_pct={r['weighted_coverage_pct']}"])
    for r in rep["by_service"]:
        w.writerow(["by_service", r["service"], f"flows={r['flows']};protected={r['protected']};safe_traffic_pct={r['quantum_safe_traffic_pct']}"])
    for r in rep["unprotected_flows"]:
        w.writerow(["unprotected_flow", r["name"], f"weight={r['weight']};blocked_by={'|'.join(r['blocking_switches'])}"])
    return buf.getvalue().encode()


def _render(rep, fmt):
    if fmt == "pdf":
        return render_pdf(rep), "application/pdf"
    if fmt == "csv":
        return render_csv(rep), "text/csv"
    return json.dumps(rep, indent=2).encode(), "application/json"


def _record(rep, fmt, actor, content, ctype, schedule_id=None):
    GLOBAL._rep_seq += 1
    rec = {"id": f"REP-{GLOBAL._rep_seq:04d}", "title": rep["title"], "env": rep["env"], "site": rep["site"], "format": fmt,
           "created_at": rep["generated_at"], "created_by": actor, "size_bytes": len(content), "schedule_id": schedule_id,
           "quantum_safe_traffic_pct": rep["summary"]["quantum_safe_traffic_pct"], "_content": content, "_type": ctype}
    GLOBAL.report_history.append(rec)
    return rec


def _public(rec):
    return {k: v for k, v in rec.items() if not k.startswith("_")} | {"download_url": f"/api/v1/reports/history/{rec['id']}/download"}


@rep_router.get("/compliance", summary="Compliance report as JSON (on-screen preview)")
def compliance(site: Optional[str] = None, env=Depends(get_env)):
    return build_compliance(env, site)


@rep_router.get("/compliance/export", summary="Download the compliance report (pdf | csv | json)")
def export(format: str = Query("pdf", pattern="^(pdf|csv|json)$"), site: Optional[str] = None,
           env=Depends(get_env), actor: str = Depends(get_actor)):
    rep = build_compliance(env, site)
    content, ctype = _render(rep, format)
    rec = _record(rep, format, actor, content, ctype)
    return Response(content, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="qsmo-compliance-{env.name}-{rec["id"]}.{format}"'})


@rep_router.post("/generate", status_code=201, summary="Generate + store a report; returns metadata and a download URL")
def generate(body: ReportRequest, actor: str = Depends(get_actor)):
    env = env_store(body.env)
    rep = build_compliance(env, body.site)
    if body.title:
        rep["title"] = body.title
    content, ctype = _render(rep, body.format)
    return _public(_record(rep, body.format, actor, content, ctype))


@rep_router.get("/history", summary="Generated reports")
def history(page: int = 1, page_size: int = 25):
    rows = [_public(r) for r in reversed(GLOBAL.report_history)]
    return paginate(rows, page, page_size)


@rep_router.get("/history/{report_id}/download", summary="Download a stored report")
def download(report_id: str):
    rec = next((r for r in GLOBAL.report_history if r["id"] == report_id), None)
    if not rec:
        raise HTTPException(404, f"report '{report_id}' not found")
    return Response(rec["_content"], media_type=rec["_type"],
                    headers={"Content-Disposition": f'attachment; filename="{report_id}.{rec["format"]}"'})


def _sched(s):
    return {**s, "next_run_at": GLOBAL.next_run(s) if s["enabled"] else None}


@rep_router.get("/schedules", summary="Scheduled reports")
def schedules():
    return {"items": [_sched(s) for s in GLOBAL.schedules.values()]}


@rep_router.post("/schedules", status_code=201, summary="Create a schedule")
def create_schedule(body: ScheduleIn, actor: str = Depends(get_actor)):
    env_store(body.env)
    GLOBAL._sch_seq += 1
    sid = f"sch-{GLOBAL._sch_seq:03d}"
    GLOBAL.schedules[sid] = {"id": sid, "name": body.name, "frequency": body.frequency, "time": body.time, "format": body.format,
                             "scope": {"env": body.env, "site": body.site}, "recipients": body.recipients,
                             "enabled": body.enabled, "last_run_at": None, "created_by": actor}
    return _sched(GLOBAL.schedules[sid])


@rep_router.put("/schedules/{sid}", summary="Update a schedule")
def update_schedule(sid: str, body: ScheduleIn):
    if sid not in GLOBAL.schedules:
        raise HTTPException(404, "schedule not found")
    s = GLOBAL.schedules[sid]
    s.update(name=body.name, frequency=body.frequency, time=body.time, format=body.format,
             scope={"env": body.env, "site": body.site}, recipients=body.recipients, enabled=body.enabled)
    return _sched(s)


@rep_router.delete("/schedules/{sid}", status_code=204, summary="Delete a schedule")
def delete_schedule(sid: str):
    if sid not in GLOBAL.schedules:
        raise HTTPException(404, "schedule not found")
    del GLOBAL.schedules[sid]


@rep_router.post("/schedules/{sid}/run", status_code=201, summary="Run a schedule now")
def run_schedule(sid: str, actor: str = Depends(get_actor)):
    if sid not in GLOBAL.schedules:
        raise HTTPException(404, "schedule not found")
    s = GLOBAL.schedules[sid]
    rep = build_compliance(env_store(s["scope"]["env"]), s["scope"]["site"])
    content, ctype = _render(rep, s["format"])
    rec = _record(rep, s["format"], actor, content, ctype, sid)
    s["last_run_at"] = rec["created_at"]
    return {**_public(rec), "delivered_to": s["recipients"]}
