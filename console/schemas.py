"""Request bodies (pydantic). Responses are plain dicts - see /docs for live examples."""
from typing import List, Optional, Literal

from pydantic import BaseModel, Field

Strategy = Literal["sgbm", "simple_greedy", "sequential", "random", "manual"]
Role = Literal["Admin", "Operator", "Auditor"]


class DryRunRequest(BaseModel):
    strategy: Strategy = "sgbm"
    budget: Optional[float] = Field(None, gt=0, description="Absolute budget in cost units")
    budget_pct: Optional[float] = Field(None, gt=0, le=100, description="Budget as % of total migration cost")
    alpha: float = Field(1.0, gt=0, le=3, description="SGBM score exponent: gain / cost^alpha")
    device_ids: Optional[List[str]] = Field(None, description="Required for strategy=manual (IDs or names)")


class PlanCreate(DryRunRequest):
    name: str = Field(..., min_length=3, max_length=120)
    notes: Optional[str] = None


class PlanUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    strategy: Optional[Strategy] = None
    budget: Optional[float] = Field(None, gt=0)
    budget_pct: Optional[float] = Field(None, gt=0, le=100)
    alpha: Optional[float] = Field(None, gt=0, le=3)
    device_ids: Optional[List[str]] = None
    notes: Optional[str] = None


class MaintenanceWindow(BaseModel):
    start: str
    end: str


class SubmitRequest(BaseModel):
    maintenance_window: MaintenanceWindow
    justification: str = Field(..., min_length=10)
    rollback_plan: str = Field(..., min_length=5)


class CRCreate(SubmitRequest):
    plan_id: str


class Decision(BaseModel):
    comment: Optional[str] = None


class RejectRequest(BaseModel):
    comment: str = Field(..., min_length=3)


class CommentIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ConfirmPlanRollback(BaseModel):
    confirm_plan_id: str
    reason: str = Field(..., min_length=5)


class RollbackRequest(BaseModel):
    confirm_switch_id: str = Field(..., description="Type the switch name or dpid to confirm")
    reason: str = Field(..., min_length=5)


class PolicyUpdate(BaseModel):
    tau_latency_ms: Optional[float] = Field(None, gt=0, le=500)
    tau_failure: Optional[float] = Field(None, gt=0, le=1)
    kappa_retries: Optional[int] = Field(None, ge=1, le=20)
    poll_interval_s: Optional[int] = Field(None, ge=5, le=3600)
    cooldown_s: Optional[int] = Field(None, ge=5, le=3600)


class FlowInputsUpdate(BaseModel):
    impact_confidentiality: Optional[Literal["Low", "Moderate", "High"]] = None
    impact_integrity: Optional[Literal["Low", "Moderate", "High"]] = None
    impact_availability: Optional[Literal["Low", "Moderate", "High"]] = None
    bandwidth_mbps: Optional[float] = Field(None, gt=0)
    q_delay_ms: Optional[float] = Field(None, gt=0)


class DeviceTags(BaseModel):
    tags: List[str]


class BulkIds(BaseModel):
    ids: List[str] = Field(..., min_length=1)


class BulkExport(BulkIds):
    format: Literal["csv", "json"] = "csv"


class BulkTag(BulkIds):
    tags: List[str]


class BulkPlan(BulkIds):
    name: str = Field(..., min_length=3)
    notes: Optional[str] = None


class ReportRequest(BaseModel):
    format: Literal["pdf", "csv", "json"] = "pdf"
    env: str = "prod"
    site: Optional[str] = None
    title: Optional[str] = None


class ScheduleIn(BaseModel):
    name: str = Field(..., min_length=3)
    frequency: Literal["daily", "weekly", "monthly"]
    time: str = Field("06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    format: Literal["pdf", "csv", "json"] = "pdf"
    env: str = "prod"
    site: Optional[str] = None
    recipients: List[str] = Field(..., min_length=1)
    enabled: bool = True


class SettingsUpdate(BaseModel):
    program_budget_pct_of_total_cost: Optional[float] = Field(None, gt=0, le=100)
    default_alpha: Optional[float] = Field(None, gt=0, le=3)
    default_budget_pct: Optional[float] = Field(None, gt=0, le=100)
    audit_retention_days: Optional[int] = Field(None, ge=30, le=3650)
    auto_rollback_enabled: Optional[bool] = None
    require_change_request: Optional[bool] = None
    block_self_approval: Optional[bool] = None
    maintenance_window_default_hours: Optional[int] = Field(None, ge=1, le=24)
    timezone: Optional[str] = None


class ChannelIn(BaseModel):
    type: Literal["email", "slack", "webhook"]
    name: str = Field(..., min_length=2)
    target: str = Field(..., min_length=3)
    events: List[str] = Field(default_factory=lambda: ["alert.critical"])
    enabled: bool = True


class ApiKeyIn(BaseModel):
    name: str = Field(..., min_length=2)
    scopes: List[str] = Field(default_factory=lambda: ["devices:read"])


class SsoUpdate(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[Literal["oidc", "saml"]] = None
    issuer_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    default_role: Optional[Role] = None
    group_role_mapping: Optional[dict] = None


class UserCreate(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Role = "Auditor"
    team: Optional[str] = None


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2)
    role: Optional[Role] = None
    team: Optional[str] = None
    status: Optional[Literal["active", "disabled"]] = None
