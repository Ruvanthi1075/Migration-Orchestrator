import hashlib
import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from console.common import csv_response, get_actor, get_env
from console.schemas import (ApiKeyIn, ChannelIn, SettingsUpdate, SsoUpdate, UserCreate, UserUpdate)
from console.state import GLOBAL, ROLE_PERMISSIONS, Conflict, env_store, iso, now, paginate, parse_dt

audit_router = APIRouter(prefix="/api/v1/audit", tags=["10. Audit Log (MML)"])
set_router = APIRouter(prefix="/api/v1/settings", tags=["11. Settings"])
user_router = APIRouter(prefix="/api/v1", tags=["12. Users & Roles"])

AUDIT_COLS = ["id", "ts", "actor", "action", "outcome", "switch_id", "switch_name", "reason", "state_before",
              "state_after", "plan_id", "associated_flows", "hash"]


def _audit_log(actor, action, details=None):
    env_store("prod").log(action, actor, "success", details=details)


# ------------------------------ audit (read-only, hash-chained) ------------------------------
def _filter(env, actor, action, outcome, switch, plan_id, q, date_from, date_to):
    rows = env.audit
    if actor:
        rows = [e for e in rows if actor.lower() in e["actor"].lower()]
    if action:
        rows = [e for e in rows if (e["action"].startswith(action[:-1]) if action.endswith("*") else e["action"] == action)]
    if outcome:
        rows = [e for e in rows if e["outcome"] == outcome]
    if switch:
        rows = [e for e in rows if switch in (e["switch_id"], e["switch_name"])]
    if plan_id:
        rows = [e for e in rows if e["plan_id"] == plan_id]
    if date_from:
        f = iso(parse_dt(date_from)); rows = [e for e in rows if e["ts"] >= f]
    if date_to:
        t = iso(parse_dt(date_to)); rows = [e for e in rows if e["ts"] <= t]
    if q:
        rows = [e for e in rows if q.lower() in json.dumps(e, default=str).lower()]
    return rows


@audit_router.get("", summary="Audit table (append-only; no create/update/delete endpoints exist)")
def audit(actor: Optional[str] = None, action: Optional[str] = Query(None, description="exact, or prefix with * e.g. migration.*"),
          outcome: Optional[str] = Query(None, description="success|failed|reverted|info"), switch: Optional[str] = None,
          plan_id: Optional[str] = None, q: Optional[str] = None, date_from: Optional[str] = Query(None, alias="from"),
          date_to: Optional[str] = Query(None, alias="to"), order: str = Query("desc", pattern="^(asc|desc)$"),
          page: int = 1, page_size: int = 50, env=Depends(get_env)):
    rows = _filter(env, actor, action, outcome, switch, plan_id, q, date_from, date_to)
    rows = list(reversed(rows)) if order == "desc" else list(rows)
    res = paginate(rows, page, page_size)
    ok = sum(e["action"] == "migration.success" for e in env.audit)
    rev = sum(e["action"] == "migration.reverted" for e in env.audit)
    res["rollback_rate_pct"] = round(100 * rev / (ok + rev), 2) if ok + rev else 0.0
    return res


@audit_router.get("/facets", summary="Distinct actors / actions / outcomes for the filter bar")
def facets(env=Depends(get_env)):
    return {"actors": sorted({e["actor"] for e in env.audit}), "actions": sorted({e["action"] for e in env.audit}),
            "outcomes": sorted({e["outcome"] for e in env.audit})}


@audit_router.get("/export", summary="Export the filtered log (csv | json)")
def audit_export(format: str = Query("csv", pattern="^(csv|json)$"), actor: Optional[str] = None, action: Optional[str] = None,
                 outcome: Optional[str] = None, switch: Optional[str] = None, plan_id: Optional[str] = None, q: Optional[str] = None,
                 date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                 env=Depends(get_env)):
    rows = _filter(env, actor, action, outcome, switch, plan_id, q, date_from, date_to)
    if format == "json":
        return {"items": rows, "total": len(rows)}
    return csv_response(rows, AUDIT_COLS, f"qsmo-audit-{env.name}.csv")


@audit_router.get("/verify", summary="Verify the hash chain (tamper evidence)")
def verify(env=Depends(get_env)):
    prev = "0" * 64
    for e in env.audit:
        body = json.dumps({k: e[k] for k in sorted(e) if k != "hash"}, sort_keys=True, default=str)
        if e["prev_hash"] != prev or hashlib.sha256(body.encode()).hexdigest() != e["hash"]:
            return {"valid": False, "events": len(env.audit), "first_bad_event": e["id"]}
        prev = e["hash"]
    return {"valid": True, "events": len(env.audit), "head_hash": prev}


