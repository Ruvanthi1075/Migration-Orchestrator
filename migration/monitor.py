"""
QSMO dynamic migration health monitoring.

The monitor deliberately contains no topology-specific switch names, DPIDs,
counts, latency values, or fixed migration targets.

Flow for one run:
    1. discover all switches from the live TopologyAdapter
    2. measure a per-switch Legacy latency baseline
    3. SGBM/BBM performs the migration
    4. measure a per-switch Hybrid baseline for migrated switches
    5. compare Legacy vs Hybrid (report only) and keep the Hybrid statistics
       as the primary monitoring baseline
    6. continuously probe Hybrid switches using a rolling window
    7. rollback a switch only after repeated, measured degradation

Runtime health uses two latency checks plus failure/connectivity checks:
    Check 1 (hybrid-baseline): the rolling median must not rise rapidly above
        the switch's own Hybrid baseline.
    Check 2 (legacy-compare):  the rolling median must stay within a large,
        coarse bound over the Legacy mean, so the switch never behaves
        abnormally compared to its pre-migration state.

Either check marks a poll as degraded.  Rollback only happens after
consecutive_degraded_observations consecutive degraded polls.

Latency measurement is always taken from a real probe in the production
path.  The live OS-Ken integration uses OpenFlow Echo Request/Reply RTT for
each discovered datapath.  No latency or failure value is manufactured by
this class.

A small compatibility metric_provider is retained for unit tests only.  The
production path must use latency_probe or the live QSMO echo-probe app.
"""

from __future__ import annotations

from collections import deque
import logging
import math
import statistics
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional

from migration.migrate_link import local_port_for_dpid


LatencyProbe = Callable[[str], Any]


class MonitorConfig:
    """Statistics and policy for dynamic migration monitoring."""

    def __init__(
        self,
        poll_interval: float = 2.0,
        baseline_samples: int = 30,
        warmup_samples: int = 3,
        rolling_window: int = 5,
        failure_window: int = 10,
        failure_threshold: int = 3,
        sigma_multiplier: float = 3.0,
        min_latency_threshold_ms: float = 1.0,
        consecutive_degraded_observations: int = 5,
        degradation_threshold_percent: float = 50.0,
        legacy_threshold_percent: float = 200.0,
        legacy_threshold_floor_ms: float = 5.0,
    ):
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if baseline_samples <= warmup_samples:
            raise ValueError("baseline_samples must be greater than warmup_samples")
        if warmup_samples < 0:
            raise ValueError("warmup_samples cannot be negative")
        if rolling_window < 1:
            raise ValueError("rolling_window must be positive")
        if failure_window < 1:
            raise ValueError("failure_window must be positive")
        if failure_threshold < 1 or failure_threshold > failure_window:
            raise ValueError("failure_threshold must be between 1 and failure_window")
        if sigma_multiplier < 0:
            raise ValueError("sigma_multiplier cannot be negative")
        if min_latency_threshold_ms < 0:
            raise ValueError("min_latency_threshold_ms cannot be negative")
        if (
            isinstance(consecutive_degraded_observations, bool)
            or not isinstance(consecutive_degraded_observations, int)
            or consecutive_degraded_observations < 1
        ):
            raise ValueError("consecutive_degraded_observations must be a positive integer")
        if (
            not isinstance(degradation_threshold_percent, (int, float))
            or not math.isfinite(float(degradation_threshold_percent))
            or degradation_threshold_percent < 0
        ):
            raise ValueError("degradation_threshold_percent must be a non-negative finite number")
        if (
            not isinstance(legacy_threshold_percent, (int, float))
            or not math.isfinite(float(legacy_threshold_percent))
            or legacy_threshold_percent < 0
        ):
            raise ValueError("legacy_threshold_percent must be a non-negative finite number")
        if (
            not isinstance(legacy_threshold_floor_ms, (int, float))
            or not math.isfinite(float(legacy_threshold_floor_ms))
            or legacy_threshold_floor_ms < 0
        ):
            raise ValueError("legacy_threshold_floor_ms must be a non-negative finite number")

        self.poll_interval = poll_interval
        self.baseline_samples = baseline_samples
        self.warmup_samples = warmup_samples
        self.rolling_window = rolling_window
        self.failure_window = failure_window
        self.failure_threshold = failure_threshold
        self.sigma_multiplier = sigma_multiplier
        self.min_latency_threshold_ms = min_latency_threshold_ms
        self.consecutive_degraded_observations = consecutive_degraded_observations
        self.degradation_threshold_percent = float(degradation_threshold_percent)
        self.legacy_threshold_percent = float(legacy_threshold_percent)
        self.legacy_threshold_floor_ms = float(legacy_threshold_floor_ms)


