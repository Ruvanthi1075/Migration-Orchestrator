# QSMO Console API - endpoint reference

Base URL `http://localhost:8000`  |  Interactive docs: `/docs`  |  No authentication.

- Every data endpoint accepts `?env=prod|staging|lab` (default `prod`).
- Optional `X-Actor: <email>` header sets who is recorded in the audit trail.
- Lists accept `page`, `page_size` and return `{items,total,page,page_size,pages}`.

## 0. App shell

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/alerts` | Alerts list |
| POST | `/api/v1/alerts/bulk` | Bulk acknowledge / resolve |
| GET | `/api/v1/alerts/summary` | Bell badge counts |
| POST | `/api/v1/alerts/{alert_id}/acknowledge` | Acknowledge an alert |
| POST | `/api/v1/alerts/{alert_id}/resolve` | Resolve an alert |
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/meta` | Environment / site switchers, enums, defaults |
| GET | `/api/v1/search` | Global search: switches, flows, plans, change requests |
| POST | `/api/v1/sync` | Refresh from the controller (LLDP re-discovery) - simulated here |
| GET | `/api/v1/sync/status` | 'Last synced' indicator |

## 1. Overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/overview` | Dashboard payload: KPIs, trend, at-risk flows, activity |
| GET | `/api/v1/overview/coverage-trend` | Daily weighted coverage for the last N days (max 30) |
| GET | `/api/v1/overview/recent-activity` | Latest audit events |

## 10. Audit Log (MML)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/audit` | Audit table (append-only; no create/update/delete endpoints exist) |
| GET | `/api/v1/audit/export` | Export the filtered log (csv | json) |
| GET | `/api/v1/audit/facets` | Distinct actors / actions / outcomes for the filter bar |
| GET | `/api/v1/audit/verify` | Verify the hash chain (tamper evidence) |
| GET | `/api/v1/audit/{event_id}` | Event detail (drawer with raw JSON) |

## 11. Settings

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/settings` | General settings |
| PUT | `/api/v1/settings` | Update general settings (partial) |
| GET | `/api/v1/settings/api-keys` | API keys (secrets are never returned after creation) |
| POST | `/api/v1/settings/api-keys` | Create an API key - the secret is returned ONCE |
| DELETE | `/api/v1/settings/api-keys/{kid}` | Revoke an API key |
| GET | `/api/v1/settings/channels` | Notification channels (email / Slack / webhook) |
| POST | `/api/v1/settings/channels` | Add a channel |
| PUT | `/api/v1/settings/channels/{cid}` | Update a channel |
| DELETE | `/api/v1/settings/channels/{cid}` | Delete a channel |
| POST | `/api/v1/settings/channels/{cid}/test` | Send a test notification (simulated) |
| GET | `/api/v1/settings/sso` | SSO configuration (placeholder) |
| PUT | `/api/v1/settings/sso` | Update SSO configuration (placeholder; not enforced) |

## 12. Users & Roles

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/me` | Current user + permissions, resolved from the X-Actor header (no auth) |
| GET | `/api/v1/roles` | Roles and permission matrix (UI uses it to show/hide buttons) |
| GET | `/api/v1/users` | User list |
| POST | `/api/v1/users` | Create a user |
| GET | `/api/v1/users/{uid}` | User detail |
| PUT | `/api/v1/users/{uid}` | Update a user (role / status / name) |
| DELETE | `/api/v1/users/{uid}` | Delete a user |