@audit_router.get("/{event_id}", summary="Event detail (drawer with raw JSON)")
def audit_event(event_id: str, env=Depends(get_env)):
    e = next((x for x in env.audit if x["id"] == event_id), None)
    if not e:
        raise HTTPException(404, f"event '{event_id}' not found")
    return e


# ------------------------------ settings ------------------------------
@set_router.get("", summary="General settings")
def get_settings():
    return GLOBAL.settings


@set_router.put("", summary="Update general settings (partial)")
def put_settings(body: SettingsUpdate, actor: str = Depends(get_actor)):
    ch = body.model_dump(exclude_none=True)
    diff = {k: [GLOBAL.settings[k], v] for k, v in ch.items() if GLOBAL.settings[k] != v}
    GLOBAL.settings.update(ch)
    if "program_budget_pct_of_total_cost" in ch:
        for name in ("prod", "staging", "lab"):
            try:
                e = env_store(name)
                e.budget_total = round(ch["program_budget_pct_of_total_cost"] / 100 * e.total_cost(), 3)
            except Exception:
                pass
    _audit_log(actor, "settings.updated", diff)
    return GLOBAL.settings


def _mask(ch):
    t = ch["target"]
    if ch["type"] != "email" and len(t) > 24:
        t = t[:24] + "..." + t[-4:]
    return {**ch, "target": t}


@set_router.get("/channels", summary="Notification channels (email / Slack / webhook)")
def channels():
    return {"items": [_mask(c) for c in GLOBAL.channels.values()]}


@set_router.post("/channels", status_code=201, summary="Add a channel")
def add_channel(body: ChannelIn, actor: str = Depends(get_actor)):
    GLOBAL._ch_seq += 1
    cid = f"chn-{GLOBAL._ch_seq:03d}"
    GLOBAL.channels[cid] = {"id": cid, **body.model_dump()}
    _audit_log(actor, "channel.created", {"id": cid, "type": body.type})
    return _mask(GLOBAL.channels[cid])


@set_router.put("/channels/{cid}", summary="Update a channel")
def update_channel(cid: str, body: ChannelIn, actor: str = Depends(get_actor)):
    if cid not in GLOBAL.channels:
        raise HTTPException(404, "channel not found")
    GLOBAL.channels[cid] = {"id": cid, **body.model_dump()}
    _audit_log(actor, "channel.updated", {"id": cid})
    return _mask(GLOBAL.channels[cid])


@set_router.delete("/channels/{cid}", status_code=204, summary="Delete a channel")
def delete_channel(cid: str, actor: str = Depends(get_actor)):
    if cid not in GLOBAL.channels:
        raise HTTPException(404, "channel not found")
    del GLOBAL.channels[cid]
    _audit_log(actor, "channel.deleted", {"id": cid})


@set_router.post("/channels/{cid}/test", summary="Send a test notification (simulated)")
def test_channel(cid: str):
    if cid not in GLOBAL.channels:
        raise HTTPException(404, "channel not found")
    c = GLOBAL.channels[cid]
    return {"ok": c["enabled"], "channel": c["name"], "type": c["type"], "latency_ms": 118,
            "message": "Test notification delivered (simulated)" if c["enabled"] else "Channel is disabled"}


@set_router.get("/api-keys", summary="API keys (secrets are never returned after creation)")
def api_keys():
    return {"items": [{k: v for k, v in key.items() if k != "hash"} for key in GLOBAL.api_keys.values()]}


@set_router.post("/api-keys", status_code=201, summary="Create an API key - the secret is returned ONCE")
def create_key(body: ApiKeyIn, actor: str = Depends(get_actor)):
    GLOBAL._key_seq += 1
    kid = f"key-{GLOBAL._key_seq:03d}"
    secret = "qsmo_live_" + secrets.token_urlsafe(32)
    GLOBAL.api_keys[kid] = {"id": kid, "name": body.name, "prefix": secret[:14], "hash": hashlib.sha256(secret.encode()).hexdigest(),
                            "scopes": body.scopes, "created_by": actor, "created_at": iso(now()), "last_used_at": None, "revoked": False}
    _audit_log(actor, "api_key.created", {"id": kid, "scopes": body.scopes})
    return {**{k: v for k, v in GLOBAL.api_keys[kid].items() if k != "hash"}, "secret": secret,
            "warning": "Store this secret now; it cannot be shown again."}