class MigrationMonitor:
    """Dynamic per-switch monitoring and automatic rollback coordinator."""

    def __init__(
        self,
        executor,
        config: Optional[MonitorConfig] = None,
        logger=None,
        latency_probe: Optional[LatencyProbe] = None,
        metric_provider=None,
    ):
        self.executor = executor
        self.topology = executor.topology
        self.ledger = executor.ledger
        self.runner = executor.runner

        self.config = config or MonitorConfig()
        self.logger = logger or logging.getLogger(__name__)

        # Production code must provide a real probe.  A probe can be attached
        # directly to the executor so the monitor does not need topology- or
        # controller-specific knowledge.
        self.latency_probe = latency_probe or getattr(executor, "latency_probe", None)
        self._live_probe = None

        # Kept only so existing unit tests can inject deterministic values.
        # The integrated demo must not use this to manufacture telemetry.
        self.metric_provider = metric_provider

        self._degraded_counts: Dict[str, int] = {}
        self._rollback_attempted = set()
        self._rolling_latencies: Dict[str, deque] = {}
        self._rolling_failures: Dict[str, deque] = {}
        self._baselines: Dict[str, Dict[str, Any]] = {}
        self._post_migration: Dict[str, Dict[str, Any]] = {}
        self._migration_report: Dict[str, Dict[str, Any]] = {}
        self._stop_event = threading.Event()
        self._thread = None

    def bind_latency_probe(self, probe: LatencyProbe) -> None:
        """Attach the real per-switch probe used by the live integration."""
        if not callable(probe):
            raise TypeError("probe must be callable")
        self.latency_probe = probe

    # ------------------------------------------------------------------
    # Dynamic topology discovery
    # ------------------------------------------------------------------

    def discover_switches(self, states: Optional[Iterable[str]] = None) -> list[str]:
        """Return DPIDs currently present in the live topology graph."""
        graph = self.topology.get_graph()
        allowed = set(states) if states is not None else None
        return [
            dpid for dpid in graph.nodes
            if allowed is None or self.topology.get_state(dpid) in allowed
        ]

    def discover_legacy_switches(self) -> list[str]:
        return self.discover_switches(states=("Legacy",))

    def discover_hybrid_switches(self) -> list[str]:
        return self.discover_switches(states=("Hybrid",))

    def switch_label(self, dpid: str) -> str:
        """Use the topology adapter's actual label; never derive sN from DPID."""
        graph = self.topology.get_graph()
        data = graph.nodes.get(dpid, {})
        return data.get("name") or dpid

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def is_switch_hybrid(self, dpid: str) -> bool:
        return self.topology.get_state(dpid) == "Hybrid"

    def resolve_switch_name(self, dpid: str) -> str:
        return self.executor._resolve_switch_name(dpid)

    def is_hybrid_connected(self, dpid: str) -> bool:
        if not self.is_switch_hybrid(dpid):
            return False
        switch_name = self.resolve_switch_name(dpid)
        local_port = local_port_for_dpid(dpid)
        hybrid_target = f"tcp:127.0.0.1:{local_port}"
        return self.executor._controller_target_connected(
            switch_name, hybrid_target
        )

    def observe_hybrid_switches(self):
        observations = []
        for dpid in self.discover_hybrid_switches():
            try:
                connected = self.is_hybrid_connected(dpid)
                observations.append({
                    "dpid": dpid,
                    "switch": self.switch_label(dpid),
                    "state": "Hybrid",
                    "connected": connected,
                    "status": "connected" if connected else "disconnected",
                })
            except Exception as exc:
                observations.append({
                    "dpid": dpid,
                    "switch": self.switch_label(dpid),
                    "state": "Hybrid",
                    "connected": None,
                    "status": "observation_error",
                    "reason": str(exc),
                })
        return observations

    def evaluate_health(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility helper for unit tests; production uses _runtime_health."""
        reasons = []
        latency = metrics.get("latency_ms")
        failure_rate = metrics.get("failure_rate")
        if latency is None and failure_rate is None:
            return {"status": "unknown", "healthy": False, "degraded": False,
                    "reasons": ["latency/failure metrics unavailable"]}
        if isinstance(latency, (int, float)) and self.config.min_latency_threshold_ms is not None:
            if latency > self.config.min_latency_threshold_ms:
                reasons.append(f"latency {latency} ms exceeds configured threshold {self.config.min_latency_threshold_ms} ms")
        if isinstance(failure_rate, (int, float)):
            # Compatibility only; no production decision is based on this path.
            if failure_rate >= self.config.failure_threshold / self.config.failure_window:
                reasons.append(f"failure rate {failure_rate} exceeds compatibility threshold")
        status = "degraded" if reasons else "healthy"
        return {"status": status, "healthy": status == "healthy",
                "degraded": status == "degraded", "reasons": reasons}

    def build_switch_health_report(self, dpid: str, connectivity: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
        health = self.evaluate_health(metrics)
        reasons = list(health["reasons"])
        if connectivity.get("status") == "disconnected":
            reasons.append("Hybrid controller disconnected")
        status = "degraded" if reasons else health["status"]
        return {
            "dpid": dpid, "switch": self.switch_label(dpid),
            "state": connectivity.get("state", "Hybrid"),
            "connectivity": connectivity.get("status", "unknown"),
            "latency_ms": metrics.get("latency_ms"),
            "failure_rate": metrics.get("failure_rate"),
            "status": status, "healthy": status == "healthy",
            "degraded": status == "degraded", "reasons": reasons,
        }

    def collect_metrics(self, dpid: str) -> Dict[str, Any]:
        """Compatibility path for legacy unit tests only."""
        return self._compat_metric_provider(dpid)

    # ------------------------------------------------------------------
    # Real latency probe contract
    # ------------------------------------------------------------------

    def _probe(self, dpid: str) -> Dict[str, Any]:
        """
        Execute exactly one real per-switch probe.

        Expected latency_probe result:
          float latency_ms
        or:
          {"latency_ms": float, "ok": bool, "error": optional str}

        A failed probe is represented as ok=False and is counted as a
        failure; it is never converted into a fake latency value.
        """
        probe = self.latency_probe
        if probe is None:
            if self._live_probe is None:
                self._live_probe = self._discover_live_probe()
            probe = self._live_probe.probe

        result = probe(dpid)
        if isinstance(result, (int, float)):
            if not math.isfinite(float(result)) or result < 0:
                raise ValueError(f"invalid latency probe result for {dpid}: {result!r}")
            return {"ok": True, "latency_ms": float(result), "error": None}

        if not isinstance(result, dict):
            raise TypeError("latency_probe must return a number or dictionary")

        ok = bool(result.get("ok", result.get("latency_ms") is not None))
        latency = result.get("latency_ms")
        if ok:
            if not isinstance(latency, (int, float)) or not math.isfinite(float(latency)) or latency < 0:
                raise ValueError(f"successful probe has invalid latency for {dpid}: {latency!r}")
            return {"ok": True, "latency_ms": float(latency), "error": result.get("error")}

        return {"ok": False, "latency_ms": None, "error": result.get("error", "probe failed")}

    @staticmethod
    def _discover_live_probe():
        """Find the real QSMO OpenFlow Echo probe loaded by OS-Ken."""
        try:
            from os_ken.base import app_manager
        except ImportError as exc:
            raise RuntimeError(
                "OS-Ken is not available; a real OpenFlow Echo probe cannot run"
            ) from exc

        for name, app in app_manager.AppManager.get_instance().applications.items():
            if callable(getattr(app, "probe", None)) and callable(getattr(app, "datapath_ids", None)):
                return app

        raise RuntimeError(
            "No live QSMO OpenFlow Echo probe is loaded. "
            "Load controller/qsmo_echo_probe.py with osken-manager."
        )

    def _compat_metric_provider(self, dpid: str) -> Dict[str, Any]:
        """Unit-test compatibility only; never used by default production flow."""
        if self.metric_provider is None:
            return {}
        metrics = self.metric_provider(dpid)
        if not isinstance(metrics, dict):
            raise TypeError("metric_provider must return a dictionary")
        return metrics

    # ------------------------------------------------------------------
    # Baseline statistics
    # ------------------------------------------------------------------

    def _summarize_samples(self, samples: list[float], failures: int, total: int) -> Dict[str, Any]:
        if not samples:
            raise RuntimeError("no successful latency samples were collected")

        mean_ms = statistics.mean(samples)
        median_ms = statistics.median(samples)
        stddev_ms = statistics.stdev(samples) if len(samples) > 1 else 0.0
        threshold_ms = max(
            self.config.min_latency_threshold_ms,
            self.config.sigma_multiplier * stddev_ms,
        )

        return {
            "sample_count": len(samples),
            "attempt_count": total,
            "warmup_discarded": self.config.warmup_samples,
            "failures": failures,
            "failure_rate": failures / total if total else 1.0,
            "mean_ms": mean_ms,
            "median_ms": median_ms,
            "stddev_ms": stddev_ms,
            "latency_threshold_ms": threshold_ms,
        }

    def capture_baseline(self, dpids: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        Measure Legacy latency for every currently discovered switch.

        Exactly baseline_samples probes are attempted per switch.  The first
        warmup_samples successful measurements are discarded from the
        statistical sample; failed warmups are still failures and do not
        become synthetic measurements.
        """
        targets = list(dpids) if dpids is not None else self.discover_legacy_switches()
        results = {}

        self.logger.info(
            "QSMO baseline: measuring %d Legacy switches (%d probes/switch, %d warm-up)",
            len(targets), self.config.baseline_samples, self.config.warmup_samples,
        )

        for dpid in targets:
            raw = []
            usable = []
            failures = 0
            for attempt in range(self.config.baseline_samples):
                sample = self._probe(dpid)
                if sample["ok"]:
                    value = sample["latency_ms"]
                    raw.append(value)
                    if attempt >= self.config.warmup_samples:
                        usable.append(value)
                else:
                    raw.append(None)
                    failures += 1

            if not usable:
                raise RuntimeError(
                    f"No usable Legacy latency samples for {self.switch_label(dpid)} ({dpid})"
                )

            summary = self._summarize_samples(
                usable, failures, self.config.baseline_samples
            )
            summary.update({
                "dpid": dpid,
                "switch": self.switch_label(dpid),
                "state": "Legacy",
                "phase": "legacy_baseline",
                "raw_samples_ms": raw,
            })
            self._baselines[dpid] = summary
            results[dpid] = summary

            self.logger.info(
                "Baseline %s: mean=%.3f ms median=%.3f ms stddev=%.3f ms failures=%d/%d",
                self.switch_label(dpid), summary["mean_ms"], summary["median_ms"], summary["stddev_ms"],
                failures, self.config.baseline_samples,
            )

        return results

    def capture_post_migration(self, dpids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Measure the Hybrid baseline for the switches that actually migrated."""
        targets = list(dict.fromkeys(dpids))
        results = {}

        for dpid in targets:
            if not self.is_switch_hybrid(dpid):
                raise RuntimeError(
                    f"Cannot capture Hybrid baseline for {self.switch_label(dpid)}: state is not Hybrid"
                )

            raw = []
            usable = []
            failures = 0
            for attempt in range(self.config.baseline_samples):
                sample = self._probe(dpid)
                if sample["ok"]:
                    value = sample["latency_ms"]
                    raw.append(value)
                    if attempt >= self.config.warmup_samples:
                        usable.append(value)
                else:
                    raw.append(None)
                    failures += 1

            if not usable:
                raise RuntimeError(
                    f"No usable Hybrid latency samples for {self.switch_label(dpid)} ({dpid})"
                )

            summary = self._summarize_samples(
                usable, failures, self.config.baseline_samples
            )
            summary.update({
                "dpid": dpid,
                "switch": self.switch_label(dpid),
                "state": "Hybrid",
                "phase": "hybrid_baseline",
                "raw_samples_ms": raw,
            })
            self._post_migration[dpid] = summary
            self._rolling_latencies[dpid] = deque(maxlen=self.config.rolling_window)
            self._rolling_failures[dpid] = deque(maxlen=self.config.failure_window)

            legacy = self._baselines.get(dpid)
            comparison = self._compare_baselines(legacy, summary)
            summary["legacy_comparison"] = comparison
            self._migration_report[dpid] = comparison
            results[dpid] = summary

            self.logger.info(
                "Hybrid %s: mean=%.3f ms median=%.3f ms stddev=%.3f ms failures=%d/%d; delta=%s",
                self.switch_label(dpid), summary["mean_ms"], summary["median_ms"], summary["stddev_ms"],
                failures, self.config.baseline_samples,
                f"{comparison['delta_ms']:.3f} ms" if comparison else "N/A",
            )

        return results

    @staticmethod
    def _compare_baselines(legacy: Optional[Dict[str, Any]], hybrid: Dict[str, Any]):
        if not legacy:
            return None
        # The experiment specification defines the Legacy baseline using
        # the mean of the valid Echo RTT samples.  Keep median in the report,
        # but use the mean for the Legacy-vs-Hybrid comparison.
        legacy_ms = float(legacy["mean_ms"])
        hybrid_ms = float(hybrid["mean_ms"])
        delta = hybrid_ms - legacy_ms
        percent = (delta / legacy_ms * 100.0) if legacy_ms > 0 else None
        return {
            "legacy_mean_ms": legacy_ms,
            "hybrid_mean_ms": hybrid_ms,
            "legacy_median_ms": float(legacy["median_ms"]),
            "hybrid_median_ms": float(hybrid["median_ms"]),
            "delta_ms": delta,
            "percent_change": percent,
        }

    def migration_report(self) -> Dict[str, Dict[str, Any]]:
        """Return per-switch Legacy-vs-Hybrid results collected in this run."""
        return {dpid: dict(report) for dpid, report in self._migration_report.items()}

    # ------------------------------------------------------------------
    # Runtime monitoring
    # ------------------------------------------------------------------

    def _runtime_health(self, dpid: str, connectivity: Dict[str, Any]) -> Dict[str, Any]:
        post = self._post_migration.get(dpid)
        if not post:
            return {
                "status": "unknown",
                "healthy": False,
                "degraded": False,
                "reasons": ["no Hybrid baseline exists for switch"],
            }

        sample = self._probe(dpid)
        latencies = self._rolling_latencies.setdefault(
            dpid, deque(maxlen=self.config.rolling_window)
        )
        failures = self._rolling_failures.setdefault(
            dpid, deque(maxlen=self.config.failure_window)
        )

        if sample["ok"]:
            latencies.append(sample["latency_ms"])
            failures.append(False)
        else:
            failures.append(True)

        cfg = self.config
        reasons = []
        current_median = None
        hybrid_limit = None
        legacy_limit = None

        base_median = float(post["median_ms"])
        legacy = self._baselines.get(dpid)
        legacy_mean = float(legacy["mean_ms"]) if legacy else None

        if len(latencies) >= cfg.rolling_window:
            current_median = statistics.median(latencies)

            # Check 1: stability against this switch's own Hybrid baseline.
            hybrid_margin = max(
                cfg.sigma_multiplier * float(post["stddev_ms"]),
                cfg.min_latency_threshold_ms,
                base_median * cfg.degradation_threshold_percent / 100.0,
            )
            hybrid_limit = base_median + hybrid_margin
            if current_median > hybrid_limit:
                reasons.append(
                    f"[hybrid-baseline] median(last {cfg.rolling_window})="
                    f"{current_median:.3f} ms > limit {hybrid_limit:.3f} ms "
                    f"(Hybrid baseline median={base_median:.3f} ms)"
                )

            # Check 2: coarse sanity bound against Legacy.
            if legacy_mean is not None:
                legacy_margin = max(
                    legacy_mean * cfg.legacy_threshold_percent / 100.0,
                    cfg.legacy_threshold_floor_ms,
                )
                legacy_limit = legacy_mean + legacy_margin
                if current_median > legacy_limit:
                    reasons.append(
                        f"[legacy-compare] median(last {cfg.rolling_window})="
                        f"{current_median:.3f} ms > limit {legacy_limit:.3f} ms "
                        f"(Legacy mean={legacy_mean:.3f} ms)"
                    )

        failure_count = sum(1 for f in failures if f)
        window_size = len(failures)
        success_count = window_size - failure_count

        if failure_count >= cfg.failure_threshold:
            reasons.append(
                f"{failure_count} failed probes in last {window_size} probes "
                f"(threshold={cfg.failure_threshold})"
            )

        if connectivity.get("status") == "disconnected":
            reasons.append("Hybrid controller disconnected")

        degraded = bool(reasons)
        return {
            "status": "degraded" if degraded else "healthy",
            "healthy": not degraded,
            "degraded": degraded,
            "latency_ms": sample.get("latency_ms"),
            "rolling_median_ms": current_median,
            "failure_count_window": failure_count,
            "failure_window_size": window_size,
            "success_count_window": success_count,
            "success_rate": success_count / window_size if window_size else None,
            "failure_rate": failure_count / window_size if window_size else None,
            "latency_threshold_ms": post["latency_threshold_ms"],
            "hybrid_limit_ms": hybrid_limit,
            "legacy_limit_ms": legacy_limit,
            "legacy_mean_ms": legacy_mean,
            "baseline_median_ms": base_median,
            "baseline_stddev_ms": post["stddev_ms"],
            "reasons": reasons,
        }

    def monitor_once(self):
        """Measure every current Hybrid switch and apply rollback policy."""
        reports = []

        for observation in self.observe_hybrid_switches():
            dpid = observation["dpid"]
            try:
                health = self._runtime_health(dpid, observation)
            except Exception as exc:
                health = {
                    "status": "degraded",
                    "healthy": False,
                    "degraded": True,
                    "latency_ms": None,
                    "rolling_median_ms": None,
                    "failure_count_window": None,
                    "failure_window_size": None,
                    "reasons": [f"latency probe failed: {exc}"],
                }

            report = {
                "dpid": dpid,
                "switch": self.switch_label(dpid),
                "state": "Hybrid",
                "connectivity": observation.get("status", "unknown"),
                **health,
            }

            if report["status"] == "degraded":
                count = self._degraded_counts.get(dpid, 0) + 1
                self._degraded_counts[dpid] = count
                if (
                    count >= self.config.consecutive_degraded_observations
                    and dpid not in self._rollback_attempted
                ):
                    self._rollback_attempted.add(dpid)
                    reason = "; ".join(report["reasons"])
                    try:
                        report["recovery"] = self.rollback(
                            dpid,
                            reason or "Repeated measured health degradation",
                        )
                    except Exception as exc:
                        self.logger.exception(
                            "Automatic rollback attempt failed for %s", dpid
                        )
                        report["recovery"] = {
                            "dpid": dpid,
                            "outcome": "failed",
                            "reason": str(exc),
                        }
            else:
                self._degraded_counts[dpid] = 0
                self._rollback_attempted.discard(dpid)

            reports.append(report)

            success_text = (
                f"{report['success_rate'] * 100:.1f}%"
                if report.get("success_rate") is not None
                else "N/A"
            )
            if report["status"] == "degraded":
                self.logger.warning(
                    "Migration health degraded for %s (%d/%d): success=%s | %s",
                    self.switch_label(dpid),
                    self._degraded_counts.get(dpid, 0),
                    self.config.consecutive_degraded_observations,
                    success_text,
                    report["reasons"],
                )
            else:
                self.logger.info(
                    "Migration health healthy for %s: latency=%s ms | success=%s",
                    self.switch_label(dpid),
                    report.get("latency_ms"),
                    success_text,
                )

        return reports

    # ------------------------------------------------------------------
    # Human-readable report
    # ------------------------------------------------------------------

    def log_migration_report(self):
        """Print the final human-readable Legacy vs Hybrid report."""
        print("\nQSMO Migration Report")
        print("=" * 52)
        print()
        print(
            f"{'Switch':<12} {'Legacy':<12} {'Hybrid':<12} "
            f"{'Change':<10} {'Recovery':<12} {'Status'}"
        )
        print("-" * 72)

        for dpid in sorted(self._post_migration):
            switch = self.switch_label(dpid)
            hybrid = self._post_migration[dpid]
            comparison = hybrid.get("legacy_comparison")
            if comparison is None:
                legacy_text = "N/A"
                hybrid_text = f"{hybrid['median_ms']:.1f} ms"
                change_text = "N/A"
            else:
                legacy_text = f"{comparison['legacy_median_ms']:.1f} ms"
                hybrid_text = f"{comparison['hybrid_median_ms']:.1f} ms"
                pct = comparison.get("percent_change")
                change_text = f"{pct:+.0f}%" if pct is not None else "N/A"

            recovery_text = "N/A"
            history = self.ledger.get_history(dpid)

            for event in reversed(history):
                if event.get("outcome") == "reverted":
                    recovery_latency = event.get("recovery_latency_ms")
                    if recovery_latency is not None:
                        recovery_text = f"{float(recovery_latency):.1f} ms"
                    break

            status = "HEALTHY"
            if dpid in self._rollback_attempted:
                if any(e.get("outcome") == "reverted" for e in history):
                    status = "DEGRADED / ROLLED BACK"
                else:
                    status = "DEGRADED"

            print(
                f"{switch:<12} {legacy_text:<12} {hybrid_text:<12} "
                f"{change_text:<10} {recovery_text:<12} {status}"
            )

        print()
        return self.migration_report()

    def write_text_report(self, path):
        """Persist the same human-readable report as plain text, not JSON."""
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.log_migration_report()
        text = buffer.getvalue()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ------------------------------------------------------------------
    # Existing continuous-monitor and rollback lifecycle
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        self.logger.info("Continuous migration monitoring started")
        while not self._stop_event.is_set():
            try:
                self.monitor_once()
            except Exception:
                self.logger.exception("Migration monitoring cycle failed")
            self._stop_event.wait(self.config.poll_interval)
        self.logger.info("Continuous migration monitoring stopped")

    def start(self):
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
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def decide_recovery(self, report: Dict[str, Any]) -> Dict[str, Any]:
        status = report.get("status", "unknown")
        dpid = report.get("dpid")
        return {
            "dpid": dpid,
            "decision": "rollback_candidate" if status == "degraded" else (
                "no_action" if status == "healthy" else "unknown"
            ),
            "action_taken": False,
            "reasons": list(report.get("reasons", [])),
        }

    def rollback(self, dpid: str, reason: str, associated_flows=None) -> Dict[str, Any]:
        """Restore a degraded Hybrid switch to Legacy and record the outcome."""
        if not self.is_switch_hybrid(dpid):
            return {
                "dpid": dpid,
                "outcome": "not_rolled_back",
                "reason": "switch is not currently Hybrid",
            }

        history = self.ledger.get_history(dpid)
        successful_events = [event for event in history if event["outcome"] == "success"]
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
        legacy_target = f"ssl:{self.executor.controller_ip}:{self.executor.legacy_port}"

        try:
            recovery_start = time.monotonic()

            rc, out, err = self.executor._set_controller_target(
                switch_name, legacy_target
            )
            if rc != 0:
                recovery_latency_ms = (time.monotonic() - recovery_start) * 1000.0
                return {
                    "dpid": dpid,
                    "outcome": "failed",
                    "reason": f"Legacy controller configuration failed: {(err or out).strip()}",
                    "recovery_latency_ms": recovery_latency_ms,
                }

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.executor._controller_target_connected(switch_name, legacy_target):
                    recovery_latency_ms = (time.monotonic() - recovery_start) * 1000.0
                    self.topology.set_state(dpid, "Legacy")
                    self.ledger.log_event(
                        dpid,
                        baseline,
                        post,
                        outcome="reverted",
                        reason=reason,
                        associated_flows=associated_flows,
                        recovery_latency_ms=recovery_latency_ms,
                    )

                    self.logger.warning(
                        "Rolled back %s (%s) to Legacy: %s | recovery_latency=%.2f ms",
                        dpid, switch_name, reason, recovery_latency_ms,
                    )
                    return {
                        "dpid": dpid,
                        "outcome": "reverted",
                        "reason": reason,
                        "recovery_latency_ms": recovery_latency_ms,
                    }
                time.sleep(0.5)

            recovery_latency_ms = (time.monotonic() - recovery_start) * 1000.0
            return {
                "dpid": dpid,
                "outcome": "failed",
                "reason": "Legacy controller connection was not verified after 10 seconds",
                "recovery_latency_ms": recovery_latency_ms,
            }
        except Exception as exc:
            recovery_latency_ms = (time.monotonic() - recovery_start) * 1000.0
            self.logger.exception("Rollback failed for switch %s", dpid)
            return {
                "dpid": dpid,
                "outcome": "failed",
                "reason": str(exc),
                "recovery_latency_ms": recovery_latency_ms,
            }
