"""
QSMO migration health monitoring and rollback.

Responsibilities:
- Monitor migrated switches for degradation.
- Detect latency, failure-rate, and connectivity problems.
- Roll back an affected switch to Legacy when required.
- Record monitoring and rollback outcomes in the migration ledger.

The monitor must not modify optimizer candidate selection.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional
from migration.migrate_link import local_port_for_dpid


class MonitorConfig:
    """Configuration for migration health monitoring."""

    def __init__(
        self,
        poll_interval: float = 2.0,
        latency_threshold_ms: Optional[float] = None,
        failure_rate_threshold: Optional[float] = None,
        consecutive_degraded_observations: int = 3,
    ):
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

        if latency_threshold_ms is not None and latency_threshold_ms < 0:
            raise ValueError("latency_threshold_ms cannot be negative")

        if failure_rate_threshold is not None:
            if not 0.0 <= failure_rate_threshold <= 1.0:
                raise ValueError(
                    "failure_rate_threshold must be between 0.0 and 1.0"
                )

        if (
            isinstance(consecutive_degraded_observations, bool)
            or not isinstance(consecutive_degraded_observations, int)
            or consecutive_degraded_observations < 1
        ):
            raise ValueError(
                'consecutive_degraded_observations must be a positive integer'
            )

        self.poll_interval = poll_interval
        self.latency_threshold_ms = latency_threshold_ms
        self.failure_rate_threshold = failure_rate_threshold
        self.consecutive_degraded_observations = (
            consecutive_degraded_observations
        )


class MigrationMonitor:
    """Monitor migrated switches and coordinate safe rollback."""

    def __init__(
        self,
        executor,
        config: Optional[MonitorConfig] = None,
        logger=None,
        metric_provider=None,
    ):
        self.executor = executor
        self.topology = executor.topology
        self.ledger = executor.ledger
        self.runner = executor.runner

        self.config = config or MonitorConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.metric_provider = metric_provider
        self._degraded_counts = {}
        self._rollback_attempted = set()
        self._stop_event = threading.Event()
        self._thread = None

    def evaluate_health(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate supplied health metrics without changing network state.

        Returns a health result containing the detected degradation reasons.
        """
        reasons = []

        latency = metrics.get("latency_ms")
        failure_rate = metrics.get("failure_rate")

        if latency is None:
            reasons.append("latency measurement unavailable")
        elif self.config.latency_threshold_ms is not None:
            if latency > self.config.latency_threshold_ms:
                reasons.append(
                    f"latency {latency} ms exceeds configured threshold "
                    f"{self.config.latency_threshold_ms} ms"
                )

        if failure_rate is None:
            reasons.append("failure-rate measurement unavailable")
        elif self.config.failure_rate_threshold is not None:
            if failure_rate > self.config.failure_rate_threshold:
                reasons.append(
                    f"failure rate {failure_rate} exceeds configured threshold "
                    f"{self.config.failure_rate_threshold}"
                )

        degraded = any(
            "exceeds configured threshold" in reason for reason in reasons
        )

        if degraded:
            status = "degraded"
        elif reasons:
            status = "unknown"
        else:
            status = "healthy"

        return {
            "status": status,
            "healthy": status == "healthy",
            "degraded": status == "degraded",
            "reasons": reasons,
        }

    def build_switch_health_report(
        self,
        dpid: str,
        connectivity: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine supplied connectivity and metric observations."""
        health = self.evaluate_health(metrics)
        connection_status = connectivity.get("status", "unknown")
        reasons = list(health["reasons"])

        if connection_status == "disconnected":
            reasons.append("Hybrid controller disconnected")
            status = "degraded"
        elif connection_status == "observation_error":
            reasons.append(
                connectivity.get("reason", "connectivity observation failed")
            )
            status = (
                "degraded" if health["degraded"] else "unknown"
            )
        elif connection_status != "connected":
            reasons.append("connectivity measurement unavailable")
            status = (
                "degraded" if health["degraded"] else "unknown"
            )
        else:
            status = health["status"]

        return {
            "dpid": dpid,
            "state": connectivity.get("state", "Hybrid"),
            "connectivity": connection_status,
            "latency_ms": metrics.get("latency_ms"),
            "failure_rate": metrics.get("failure_rate"),
            "status": status,
            "healthy": status == "healthy",
            "degraded": status == "degraded",
            "reasons": reasons,
        }

    def is_switch_hybrid(self, dpid: str) -> bool:
        """
        Check whether a switch is currently recorded as Hybrid.

        This method only reads topology state.
        It does not change the switch configuration.
        """
        return self.topology.get_state(dpid) == "Hybrid"
    def resolve_switch_name(self, dpid: str) -> str:
        """
        Resolve the actual OVS bridge name for a switch.

        Reuses the migration executor's existing discovery method.
        """
        return self.executor._resolve_switch_name(dpid)
    def is_hybrid_connected(self, dpid: str) -> bool:
        """
        Check whether the switch is connected to its Hybrid controller.

        This method only reads OVS connectivity status.
        It does not change the switch configuration.
        """
        if not self.is_switch_hybrid(dpid):
            return False

        switch_name = self.resolve_switch_name(dpid)
        local_port = local_port_for_dpid(dpid)
        hybrid_target = f"tcp:127.0.0.1:{local_port}"

        return self.executor._controller_target_connected(
            switch_name, hybrid_target
        )
    def observe_hybrid_switches(self):
        """Return a read-only connectivity snapshot of Hybrid switches."""
        graph = self.topology.get_graph()
        observations = []

        for dpid in graph.nodes:
            if not self.is_switch_hybrid(dpid):
                continue

            try:
                connected = self.is_hybrid_connected(dpid)

                observations.append({
                    "dpid": dpid,
                    "state": "Hybrid",
                    "connected": connected,
                    "status": (
                        "connected" if connected else "disconnected"
                    ),
                })

            except Exception as exc:
                observations.append({
                    "dpid": dpid,
                    "state": "Hybrid",
                    "connected": None,
                    "status": "observation_error",
                    "reason": str(exc),
                })

        return observations
    def collect_metrics(self, dpid: str) -> Dict[str, Any]:
        """Read metrics from an injected provider, without network side effects."""
        if self.metric_provider is None:
            return {}

        metrics = self.metric_provider(dpid)

        if not isinstance(metrics, dict):
            raise TypeError("metric_provider must return a dictionary")

        return metrics

    def monitor_once(self):
        """Collect and log read-only per-switch health reports."""
        observations = self.observe_hybrid_switches()
        reports = []

        for observation in observations:
            dpid = observation.get("dpid")
            try:
                metrics = self.collect_metrics(dpid)
            except Exception as exc:
                metrics = {}
                observation = dict(observation)
                existing_reason = observation.get("reason")
                metric_reason = f"metric collection failed: {exc}"
                observation["reason"] = (
                    f"{existing_reason}; {metric_reason}"
                    if existing_reason else metric_reason
                )

                if observation.get("status") == "connected":
                    observation["status"] = "metric_observation_error"

            report_connectivity = dict(observation)
            if report_connectivity.get("status") == "metric_observation_error":
                report_connectivity["status"] = "connected"

            report = self.build_switch_health_report(
                dpid,
                report_connectivity,
                metrics,
            )

            if observation.get("status") == "metric_observation_error":
                report["status"] = (
                    "degraded" if report["degraded"] else "unknown"
                )
                report["healthy"] = report["status"] == "healthy"
                report["degraded"] = report["status"] == "degraded"
                report["reasons"].append(observation["reason"])
            # Count consecutive confirmed degradation per switch.
            if dpid is not None:
                counts = getattr(self, '_degraded_counts', None)
                if counts is None:
                    counts = self._degraded_counts = {}
                attempted = getattr(self, '_rollback_attempted', None)
                if attempted is None:
                    attempted = self._rollback_attempted = set()

                if report.get('status') == 'degraded':
                    count = counts.get(dpid, 0) + 1
                    counts[dpid] = count
                    config = getattr(self, 'config', None)
                    limit = getattr(
                        config, 'consecutive_degraded_observations', 3
                    )
                    if count >= limit and dpid not in attempted:
                        attempted.add(dpid)
                        reason = '; '.join(report.get('reasons', []))
                        try:
                            report['recovery'] = self.rollback(
                                dpid, reason or 'Repeated health degradation'
                            )
                        except Exception as exc:
                            self.logger.exception(
                                'Automatic rollback attempt failed for %s', dpid
                            )
                            report['recovery'] = {
                                'dpid': dpid,
                                'outcome': 'failed',
                                'reason': str(exc),
                            }
                else:
                    counts[dpid] = 0
                    attempted.discard(dpid)

            reports.append(report)

            if report["status"] == "degraded":
                self.logger.warning(
                    "Migration health degraded for switch %s: %s",
                    dpid,
                    report["reasons"],
                )
            elif report["status"] == "unknown":
                self.logger.warning(
                    "Migration health unknown for switch %s: %s",
                    dpid,
                    report["reasons"],
                )
            else:
                self.logger.info(
                    "Migration health healthy for switch %s",
                    dpid,
                )

        self.logger.info("Migration health reports: %s", reports)
        return reports

    def _monitor_loop(self):
        """Repeat read-only health observations until stopped."""
        self.logger.info("Continuous migration monitoring started")

        while not self._stop_event.is_set():
            try:
                self.monitor_once()
            except Exception:
                self.logger.exception("Migration monitoring cycle failed")

            self._stop_event.wait(self.config.poll_interval)

        self.logger.info("Continuous migration monitoring stopped")

    def start(self):
        """Start continuous observation without changing network state."""
        if self._thread is not None and self._thread.is_alive():
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="migration-health-monitor",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: Optional[float] = None):
        """Request monitoring shutdown and wait for the worker to finish."""
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()

        return True

    def decide_recovery(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """Classify a health report without executing recovery actions."""
        status = report.get("status", "unknown")
        dpid = report.get("dpid")

        if status == "degraded":
            return {
                "dpid": dpid,
                "decision": "rollback_candidate",
                "action_taken": False,
                "reasons": list(report.get("reasons", [])),
            }

        if status == "healthy":
            decision = "no_action"
        else:
            decision = "unknown"

        return {
            "dpid": dpid,
            "decision": decision,
            "action_taken": False,
            "reasons": list(report.get("reasons", [])),
        }

    def rollback(
        self,
        dpid: str,
        reason: str,
        associated_flows=None,
    ) -> Dict[str, Any]:
        """
        Restore a degraded Hybrid switch to Legacy.

        Topology and ledger are updated only after OVS confirms
        that the Legacy controller is connected.
        """
        if not self.is_switch_hybrid(dpid):
            return {
                "dpid": dpid,
                "outcome": "not_rolled_back",
                "reason": "switch is not currently Hybrid",
            }

        history = self.ledger.get_history(dpid)

        successful_events = [
            event for event in history
            if event["outcome"] == "success"
        ]

        if not successful_events:
            return {
                "dpid": dpid,
                "outcome": "failed",
                "reason": "no successful migration record found",
            }

        migration_event = successful_events[-1]
        baseline = migration_event["baseline"]
        post = migration_event["post"]

        switch_name = self.resolve_switch_name(dpid)
        legacy_target = (
            f"ssl:{self.executor.controller_ip}:"
            f"{self.executor.legacy_port}"
        )

        try:
            rc, out, err = self.executor._set_controller_target(
                switch_name,
                legacy_target,
            )

            if rc != 0:
                error = (err or out).strip()
                return {
                    "dpid": dpid,
                    "outcome": "failed",
                    "reason": f"Legacy controller configuration failed: {error}",
                }

            connected = self.executor._controller_target_connected(
                switch_name,
                legacy_target,
            )

            if not connected:
                return {
                    "dpid": dpid,
                    "outcome": "failed",
                    "reason": "Legacy controller connection was not verified",
                }

            self.topology.set_state(dpid, "Legacy")

            self.ledger.log_event(
                dpid,
                baseline,
                post,
                outcome="reverted",
                reason=reason,
                associated_flows=associated_flows,
            )

            self.logger.warning(
                "Rolled back switch %s (%s) to Legacy: %s",
                dpid,
                switch_name,
                reason,
            )

            return {
                "dpid": dpid,
                "outcome": "reverted",
                "reason": reason,
            }

        except Exception as exc:
            self.logger.exception(
                "Rollback failed for switch %s",
                dpid,
            )

            return {
                "dpid": dpid,
                "outcome": "failed",
                "reason": str(exc),
            }
