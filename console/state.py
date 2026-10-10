"""
console/state.py - in-memory, seeded state + domain logic for the QSMO Console API.

The coverage / SGBM math is NOT re-implemented here: it calls the project's own
network/coverage.py and optimizer/optimizer.py on a seeded multi-site topology.
Everything is in memory (resets on restart); swap EnvStore for a DB layer later
without touching the routers' URL contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone

import networkx as nx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from network.coverage import (  # noqa: E402
    annotate_flow_weights, compute_legacy_set, compute_path, core_switch,
    edge_key, flow_weight,
)
import optimizer.optimizer as opt  # noqa: E402
from optimizer.capability_tracker import CapabilityTracker  # noqa: E402

OPT_LOCK = threading.Lock()  # optimizer.SCORE_ALPHA is a module global


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def parse_dt(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pct(x):
    return round(100.0 * x, 2)


def percentile(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)


def paginate(items, page=1, page_size=25):
    page, page_size = max(1, page), max(1, min(page_size, 500))
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total,
            "page": page, "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size))}


def sort_items(items, sort, order="asc"):
    if not sort:
        return items
    key = lambda x: (x.get(sort) is None, str(x.get(sort)).lower() if isinstance(x.get(sort), str) else x.get(sort))
    try:
        return sorted(items, key=key, reverse=(order == "desc"))
    except TypeError:
        return items


# ----------------------------------------------------------------------
# reference data
# ----------------------------------------------------------------------
SITES = {
    "chn": {"code": "chn", "name": "Chennai DC", "region": "IN-South"},
    "blr": {"code": "blr", "name": "Bengaluru DC", "region": "IN-South"},
    "hyd": {"code": "hyd", "name": "Hyderabad DC", "region": "IN-Central"},
}
ENV_CONFIG = {  # edge switches per site (chn, blr, hyd) -> prod = 44 switches
    "prod": {"edges": (13, 12, 10), "seed": 11, "label": "Production"},
    "staging": {"edges": (5, 4, 4), "seed": 22, "label": "Staging"},
    "lab": {"edges": (2, 2, 2), "seed": 33, "label": "Lab"},
}
MODELS = {
    "core": [("Arista", "7280CR3-32P4", "4.31.2F"), ("Cisco", "Nexus 9336C-FX2", "10.3(4a)M"),
             ("Juniper", "PTX10001-36MR", "23.4R2-S2")],
    "agg": [("Arista", "7050SX3-96YC8", "4.31.2F"), ("Juniper", "QFX5120-48Y", "23.4R2-S2"),
            ("Cisco", "Nexus 93240YC-FX2", "10.3(4a)M")],
    "edge": [("Arista", "720XP-48ZC2", "4.30.4M"), ("Cisco", "Catalyst 9300-48P", "17.9.4a"),
             ("Juniper", "EX4300-48T", "21.4R3-S5"), ("Dell EMC", "S4148F-ON", "10.5.5.5")],
}
SERVICES = [  # (service, bandwidth, C, I, A, class, q_delay, measured)
    ("Clinical Network", 25, "High", "High", "Moderate", "delay_sensitive", 15, 12),
    ("Payments & Banking", 25, "High", "High", "Moderate", "delay_sensitive", 15, 12),
    ("Video Conferencing", 5, "Low", "Low", "Low", "delay_sensitive", 60, 55),
    ("IoT Telemetry", 8, "Low", "Moderate", "Low", "delay_sensitive", 45, 40),
    ("Corporate LAN", 12, "Moderate", "Moderate", "Moderate", "delay_sensitive", 40, 33),
]
INFRA = ("Control-Plane Backbone", 10, "Moderate", "Moderate", "Moderate", "delay_sensitive", 40, 35)

LEGACY_LAT_MS, HYBRID_LAT_MS = 18.49, 24.81  # paper Fig. 3
LEGACY_HS_BYTES, HYBRID_HS_BYTES = 2900, 13800  # nominal: X25519+ECDSA vs X25519MLKEM768+ML-DSA-65
LEGACY_CPU_MS, HYBRID_CPU_MS = 0.9, 1.6

DEFAULT_POLICY = {"tau_latency_ms": 10.0, "tau_failure": 0.05, "kappa_retries": 3,
                  "poll_interval_s": 30, "cooldown_s": 60}

PLAN_STATUSES = ["Draft", "Pending approval", "Approved", "Running", "Completed", "Failed", "Rolled back", "Cancelled"]
STRATEGIES = {
    "sgbm": "SGBM (supermodular-aware greedy batch)",
    "simple_greedy": "Simple greedy (best single-switch gain/cost)",
    "sequential": "Sequential (by device ID)",
    "random": "Random order",
    "manual": "Manual (explicit device list)",
}
ROLE_PERMISSIONS = {
    "Admin": ["*"],
    "Operator": ["devices:read", "flows:read", "plans:read", "plans:write", "plans:execute",
                 "cr:read", "cr:write", "monitoring:read", "monitoring:rollback", "alerts:ack",
                 "reports:read", "reports:write", "audit:read", "settings:read"],
    "Auditor": ["devices:read", "flows:read", "plans:read", "cr:read", "monitoring:read",
                "reports:read", "audit:read", "settings:read", "users:read"],
}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


# ----------------------------------------------------------------------
# per-environment store
# ----------------------------------------------------------------------
class EnvStore:
    def __init__(self, name):
        cfg = ENV_CONFIG[name]
        self.name, self.cfg = name, cfg
        self.rng = random.Random(cfg["seed"])
        self.lock = threading.RLock()
        self.policy = dict(DEFAULT_POLICY)
        self.G = nx.Graph()
        self.sw = {}            # dpid -> switch dict
        self.by_name = {}
        self.flows = []         # annotated flow dicts
        self.flow_by_id = {}
        self.paths = {}         # flow name -> path (static topology)
        self.plans, self.crs, self.alerts = {}, {}, {}
        self.audit = []
        self._seq = {"evt": 0, "plan": 6, "cr": 1040, "alr": 0}
        self.last_synced = now()
        self.budget_total = 0.0
        self._cache = {}
        self._build_network()
        self._build_flows()
        self._seed_history()

    # ---- ids ---------------------------------------------------------
    def next_id(self, kind, fmt):
        self._seq[kind] += 1
        return fmt.format(self._seq[kind])

    # ---- network -------------------------------------------------------
    def _build_network(self):
        rng, G = self.rng, self.G
        site_nodes = {}
        for si, (code, edges_n) in enumerate(zip(SITES, self.cfg["edges"])):
            counters = {"core": 0, "agg": 0, "edge": 0}
            nodes = {"core": [], "agg": [], "edge": []}
            plan = [("core", 1), ("agg", 2), ("edge", edges_n)]
            for role, n in plan:
                for _ in range(n):
                    counters[role] += 1
                    k = counters[role]
                    name = f"sw-{code}-{role}-{k:02d}"
                    dpid = f"{si + 1:02x}{ENV_CONFIG[self.name]['seed']:02x}{ {'core':1,'agg':2,'edge':3}[role]:02x}00{k:08x}"
                    vendor, model, fw = rng.choice(MODELS[role])
                    capable = not (role == "edge" and rng.random() < 0.12)
                    if not capable:
                        fw = fw.split("-")[0] + "-legacy"
                    boot_days = rng.randint(9, 420)
                    base_cost = round(0.26 + {"core": 0.30, "agg": 0.20, "edge": 0.0}[role] + rng.random() * 0.10, 3)
                    legacy_lat = round(max(8.0, rng.gauss(LEGACY_LAT_MS, 1.4)), 2)
                    s = {
                        "id": dpid, "name": name, "site": code, "role": role,
                        "vendor": vendor, "model": model, "firmware": fw,
                        "mgmt_ip": f"10.{20 + si}.{ {'core':0,'agg':1,'edge':2}[role] }.{k}",
                        "serial": f"{vendor[:3].upper()}{rng.randint(10**7, 10**8 - 1)}",
                        "booted_at": now() - timedelta(days=boot_days, hours=rng.randint(0, 23)),
                        "pqc_capable": capable, "base_cost": base_cost,
                        "health": "healthy", "baseline": {
                            "latency_ms": legacy_lat, "failure_rate": round(rng.uniform(0.0005, 0.004), 4),
                            "overhead_bytes": LEGACY_HS_BYTES + rng.randint(-120, 120)},
                        "post": None, "migrated_at": None, "degraded_count": 0,
                        "degraded_since": None, "next_recheck_at": None,
                        "tags": [], "cert_serial": secrets.token_hex(8),
                    }
                    self.sw[dpid] = s
                    self.by_name[name] = dpid
                    G.add_node(dpid, state="Legacy", name=name)
                    nodes[role].append(dpid)
            site_nodes[code] = nodes
            core = nodes["core"][0]
            for a in nodes["agg"]:
                G.add_edge(core, a, kind="uplink")
            if len(nodes["agg"]) == 2:
                G.add_edge(nodes["agg"][0], nodes["agg"][1], kind="uplink")
            for i, e in enumerate(nodes["edge"]):
                G.add_edge(e, nodes["agg"][i % 2], kind="access")
                if i % 3 == 0:  # dual-homed
                    G.add_edge(e, nodes["agg"][(i + 1) % 2], kind="access")
                if i % 4 == 1:  # also direct to the site core
                    G.add_edge(e, core, kind="access")
        cores = [site_nodes[c]["core"][0] for c in SITES]
        for i in range(len(cores)):
            for j in range(i + 1, len(cores)):
                G.add_edge(cores[i], cores[j], kind="wan")
        # make sure the model's core (max degree) is the first site's core switch
        first_core, first_edges = cores[0], list(site_nodes["chn"]["edge"])
        for e in first_edges:
            if core_switch(G) == first_core:
                break
            if not G.has_edge(e, first_core):
                G.add_edge(e, first_core, kind="access")
        for e in self.rng.sample(list(G.edges), len(G.edges)):
            G.edges[e]["utilization"] = round(self.rng.uniform(0.08, 0.85), 3)
        self.core_id = core_switch(G)
        # one unreachable legacy edge in prod (alert fodder)
        if self.name == "prod":
            victim = [d for d, s in self.sw.items() if s["role"] == "edge" and s["site"] == "hyd"][-1]
            self.sw[victim]["health"] = "unreachable"
        self.budget_total = round(0.6 * sum(s["base_cost"] for s in self.sw.values()), 3)

    def _build_flows(self):
        rng = self.rng
        raw, svc_i = [], 0
        for i, (d, s) in enumerate(sorted(self.sw.items(), key=lambda kv: kv[1]["name"])):
            if s["role"] == "edge":
                prof = SERVICES[svc_i % len(SERVICES)]
                svc_i += 1
            else:
                prof = INFRA
            svc, bw, c, ii, a, tc, q, m = prof
            raw.append({
                "id": f"flw-{i + 1:04d}", "name": f"ctrl-{s['name']}", "source": "c0",
                "destination": s["name"], "destination_dpid": d, "service": svc, "site": s["site"],
                "bandwidth_mbps": bw, "impact_confidentiality": c, "impact_integrity": ii,
                "impact_availability": a, "traffic_class": tc, "q_delay_ms": q,
                "measured_delay_ms": m + rng.randint(-2, 2)})
        util = {edge_key(u, v): self.G.edges[u, v]["utilization"] for u, v in self.G.edges}
        bw = {f["name"]: f["bandwidth_mbps"] for f in raw}
        self.flows = annotate_flow_weights(self.G, raw, util, bw)
        for f in self.flows:
            f["weight"] = flow_weight(f)
            self.paths[f["name"]] = compute_path(self.G, f)
        self.flow_by_id = {f["id"]: f for f in self.flows}
        self.total_w = sum(f["weight"] for f in self.flows)

    def recompute_weights(self):
        util = {edge_key(u, v): self.G.edges[u, v]["utilization"] for u, v in self.G.edges}
        bw = {f["name"]: f["bandwidth_mbps"] for f in self.flows}
        self.flows = annotate_flow_weights(self.G, self.flows, util, bw)
        for f in self.flows:
            f["weight"] = flow_weight(f)
        self.flow_by_id = {f["id"]: f for f in self.flows}
        self.total_w = sum(f["weight"] for f in self.flows)

    # ---- state / coverage ------------------------------------------------
    def state(self, d):
        return self.G.nodes[d]["state"]

    def hybrid_set(self):
        return {d for d in self.G.nodes if self.state(d) == "Hybrid"}

    def cov(self, hset=None):
        """Weighted coverage as a fraction in [0, 1]."""
        hset = self.hybrid_set() if hset is None else hset
        if not self.total_w:
            return 0.0
        return sum(f["weight"] for f in self.flows if set(self.paths[f["name"]]) <= hset) / self.total_w

    def resolve(self, ident):
        if ident in self.sw:
            return ident
        if ident in self.by_name:
            return self.by_name[ident]
        raise NotFound(f"switch '{ident}' not found in env '{self.name}'")

    def flow_view(self, f, brief=False):
        path = self.paths[f["name"]]
        legacy = [d for d in path if self.state(d) != "Hybrid"]
        w = f["weight"]
        out = {
            "id": f["id"], "name": f["name"], "service": f["service"], "site": f["site"],
            "destination": f["destination"], "destination_dpid": f["destination_dpid"],
            "weight": round(w, 4), "criticality": round(f["criticality"], 4),
            "security_req": round(f["security_sla"], 4), "latency_sens": round(f["latency_sla"], 4),
            "protected": not legacy, "hops": len(path),
            "blocking_switches": [self.sw[d]["name"] for d in legacy],
            "bandwidth_mbps": f["bandwidth_mbps"],
        }
        if not brief:
            out.update({
                "weight_breakdown": {"criticality": {"value": round(f["criticality"], 4), "factor": 0.5, "contribution": round(0.5 * f["criticality"], 4)},
                                     "security_req": {"value": round(f["security_sla"], 4), "factor": 0.3, "contribution": round(0.3 * f["security_sla"], 4)},
                                     "latency_sens": {"value": round(f["latency_sla"], 4), "factor": 0.2, "contribution": round(0.2 * f["latency_sla"], 4)}},
                "inputs": {k: f[k] for k in ("impact_confidentiality", "impact_integrity", "impact_availability",
                                             "traffic_class", "q_delay_ms", "measured_delay_ms")},
                "latency_feasible": f.get("latency_feasible", True),
                "path": [{"id": d, "name": self.sw[d]["name"], "state": self.state(d), "role": self.sw[d]["role"]} for d in path],
                "share_of_total_weight_pct": pct(w / self.total_w),
            })
        return out

    def switch_view(self, d, detail=False):
        s = self.sw[d]
        flows_via = [f for f in self.flows if d in self.paths[f["name"]]]
        blocked = [f for f in flows_via if not set(self.paths[f["name"]]) <= self.hybrid_set()]
        out = {
            "id": d, "name": s["name"], "site": s["site"], "role": s["role"], "vendor": s["vendor"],
            "model": s["model"], "firmware": s["firmware"], "mgmt_ip": s["mgmt_ip"],
            "state": self.state(d), "health": s["health"], "pqc_capable": s["pqc_capable"],
            "migration_cost": s["base_cost"], "uptime_s": int((now() - s["booted_at"]).total_seconds()),
            "degree": self.G.degree[d], "is_core": d == self.core_id,
            "migrated_at": iso(s["migrated_at"]), "degraded_count": s["degraded_count"],
            "flows_via": len(flows_via), "flows_blocked_by_this": len(blocked) if self.state(d) != "Hybrid" else 0,
            "baseline": s["baseline"], "post": s["post"],
        }
        if detail:
            out.update({"serial": s["serial"], "tags": s["tags"],
                        "neighbors": [{"id": n, "name": self.sw[n]["name"], "state": self.state(n)} for n in self.G.neighbors(d)],
                        "booted_at": iso(s["booted_at"])})
        return out

    def crypto_view(self, d):
        s = self.sw[d]
        nb = (s["booted_at"] + timedelta(days=200)).date()
        mk = lambda kex, sig, kt, issuer: {
            "tls_version": "TLS 1.3", "cipher_suite": "TLS_AES_256_GCM_SHA384", "key_exchange": kex,
            "signature": sig, "certificate": {"subject": f"CN={s['name']}", "issuer": issuer, "key_type": kt,
                                              "serial": s["cert_serial"], "not_before": str(nb),
                                              "not_after": str(nb + timedelta(days=825))}}
        legacy = mk("X25519", "ECDSA-P256 (SHA-256)", "EC P-256", "CN=sdn-lab-CA")
        hybrid = mk("X25519MLKEM768", "ML-DSA-65", "ML-DSA-65", "CN=sdn-pqc-hybrid-CA")
        hy = self.state(d) == "Hybrid"
        return {"switch_id": d, "name": s["name"], "state": self.state(d),
                "active": hybrid if hy else legacy, "active_profile": "Hybrid" if hy else "Legacy",
                "legacy_profile": legacy, "hybrid_profile": hybrid,
                "quantum_safe_key_exchange": hy, "quantum_safe_authentication": hy,
                "eligible_for_hybrid": s["pqc_capable"], "eligibility_note": None if s["pqc_capable"] else
                "Firmware does not support ML-KEM/ML-DSA; upgrade required before migration."}

    # ---- audit / alerts ----------------------------------------------------
    def _chain(self, ev):
        prev = self.audit[-1]["hash"] if self.audit else "0" * 64
        self._seq["evt"] += 1
        ev["id"] = f"evt-{self._seq['evt']:06d}"
        ev["prev_hash"] = prev
        body = json.dumps({k: ev[k] for k in sorted(ev) if k not in ("hash",)}, sort_keys=True, default=str)
        ev["hash"] = hashlib.sha256(body.encode()).hexdigest()
        self.audit.append(ev)
        return ev

    def make_event(self, action, actor="system", outcome="info", switch=None, reason=None, before=None,
                   after=None, baseline=None, post=None, plan_id=None, details=None, ts=None):
        s = self.sw.get(switch) if switch else None
        flows = [f["name"] for f in self.flows if switch and switch in self.paths[f["name"]]] if switch else []
        return {"ts": iso(ts or now()), "actor": actor, "action": action, "outcome": outcome,
                "switch_id": switch, "switch_name": s["name"] if s else None, "reason": reason,
                "state_before": before, "state_after": after, "baseline": baseline, "post": post,
                "associated_flows": flows, "plan_id": plan_id, "details": details or {}}

    def log(self, *a, **k):
        with self.lock:
            return self._chain(self.make_event(*a, **k))

    def alert(self, severity, title, message, source, switch=None, ts=None, status="open"):
        aid = self.next_id("alr", "ALR-{:04d}")
        self.alerts[aid] = {"id": aid, "severity": severity, "title": title, "message": message,
                            "source": source, "switch_id": switch,
                            "switch_name": self.sw[switch]["name"] if switch else None,
                            "status": status, "created_at": iso(ts or now()),
                            "acknowledged_by": None, "acknowledged_at": None,
                            "resolved_by": None, "resolved_at": None}
        return self.alerts[aid]

    # ---- migrations (shared by seed + plan execution) ---------------------------
    def apply_migration(self, d, actor, plan_id=None, ts=None, record=True):
        """Simulated GME: capable switches succeed, others fail. Returns the result dict."""
        s = self.sw[d]
        ts = ts or now()
        before = self.state(d)
        if before == "Hybrid":
            return {"dpid": d, "outcome": "success", "skipped": True}
        if s["health"] == "unreachable" or not s["pqc_capable"]:
            reason = "device unreachable" if s["health"] == "unreachable" else "handshake failed: ML-KEM/ML-DSA not supported by firmware"
            if record:
                self._chain(self.make_event("migration.failed", actor, "failed", d, reason, before, before,
                                            baseline=s["baseline"], plan_id=plan_id, ts=ts))
                self.alert("high", f"Migration failed on {s['name']}", reason, "plan", d, ts)
            return {"dpid": d, "outcome": "failed", "reason": reason}
        post = {"latency_ms": round(s["baseline"]["latency_ms"] * (HYBRID_LAT_MS / LEGACY_LAT_MS) + self.rng.gauss(0, 0.6), 2),
                "failure_rate": round(s["baseline"]["failure_rate"] * 1.2, 4),
                "overhead_bytes": HYBRID_HS_BYTES + self.rng.randint(-300, 300)}
        self.G.nodes[d]["state"] = "Hybrid"
        s.update(post=post, migrated_at=ts, degraded_count=0, degraded_since=None, next_recheck_at=None, health="healthy")
        if record:
            self._chain(self.make_event("migration.success", actor, "success", d, None, "Legacy", "Hybrid",
                                        baseline=s["baseline"], post=post, plan_id=plan_id, ts=ts))
        return {"dpid": d, "outcome": "success", "post": post}

    def revert(self, d, actor, reason, ts=None, plan_id=None):
        s = self.sw[d]
        self.G.nodes[d]["state"] = "Legacy"
        s.update(post=None, health="healthy", degraded_count=0, degraded_since=None, next_recheck_at=None)
        ev = self._chain(self.make_event("migration.reverted", actor, "reverted", d, reason, "Hybrid", "Legacy",
                                         baseline=s["baseline"], plan_id=plan_id, ts=ts))
        self.alert("critical", f"Rolled back {s['name']} to Legacy", reason, "monitor", d, ts)
        return ev

    # ---- SGBM + baselines ------------------------------------------------------
    def make_tracker(self):
        return CapabilityTracker(base_costs={d: s["base_cost"] for d, s in self.sw.items()}, default_cost=0.259)

    def total_cost(self):
        return sum(s["base_cost"] for s in self.sw.values())

    def _sim_migrate(self, d):
        s = self.sw[d]
        ok = s["pqc_capable"] and s["health"] != "unreachable"
        return {"dpid": d, "outcome": "success" if ok else "failed"}

    def sgbm_steps(self, G, budget, alpha=1.0, migrate_fn=None):
        migrate_fn = migrate_fn or self._sim_migrate
        tracker, steps, remaining = self.make_tracker(), [], budget
        with OPT_LOCK:
            old = opt.SCORE_ALPHA
            opt.SCORE_ALPHA = alpha
            try:
                while remaining > 1e-9 and opt.get_legacy_switches(G):
                    pick = opt.pick_best_batch(G, self.flows, remaining, tracker)
                    kind, flow_name = "batch", None
                    if pick:
                        flow, Lf, cost, gain = pick
                        members, flow_name = sorted(Lf), flow["name"]
                    else:
                        fb = opt._pick_progress_switch(G, self.flows, remaining, tracker)
                        if fb is None:
                            break
                        d, _score = fb
                        members, cost, gain, kind = [d], tracker.cost(d), 0.0, "single"
                    outs = []
                    for d in members:
                        r = migrate_fn(d)
                        tracker.record(d, r["outcome"])
                        outs.append(r["outcome"])
                        if r["outcome"] == "success":
                            G.nodes[d]["state"] = "Hybrid"
                    remaining -= cost
                    hs = {n for n in G.nodes if G.nodes[n]["state"] == "Hybrid"}
                    steps.append({"kind": kind, "flow": flow_name, "switches": members, "outcomes": outs,
                                  "cost": round(cost, 3), "gain_pct": pct(gain / self.total_w) if gain else None,
                                  "coverage_after_pct": pct(self.cov(hs)), "budget_remaining": round(max(remaining, 0), 3)})
            finally:
                opt.SCORE_ALPHA = old
        return steps

    def baseline_steps(self, strategy, budget, rng=None, order=None):
        """simple_greedy / sequential / random / manual, evaluated with the precomputed paths."""
        rng = rng or random.Random(7)
        hs = self.hybrid_set()
        legacy = [d for d in sorted(self.sw) if d not in hs]
        failed, steps, remaining = set(), [], budget
        if strategy == "random":
            rng.shuffle(legacy)
        if strategy == "manual":
            legacy = [d for d in (order or []) if d not in hs]
        queue = list(legacy)
        while remaining > 1e-9:
            cands = [d for d in queue if d not in failed and self.sw[d]["base_cost"] <= remaining + 1e-9]
            if not cands:
                break
            if strategy == "simple_greedy":
                base = self.cov(hs)
                through = {d: sum(f["weight"] for f in self.flows if d in self.paths[f["name"]]) for d in cands}
                d = max(cands, key=lambda x: ((self.cov(hs | {x}) - base + 1e-6 * through[x]) / self.sw[x]["base_cost"]))
            else:
                d = cands[0]
            r = self._sim_migrate(d)
            cost = self.sw[d]["base_cost"]
            remaining -= cost
            queue.remove(d)
            if r["outcome"] == "success":
                before = self.cov(hs)
                hs = hs | {d}
            else:
                failed.add(d)
                before = self.cov(hs)
            steps.append({"kind": "single", "flow": None, "switches": [d], "outcomes": [r["outcome"]],
                          "cost": round(cost, 3), "gain_pct": pct(self.cov(hs) - before),
                          "coverage_after_pct": pct(self.cov(hs)), "budget_remaining": round(max(remaining, 0), 3)})
        return steps

    def plan_steps(self, strategy, budget, alpha=1.0, device_ids=None):
        if strategy == "sgbm":
            return self.sgbm_steps(self.G.copy(), budget, alpha)
        if strategy in ("simple_greedy", "sequential", "random", "manual"):
            order = [self.resolve(x) for x in (device_ids or [])] if strategy == "manual" else None
            return self.baseline_steps(strategy, budget, order=order)
        raise ValueError(f"unknown strategy '{strategy}'")

    def dry_run(self, strategy, budget, alpha=1.0, device_ids=None):
        steps = self.plan_steps(strategy, budget, alpha, device_ids)
        for i, st in enumerate(steps, 1):
            st["step"] = i
            st["switch_names"] = [self.sw[d]["name"] for d in st["switches"]]
        start = self.cov()
        spent = sum(s["cost"] for s in steps)
        curve = [{"cost": 0.0, "coverage_pct": pct(start)}]
        cum = 0.0
        for st in steps:
            cum += st["cost"]
            curve.append({"cost": round(cum, 3), "coverage_pct": st["coverage_after_pct"]})
        res = {"strategy": strategy, "alpha": alpha, "budget": round(budget, 3),
               "budget_spent": round(spent, 3), "start_coverage_pct": pct(start),
               "projected_coverage_pct": steps[-1]["coverage_after_pct"] if steps else pct(start),
               "switch_count": sum(len(s["switches"]) for s in steps),
               "predicted_failures": sum(o == "failed" for s in steps for o in s["outcomes"]),
               "steps": steps, "coverage_curve": curve}
        if strategy == "sgbm":
            with OPT_LOCK:
                res["supermodular_degree"] = opt.supermodular_degree(self.G, self.flows)
                res["approx_ratio"] = round(opt.sgbm_approx_ratio(self.G, self.flows), 4)
        return res

    # ---- time series ---------------------------------------------------------------
    def series(self, d, minutes=60, step=1):
        s, pol, t_now = self.sw[d], self.policy, now()
        hybrid_lat = (s["post"] or {}).get("latency_ms", s["baseline"]["latency_ms"] * HYBRID_LAT_MS / LEGACY_LAT_MS)
        pts, n = [], max(2, minutes // step)
        for i in range(n + 1):
            ts = t_now - timedelta(minutes=(n - i) * step)
            r = random.Random(f"{d}:{int(ts.timestamp() // 60)}")
            is_h = s["migrated_at"] is not None and ts >= s["migrated_at"] and self.state(d) == "Hybrid"
            lat = (hybrid_lat if is_h else s["baseline"]["latency_ms"]) + r.gauss(0, 0.9 if is_h else 0.7)
            fr = (0.004 if is_h else s["baseline"]["failure_rate"]) * r.uniform(0.6, 1.5)
            if s["degraded_since"] and ts >= s["degraded_since"]:
                ramp = min(1.0, (ts - s["degraded_since"]).total_seconds() / 900.0)
                lat += ramp * pol["tau_latency_ms"] * 1.4
                fr += ramp * pol["tau_failure"] * 1.5
            pts.append({"t": iso(ts), "latency_ms": round(max(lat, 1.0), 2), "failure_rate": round(max(fr, 0.0), 4)})
        base = s["baseline"]
        return {"switch_id": d, "name": s["name"], "state": self.state(d), "points": pts,
                "baseline_latency_ms": base["latency_ms"], "baseline_failure_rate": base["failure_rate"],
                "latency_threshold_ms": round(base["latency_ms"] + pol["tau_latency_ms"], 2),
                "failure_threshold": round(base["failure_rate"] + pol["tau_failure"], 4),
                "rollback_marker": None}

    # ---- monitoring tick (lazy; called on monitoring reads) -------------------------------
    def tick(self):
        with self.lock:
            t, pol = now(), self.policy
            for d, s in self.sw.items():
                if self.state(d) != "Hybrid" or s["health"] != "degraded" or not s["next_recheck_at"]:
                    continue
                while s["next_recheck_at"] and s["next_recheck_at"] <= t and s["health"] == "degraded":
                    if s["degraded_count"] >= pol["kappa_retries"]:
                        self.revert(d, "qsmo-monitor", f"Persistent degradation after {pol['kappa_retries']} rechecks "
                                    f"(latency > baseline+{pol['tau_latency_ms']} ms)")
                        break
                    s["degraded_count"] += 1
                    s["next_recheck_at"] = s["next_recheck_at"] + timedelta(seconds=pol["cooldown_s"])

    def degrade(self, d, retries_used=0):
        s = self.sw[d]
        if self.state(d) != "Hybrid":
            raise Conflict(f"{s['name']} is not Hybrid; only Hybrid switches are monitored")
        s.update(health="degraded", degraded_since=now() - timedelta(minutes=12), degraded_count=retries_used,
                 next_recheck_at=now() + timedelta(seconds=self.policy["cooldown_s"]))
        self.alert("high", f"Handshake degradation on {s['name']}",
                   f"Latency exceeded baseline + {self.policy['tau_latency_ms']} ms; recheck scheduled.", "monitor", d)

    # ---- plan execution (lazy progress) ------------------------------------------------------
    SECONDS_PER_SWITCH = 3

    def advance_plan(self, plan):
        with self.lock:
            ex = plan.get("execution")
            if plan["status"] != "Running" or not ex:
                return plan
            seq = ex["sequence"]
            due = min(len(seq), int((now() - parse_dt(ex["started_at"])).total_seconds() // self.SECONDS_PER_SWITCH) + 1)
            while ex["applied"] < due:
                d = seq[ex["applied"]]
                r = self.apply_migration(d, ex["executed_by"], plan["id"])
                ex["results"].append({"switch_id": d, "name": self.sw[d]["name"], "outcome": r["outcome"],
                                      "reason": r.get("reason"), "at": iso(now())})
                ex["applied"] += 1
            if ex["applied"] >= len(seq):
                fails = sum(r["outcome"] == "failed" for r in ex["results"])
                plan["status"] = "Failed" if fails == len(seq) and seq else "Completed"
                ex["finished_at"] = iso(now())
                ex["failed_count"] = fails
                plan["final_coverage_pct"] = pct(self.cov())
                self.log("plan.completed", "qsmo-orchestrator", "success" if not fails else "failed", plan_id=plan["id"],
                         details={"switches": len(seq), "failed": fails})
                cr = self.crs.get(plan.get("change_request_id"))
                if cr and cr["status"] == "Approved":
                    cr["status"] = "Implemented"
            return plan

    # ---- seeding --------------------------------------------------------------------------------
    def _seed_history(self):
        rng, t0 = self.rng, now()
        # 1) run the real SGBM on a sandbox graph to decide what is already Hybrid
        sandbox = self.G.copy()
        budget = 0.42 * self.total_cost()
        steps = self.sgbm_steps(sandbox, budget)
        # 2) spread the steps over the last 30 days, grouped into plans
        n = max(1, len(steps))
        users = ["arun.kumar@qsmo.example", "meera.s@qsmo.example", "priya.raman@qsmo.example"]
        events, plan_groups = [], []
        per_plan = max(2, n // 3)
        for i, st in enumerate(steps):
            day_ts = t0 - timedelta(days=29.0 - 27.5 * i / n, minutes=rng.randint(0, 600))
            grp = i // per_plan
            if grp >= len(plan_groups):
                plan_groups.append({"ts": day_ts, "steps": []})
            plan_groups[grp]["steps"].append((st, day_ts))
        hybrid_seq = []
        for gi, g in enumerate(plan_groups):
            pid = f"plan-{gi + 1:04d}"
            for st, ts in g["steps"]:
                for k, (d, outcome) in enumerate(zip(st["switches"], st["outcomes"])):
                    t = ts + timedelta(seconds=40 * k)
                    s = self.sw[d]
                    if outcome == "success":
                        post = {"latency_ms": round(s["baseline"]["latency_ms"] * HYBRID_LAT_MS / LEGACY_LAT_MS + rng.gauss(0, 0.6), 2),
                                "failure_rate": round(s["baseline"]["failure_rate"] * 1.2, 4),
                                "overhead_bytes": HYBRID_HS_BYTES + rng.randint(-300, 300)}
                        events.append(self.make_event("migration.success", "qsmo-orchestrator", "success", d, None, "Legacy", "Hybrid",
                                                      baseline=s["baseline"], post=post, plan_id=pid, ts=t))
                        hybrid_seq.append((d, t, post))
                    else:
                        events.append(self.make_event("migration.failed", "qsmo-orchestrator", "failed", d,
                                                      "handshake failed: ML-KEM/ML-DSA not supported by firmware", "Legacy", "Legacy",
                                                      baseline=s["baseline"], plan_id=pid, ts=t))
            g["id"] = pid
        # 3) rollbacks: one switch reverted then re-migrated, one reverted for good
        cands = [x for x in hybrid_seq if self.sw[x[0]]["role"] == "edge"][:4]
        rollback_plan = None
        if len(cands) >= 2:
            d1, t1, p1 = cands[0]
            events.append(self.make_event("migration.reverted", "qsmo-monitor", "reverted", d1,
                                          "Persistent degradation after 3 rechecks (latency > baseline+10 ms)", "Hybrid", "Legacy",
                                          baseline=self.sw[d1]["baseline"], plan_id=plan_groups[0]["id"], ts=t1 + timedelta(days=1.2)))
            events.append(self.make_event("migration.success", "qsmo-orchestrator", "success", d1, None, "Legacy", "Hybrid",
                                          baseline=self.sw[d1]["baseline"], post=p1, plan_id=plan_groups[0]["id"], ts=t1 + timedelta(days=3.4)))
            d2, t2, _p2 = cands[1]
            events.append(self.make_event("migration.reverted", "qsmo-monitor", "reverted", d2,
                                          "Failure rate exceeded baseline+0.05 after 3 rechecks", "Hybrid", "Legacy",
                                          baseline=self.sw[d2]["baseline"], plan_id=plan_groups[0]["id"], ts=t2 + timedelta(days=2.0)))
            rollback_plan = plan_groups[0]["id"]
        # 4) governance noise
        for gi, g in enumerate(plan_groups):
            u = users[gi % len(users)]
            events.append(self.make_event("plan.created", u, "success", plan_id=g["id"], ts=g["ts"] - timedelta(hours=30)))
            events.append(self.make_event("change_request.approved", "priya.raman@qsmo.example", "success", plan_id=g["id"],
                                          details={"change_request": f"CR-{1001 + gi}"}, ts=g["ts"] - timedelta(hours=3)))
        events.append(self.make_event("policy.updated", "priya.raman@qsmo.example", "success", ts=t0 - timedelta(days=12),
                                      details={"tau_latency_ms": [8.0, 10.0]}))
        events.sort(key=lambda e: e["ts"])
        # 5) replay events -> final states + coverage trend
        state = {d: "Legacy" for d in self.sw}
        last_ok = {}
        trend, ev_i = [], 0
        for day in range(30, -1, -1):
            cutoff = iso(t0 - timedelta(days=day)) if day else iso(t0 + timedelta(seconds=1))
            while ev_i < len(events) and events[ev_i]["ts"] <= cutoff:
                e = events[ev_i]
                if e["action"] == "migration.success":
                    state[e["switch_id"]] = "Hybrid"; last_ok[e["switch_id"]] = e
                elif e["action"] == "migration.reverted":
                    state[e["switch_id"]] = "Legacy"
                ev_i += 1
            hs = {d for d, v in state.items() if v == "Hybrid"}
            trend.append({"date": (t0 - timedelta(days=day)).date().isoformat(), "coverage_pct": pct(self.cov(hs)),
                          "hybrid_switches": len(hs)})
        self.trend = trend
        for d, v in state.items():
            self.G.nodes[d]["state"] = v
            if v == "Hybrid":
                e = last_ok[d]
                self.sw[d].update(post=e["post"], migrated_at=parse_dt(e["ts"]))
        for e in events:
            self._chain(e)
        # 6) current degradation (monitoring page has something to show)
        hyb_edges = [d for d in self.hybrid_set() if self.sw[d]["role"] == "edge"]
        if len(hyb_edges) >= 3:
            self.degrade(hyb_edges[-1], retries_used=1)
            self.degrade(hyb_edges[-2], retries_used=2)
        # 7) plans + change requests
        for gi, g in enumerate(plan_groups):
            seq = [d for st, _ in g["steps"] for d in st["switches"]]
            status = "Rolled back" if g["id"] == rollback_plan else "Completed"
            cr_id = f"CR-{1001 + gi}"
            plan = {"id": g["id"], "name": f"Wave {gi + 1} - SGBM migration", "strategy": "sgbm", "alpha": 1.0,
                    "budget": round(sum(st["cost"] for st, _ in g["steps"]), 3), "status": status,
                    "created_by": users[gi % len(users)], "created_at": iso(g["ts"] - timedelta(hours=30)),
                    "notes": "Seeded historical wave", "device_ids": [],
                    "steps": [dict(st, step=i + 1, switch_names=[self.sw[d]["name"] for d in st["switches"]]) for i, (st, _) in enumerate(g["steps"])],
                    "projected_coverage_pct": g["steps"][-1][0]["coverage_after_pct"], "change_request_id": cr_id,
                    "execution": {"started_at": iso(g["ts"]), "finished_at": iso(g["ts"] + timedelta(minutes=10)),
                                  "executed_by": "qsmo-orchestrator", "sequence": seq, "applied": len(seq), "results": [], "failed_count": 0}}
            self.plans[g["id"]] = plan
            self.crs[cr_id] = self._new_cr(plan, users[gi % len(users)], "Approved wave", "priya.raman@qsmo.example",
                                           "Implemented", g["ts"] - timedelta(hours=40), g["ts"] - timedelta(hours=3), g["ts"], g["ts"] + timedelta(hours=2))
        # current drafts / pending
        self._seq["plan"] = len(plan_groups)
        d_plan = self.create_plan("Wave 4 - remaining edge", "sgbm", 0.1 * self.total_cost(), 1.0, None, "meera.s@qsmo.example")
        p_plan = self.create_plan("Wave 5 - Hyderabad edge", "simple_greedy", 0.08 * self.total_cost(), 1.0, None, "arun.kumar@qsmo.example")
        self.submit_plan(p_plan["id"], "arun.kumar@qsmo.example",
                         {"start": iso(t0 + timedelta(days=2, hours=2)), "end": iso(t0 + timedelta(days=2, hours=5))},
                         "Extend quantum-safe coverage to Hyderabad payments edge.", "Revert via BRD; legacy TLS target retained.")
        # 8) alerts for the interesting stuff
        for d, s in self.sw.items():
            if s["health"] == "unreachable":
                self.alert("critical", f"{s['name']} unreachable", "No OpenFlow echo reply for 5 minutes.", "monitor", d)
        self.alert("info", "Weekly coverage report generated", "Compliance report REP-0003 is ready.", "reports",
                   ts=t0 - timedelta(days=2), status="resolved")
        self.alert("medium", "Change request awaiting approval", f"{p_plan['change_request_id']} needs review.", "change-requests")
        self.last_synced = now()

    # ---- plans / CRs ---------------------------------------------------------------------------------
    def _new_cr(self, plan, requester, title, approver, status, created, decided, w_start, w_end):
        seq_ids = plan["execution"]["sequence"] if plan.get("execution") else [d for st in plan["steps"] for d in st["switches"]]
        return {"id": plan.get("change_request_id"), "plan_id": plan["id"], "title": f"{plan['name']}: {title}",
                "requester": requester, "approver": approver, "status": status,
                "risk_level": self.risk_for(seq_ids), "maintenance_window": {"start": iso(w_start), "end": iso(w_end)},
                "justification": "Reduce harvest-now-decrypt-later exposure of control-plane traffic.",
                "rollback_plan": "Automatic BRD rollback to Legacy TLS on persistent degradation.",
                "affected_switches": len(seq_ids), "comments": [], "created_at": iso(created),
                "decided_at": iso(decided),
                "history": [{"at": iso(created), "by": requester, "event": "submitted"},
                            {"at": iso(decided), "by": approver, "event": status.lower()}]}

    def risk_for(self, ids):
        score = sum({"core": 6, "agg": 3, "edge": 1}[self.sw[d]["role"]] for d in ids)
        return "Critical" if score >= 25 else "High" if score >= 12 else "Medium" if score >= 5 else "Low"

    def create_plan(self, name, strategy, budget, alpha, device_ids, actor, notes=None):
        with self.lock:
            dr = self.dry_run(strategy, budget, alpha, device_ids)
            pid = self.next_id("plan", "plan-{:04d}")
            plan = {"id": pid, "name": name, "strategy": strategy, "alpha": alpha, "budget": round(budget, 3),
                    "status": "Draft", "created_by": actor, "created_at": iso(now()), "notes": notes,
                    "device_ids": device_ids or [], "steps": dr["steps"], "projected_coverage_pct": dr["projected_coverage_pct"],
                    "change_request_id": None, "execution": None}
            self.plans[pid] = plan
            self.log("plan.created", actor, "success", plan_id=pid, details={"strategy": strategy, "budget": plan["budget"]})
            return plan

    def submit_plan(self, pid, actor, window, justification, rollback_plan):
        with self.lock:
            plan = self.plan(pid)
            if plan["status"] not in ("Draft",):
                raise Conflict(f"plan is '{plan['status']}'; only Draft plans can be submitted")
            dr = self.dry_run(plan["strategy"], plan["budget"], plan["alpha"], plan["device_ids"])  # refresh vs. current state
            plan["steps"], plan["projected_coverage_pct"] = dr["steps"], dr["projected_coverage_pct"]
            ids = [d for st in plan["steps"] for d in st["switches"]]
            if not ids:
                raise Conflict("plan has no migration steps (budget too small or nothing left to migrate)")
            crid = self.next_id("cr", "CR-{}")
            plan["change_request_id"], plan["status"] = crid, "Pending approval"
            self.crs[crid] = {"id": crid, "plan_id": pid, "title": f"{plan['name']}: migrate {len(ids)} switches",
                              "requester": actor, "approver": None, "status": "Pending", "risk_level": self.risk_for(ids),
                              "maintenance_window": window, "justification": justification,
                              "rollback_plan": rollback_plan, "affected_switches": len(ids), "comments": [],
                              "created_at": iso(now()), "decided_at": None,
                              "history": [{"at": iso(now()), "by": actor, "event": "submitted"}]}
            self.log("plan.submitted", actor, "success", plan_id=pid, details={"change_request": crid})
            return self.crs[crid]

    def plan(self, pid):
        if pid not in self.plans:
            raise NotFound(f"plan '{pid}' not found")
        return self.advance_plan(self.plans[pid])

    def cr(self, cid):
        if cid not in self.crs:
            raise NotFound(f"change request '{cid}' not found")
        return self.crs[cid]


# ----------------------------------------------------------------------
# registry (lazy per-env build) + global (non-env) state
# ----------------------------------------------------------------------
_ENVS, _ENV_LOCK = {}, threading.Lock()


def env_store(name):
    if name not in ENV_CONFIG:
        raise NotFound(f"unknown environment '{name}' (use: {', '.join(ENV_CONFIG)})")
    with _ENV_LOCK:
        if name not in _ENVS:
            _ENVS[name] = EnvStore(name)
        return _ENVS[name]


class Globals:
    def __init__(self):
        t = now()
        self.users = {}
        for i, (n, e, r, team) in enumerate([
                ("Priya Raman", "priya.raman@qsmo.example", "Admin", "Network Security"),
                ("Arun Kumar", "arun.kumar@qsmo.example", "Operator", "NOC"),
                ("Meera S", "meera.s@qsmo.example", "Operator", "NOC"),
                ("Karthik V", "karthik.v@qsmo.example", "Auditor", "GRC"),
                ("Divya N", "divya.n@qsmo.example", "Admin", "Platform"),
                ("Rohit Menon", "rohit.menon@qsmo.example", "Auditor", "Internal Audit")], 1):
            self.users[f"usr-{i:03d}"] = {"id": f"usr-{i:03d}", "name": n, "email": e, "role": r, "team": team,
                                         "status": "active", "created_at": iso(t - timedelta(days=200 - i * 9)),
                                         "last_login_at": iso(t - timedelta(hours=i * 5))}
        self._user_seq = 6
        self.settings = {"program_budget_pct_of_total_cost": 60, "default_alpha": 1.0, "default_budget_pct": 25,
                         "audit_retention_days": 730, "auto_rollback_enabled": True,
                         "require_change_request": True, "block_self_approval": True,
                         "maintenance_window_default_hours": 3, "timezone": "Asia/Kolkata"}
        self.channels = {
            "chn-001": {"id": "chn-001", "type": "email", "name": "Security Ops DL", "target": "secops@qsmo.example",
                        "events": ["alert.critical", "alert.high", "change_request.pending"], "enabled": True},
            "chn-002": {"id": "chn-002", "type": "slack", "name": "#qsmo-alerts", "target": "https://hooks.slack.com/services/T000/B000/XXXX",
                        "events": ["alert.critical", "plan.completed", "migration.reverted"], "enabled": True},
            "chn-003": {"id": "chn-003", "type": "webhook", "name": "SIEM ingest", "target": "https://siem.example.internal/ingest/qsmo",
                        "events": ["audit.*"], "enabled": False}}
        self._ch_seq = 3
        self.api_keys = {"key-001": {"id": "key-001", "name": "Grafana exporter", "prefix": "qsmo_live_8f3a",
                                     "hash": hashlib.sha256(b"seed1").hexdigest(), "scopes": ["devices:read", "monitoring:read"],
                                     "created_by": "priya.raman@qsmo.example", "created_at": iso(t - timedelta(days=60)),
                                     "last_used_at": iso(t - timedelta(minutes=14)), "revoked": False}}
        self._key_seq = 1
        self.sso = {"enabled": False, "provider": "oidc", "issuer_url": "", "client_id": "", "client_secret_set": False,
                    "default_role": "Auditor", "group_role_mapping": {}, "note": "Placeholder - not enforced; API runs without authentication."}
        self.schedules = {"sch-001": {"id": "sch-001", "name": "Monthly compliance pack", "frequency": "monthly", "time": "06:00",
                                      "format": "pdf", "scope": {"env": "prod", "site": None},
                                      "recipients": ["ciso@qsmo.example", "grc@qsmo.example"], "enabled": True,
                                      "last_run_at": iso(t - timedelta(days=9)), "created_by": "priya.raman@qsmo.example"}}
        self._sch_seq = 1
        self.report_history = []
        self._rep_seq = 0

    def next_run(self, sch):
        t = now()
        hh, mm = (int(x) for x in sch["time"].split(":"))
        base = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        add = {"daily": 1, "weekly": 7, "monthly": 30}[sch["frequency"]]
        last = parse_dt(sch["last_run_at"]) if sch.get("last_run_at") else None
        nxt = (last + timedelta(days=add)).replace(hour=hh, minute=mm) if last else base
        return iso(nxt if nxt > t else base + timedelta(days=1))


GLOBAL = Globals()