## 2-4. Topology, Devices, Flows

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/devices` | Inventory table: filter / sort / paginate |
| POST | `/api/v1/devices/bulk/export` | Export selected devices |
| POST | `/api/v1/devices/bulk/plan` | Create a Draft plan (strategy=manual) from selected devices |
| POST | `/api/v1/devices/bulk/tag` | Add tags to selected devices |
| GET | `/api/v1/devices/export` | Export the filtered inventory (csv|json) |
| GET | `/api/v1/devices/{device_id}` | Device summary tab |
| PATCH | `/api/v1/devices/{device_id}` | Update device tags |
| GET | `/api/v1/devices/{device_id}/crypto` | Crypto tab: cipher suite, KEM, certificate |
| GET | `/api/v1/devices/{device_id}/flows` | Flows tab: flows whose protection path includes this device |
| GET | `/api/v1/devices/{device_id}/history` | History tab: migration / rollback events for the device |
| GET | `/api/v1/devices/{device_id}/metrics` | Metrics tab: latency / failure time series |
| GET | `/api/v1/flows` | Flows table (weight, path length, blocking switches, status) |
| GET | `/api/v1/flows/summary` | Protection summary by service and site |
| GET | `/api/v1/flows/{flow_id}` | Flow detail: weight breakdown, path, blocking switches |
| PATCH | `/api/v1/flows/{flow_id}` | Edit flow inputs (impact levels, bandwidth, delay budget); weights are recomputed |
| GET | `/api/v1/topology` | Graph for the topology view (nodes + edges), filterable |
| GET | `/api/v1/topology/summary` | Per-site counts for the filter bar |

## 5. Migration Plans

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/plans` | Plan list |
| POST | `/api/v1/plans` | Create a Draft plan (runs the dry-run and stores the schedule) |
| GET | `/api/v1/plans/budget-info` | Cost units: total migration cost, per-role costs, program budget |
| POST | `/api/v1/plans/dry-run` | Wizard step 3: preview schedule, per-step gain and projected coverage curve |
| GET | `/api/v1/plans/strategies` | Strategies + statuses for the wizard |
| GET | `/api/v1/plans/{plan_id}` | Plan detail (steps, linked change request, progress) |
| PUT | `/api/v1/plans/{plan_id}` | Edit a Draft plan (re-runs the dry-run) |
| DELETE | `/api/v1/plans/{plan_id}` | Delete a Draft / Cancelled plan |
| POST | `/api/v1/plans/{plan_id}/cancel` | Cancel a plan (stops a running one after the current switch) |
| POST | `/api/v1/plans/{plan_id}/dry-run` | Re-run the dry-run against the current network state |
| POST | `/api/v1/plans/{plan_id}/execute` | Start execution (needs an Approved change request) |
| GET | `/api/v1/plans/{plan_id}/progress` | Live progress (poll every 1-2 s): GME success/fail per switch |
| POST | `/api/v1/plans/{plan_id}/rollback` | Roll back every switch a Completed plan migrated (typed confirmation) |
| POST | `/api/v1/plans/{plan_id}/submit` | Wizard step 5: submit -> creates a Change Request (Pending approval) |

## 6. Change Requests

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/change-requests` | Change request list |
| POST | `/api/v1/change-requests` | Create a change request for a Draft plan (same as plan submit) |
| GET | `/api/v1/change-requests/{cr_id}` | Change request detail (+ linked plan summary) |
| POST | `/api/v1/change-requests/{cr_id}/approve` | Approve (self-approval blocked unless disabled in settings) |
| POST | `/api/v1/change-requests/{cr_id}/cancel` | Withdraw a Pending / Approved change request |
| POST | `/api/v1/change-requests/{cr_id}/comments` | Add a comment |
| POST | `/api/v1/change-requests/{cr_id}/reject` | Reject (plan returns to Draft) |

## 7. Monitoring & Rollback

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/monitoring/degraded` | Degraded queue with retry counters and recheck countdown |
| POST | `/api/v1/monitoring/devices/{device_id}/clear` | Mark a degraded switch stable again (operator override) |
| POST | `/api/v1/monitoring/devices/{device_id}/recheck` | Force an immediate recheck (may trigger auto-rollback at kappa) |
| POST | `/api/v1/monitoring/devices/{device_id}/rollback` | Manual rollback to Legacy (typed switch ID confirmation + reason) |
| POST | `/api/v1/monitoring/devices/{device_id}/simulate-degradation` | DEMO helper: push a Hybrid switch into the degraded queue |
| GET | `/api/v1/monitoring/live` | Charts: latency/failure series with baseline and tau thresholds |
| GET | `/api/v1/monitoring/policy` | Threshold policy: tau_L, tau_F, kappa, delta_t, delta_c |
| PUT | `/api/v1/monitoring/policy` | Update threshold policy (partial update) |
| GET | `/api/v1/monitoring/summary` | Header cards for the monitoring page |

## 8. Comparison

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/comparison/coverage-curve` | Coverage vs budget curves for every strategy (Fig.-2 style) |
| GET | `/api/v1/comparison/devices` | Per-device before/after deltas (Hybrid switches) |
| GET | `/api/v1/comparison/strategies` | Table-I style comparison of all strategies at one budget |
| GET | `/api/v1/comparison/tls` | Legacy vs Hybrid: latency, failure rate, handshake size, CPU (mean + p50/p95/p99) |

## 9. Reports

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/reports/compliance` | Compliance report as JSON (on-screen preview) |
| GET | `/api/v1/reports/compliance/export` | Download the compliance report (pdf | csv | json) |
| POST | `/api/v1/reports/generate` | Generate + store a report; returns metadata and a download URL |
| GET | `/api/v1/reports/history` | Generated reports |
| GET | `/api/v1/reports/history/{report_id}/download` | Download a stored report |
| GET | `/api/v1/reports/schedules` | Scheduled reports |
| POST | `/api/v1/reports/schedules` | Create a schedule |
| PUT | `/api/v1/reports/schedules/{sid}` | Update a schedule |
| DELETE | `/api/v1/reports/schedules/{sid}` | Delete a schedule |
| POST | `/api/v1/reports/schedules/{sid}/run` | Run a schedule now |