@set_router.delete("/api-keys/{kid}", summary="Revoke an API key")
def revoke_key(kid: str, actor: str = Depends(get_actor)):
    if kid not in GLOBAL.api_keys:
        raise HTTPException(404, "api key not found")
    GLOBAL.api_keys[kid]["revoked"] = True
    _audit_log(actor, "api_key.revoked", {"id": kid})
    return {k: v for k, v in GLOBAL.api_keys[kid].items() if k != "hash"}


@set_router.get("/sso", summary="SSO configuration (placeholder)")
def get_sso():
    return GLOBAL.sso


@set_router.put("/sso", summary="Update SSO configuration (placeholder; not enforced)")
def put_sso(body: SsoUpdate, actor: str = Depends(get_actor)):
    ch = body.model_dump(exclude_none=True)
    if "client_secret" in ch:
        ch.pop("client_secret")
        GLOBAL.sso["client_secret_set"] = True
    GLOBAL.sso.update(ch)
    _audit_log(actor, "sso.updated", {"enabled": GLOBAL.sso["enabled"], "provider": GLOBAL.sso["provider"]})
    return GLOBAL.sso


# ------------------------------ users & roles ------------------------------
def _admins():
    return [u for u in GLOBAL.users.values() if u["role"] == "Admin" and u["status"] == "active"]


@user_router.get("/roles", summary="Roles and permission matrix (UI uses it to show/hide buttons)")
def roles():
    return {"roles": [{"role": r, "permissions": p, "users": sum(u["role"] == r for u in GLOBAL.users.values())}
                      for r, p in ROLE_PERMISSIONS.items()]}


@user_router.get("/me", summary="Current user + permissions, resolved from the X-Actor header (no auth)")
def me(actor: str = Depends(get_actor)):
    u = next((x for x in GLOBAL.users.values() if x["email"] == actor), None)
    role = u["role"] if u else "Admin"
    return {"user": u, "role": role, "permissions": ROLE_PERMISSIONS[role], "authenticated": False,
            "note": "No authentication: identity is whatever X-Actor says. Permissions are advisory for the UI."}


@user_router.get("/users", summary="User list")
def users(role: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None, page: int = 1, page_size: int = 25):
    rows = [u for u in GLOBAL.users.values() if (not role or u["role"] == role) and (not status or u["status"] == status)
            and (not q or q.lower() in f"{u['name']} {u['email']}".lower())]
    return paginate(rows, page, page_size)


@user_router.post("/users", status_code=201, summary="Create a user")
def create_user(body: UserCreate, actor: str = Depends(get_actor)):
    if any(u["email"].lower() == body.email.lower() for u in GLOBAL.users.values()):
        raise Conflict(f"a user with email {body.email} already exists")
    GLOBAL._user_seq += 1
    uid = f"usr-{GLOBAL._user_seq:03d}"
    GLOBAL.users[uid] = {"id": uid, "name": body.name, "email": body.email, "role": body.role, "team": body.team,
                         "status": "active", "created_at": iso(now()), "last_login_at": None}
    _audit_log(actor, "user.created", {"id": uid, "role": body.role})
    return GLOBAL.users[uid]


@user_router.get("/users/{uid}", summary="User detail")
def get_user(uid: str):
    if uid not in GLOBAL.users:
        raise HTTPException(404, "user not found")
    u = GLOBAL.users[uid]
    return {**u, "permissions": ROLE_PERMISSIONS[u["role"]]}


@user_router.put("/users/{uid}", summary="Update a user (role / status / name)")
def update_user(uid: str, body: UserUpdate, actor: str = Depends(get_actor)):
    if uid not in GLOBAL.users:
        raise HTTPException(404, "user not found")
    u = GLOBAL.users[uid]
    ch = body.model_dump(exclude_none=True)
    would_lose_admin = u["role"] == "Admin" and (ch.get("role", "Admin") != "Admin" or ch.get("status", "active") != "active")
    if would_lose_admin and len(_admins()) <= 1:
        raise Conflict("cannot demote or disable the last active Admin")
    diff = {k: [u[k], v] for k, v in ch.items() if u[k] != v}
    u.update(ch)
    _audit_log(actor, "user.updated", {"id": uid, **diff})
    return u


@user_router.delete("/users/{uid}", status_code=204, summary="Delete a user")
def delete_user(uid: str, actor: str = Depends(get_actor)):
    if uid not in GLOBAL.users:
        raise HTTPException(404, "user not found")
    if GLOBAL.users[uid]["role"] == "Admin" and len(_admins()) <= 1:
        raise Conflict("cannot delete the last active Admin")
    del GLOBAL.users[uid]
    _audit_log(actor, "user.deleted", {"id": uid})
