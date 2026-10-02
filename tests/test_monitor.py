import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from migration.monitor import MigrationMonitor


class FakeTopology:
    def get_state(self, dpid):
        return "Legacy"


class FakeExecutor:
    def __init__(self):
        self.topology = FakeTopology()
        self.ledger = None


def test_legacy_switch_is_not_rolled_back():
    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.executor = FakeExecutor()
    monitor.topology = monitor.executor.topology

    result = monitor.rollback(
        "0000000000000001",
        "Test rollback",
    )

    assert result["outcome"] == "not_rolled_back"
    assert result["reason"] == "switch is not currently Hybrid"

    print("Legacy switch rollback guard passed.")




class FakeHybridTopology:
    def get_state(self, dpid):
        return "Hybrid"


class FakeEmptyLedger:
    def get_history(self, dpid):
        return []


def test_hybrid_switch_without_success_record_is_not_rolled_back():
    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.executor = FakeExecutor()
    monitor.topology = FakeHybridTopology()
    monitor.ledger = FakeEmptyLedger()

    result = monitor.rollback(
        "0000000000000001",
        "Test missing migration record",
    )

    assert result["outcome"] == "failed"
    assert result["reason"] == "no successful migration record found"

    print("Missing migration record guard passed.")


class FakeRollbackTopology:
    def __init__(self):
        self.state = "Hybrid"

    def get_state(self, dpid):
        return self.state

    def set_state(self, dpid, state):
        self.state = state


class FakeSuccessLedger:
    def __init__(self):
        self.events = []

    def get_history(self, dpid):
        return [
            {
                "outcome": "success",
                "baseline": {"controller": "Legacy"},
                "post": {"controller": "Hybrid"},
            }
        ]

    def log_event(
        self,
        dpid,
        baseline,
        post,
        outcome,
        reason=None,
        associated_flows=None,
    ):
        self.events.append({
            "dpid": dpid,
            "baseline": baseline,
            "post": post,
            "outcome": outcome,
            "reason": reason,
            "associated_flows": associated_flows,
        })


class FakeRollbackExecutor:
    def __init__(self, topology, ledger):
        self.topology = topology
        self.ledger = ledger
        self.controller_ip = "127.0.0.1"
        self.legacy_port = 6653
        self.target_calls = []

    def _resolve_switch_name(self, dpid):
        return "fake-bridge"

    def _set_controller_target(self, bridge, target):
        self.target_calls.append((bridge, target))
        return 0, "", ""

    def _controller_target_connected(self, bridge, target):
        return True


def test_successful_rollback_updates_state_and_ledger():
    import logging

    topology = FakeRollbackTopology()
    ledger = FakeSuccessLedger()
    executor = FakeRollbackExecutor(topology, ledger)

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.executor = executor
    monitor.topology = topology
    monitor.ledger = ledger
    monitor.logger = logging.getLogger("test_monitor")

    result = monitor.rollback(
        "0000000000000001",
        "Test simulated rollback",
    )

    assert result["outcome"] == "reverted"
    assert topology.state == "Legacy"
    assert executor.target_calls == [
        ("fake-bridge", "ssl:127.0.0.1:6653")
    ]
    assert len(ledger.events) == 1
    assert ledger.events[0]["outcome"] == "reverted"

    print("Simulated successful rollback test passed.")


def test_failed_rollback_preserves_hybrid_state():
    import logging

    topology = FakeRollbackTopology()
    ledger = FakeSuccessLedger()
    executor = FakeRollbackExecutor(topology, ledger)

    def fail_controller_configuration(bridge, target):
        executor.target_calls.append((bridge, target))
        return 1, "", "simulated configuration failure"

    executor._set_controller_target = fail_controller_configuration

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.executor = executor
    monitor.topology = topology
    monitor.ledger = ledger
    monitor.logger = logging.getLogger("test_failed_rollback")

    result = monitor.rollback(
        "0000000000000001",
        "Simulated failure test",
    )

    assert result["outcome"] == "failed"
    assert "simulated configuration failure" in result["reason"]
    assert topology.state == "Hybrid"
    assert ledger.events == []

    print("Failed rollback state-preservation test passed.")

def test_continuous_monitor_starts_and_stops():
    import logging
    import threading
    from migration.monitor import MonitorConfig

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.config = MonitorConfig(poll_interval=0.05)
    monitor.logger = logging.getLogger("test_continuous_monitor")
    monitor._stop_event = threading.Event()
    monitor._thread = None
    observed = threading.Event()

    def fake_monitor_once():
        observed.set()
        return []

    monitor.monitor_once = fake_monitor_once

    assert monitor.start() is True
    assert observed.wait(timeout=1.0)
    assert monitor.stop(timeout=1.0) is True
    assert monitor.start() is True
    assert monitor.stop(timeout=1.0) is True

    print("Continuous monitor start/stop test passed.")


def test_disconnected_switch_logs_degradation():
    import logging
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.logger = logging.getLogger("test_monitor_disconnected")
    monitor.logger.setLevel(logging.WARNING)
    observations = [{
        "dpid": "fake-switch-1",
        "state": "Hybrid",
        "connected": False,
        "status": "disconnected",
    }]
    monitor.observe_hybrid_switches = lambda: observations

    with __import__("unittest").TestCase().assertLogs(
        monitor.logger, level="WARNING"
    ) as captured:
        result = monitor.monitor_once()

    assert len(result) == 1
    assert result[0]["dpid"] == "fake-switch-1"
    assert result[0]["status"] == "degraded"
    assert result[0]["degraded"] is True
    assert any("disconnected" in reason for reason in result[0]["reasons"])
    assert "Migration health degraded" in captured.output[0]
    print("Disconnected switch health-report test passed.")


def test_observation_error_logs_failure():
    import logging
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.logger = logging.getLogger("test_monitor_observation_error")
    monitor.logger.setLevel(logging.WARNING)
    observations = [{
        "dpid": "fake-switch-2",
        "state": "Hybrid",
        "connected": None,
        "status": "observation_error",
        "reason": "simulated test error",
    }]
    monitor.observe_hybrid_switches = lambda: observations

    with __import__("unittest").TestCase().assertLogs(
        monitor.logger, level="WARNING"
    ) as captured:
        result = monitor.monitor_once()

    assert len(result) == 1
    assert result[0]["dpid"] == "fake-switch-2"
    assert result[0]["status"] == "unknown"
    assert "simulated test error" in " ".join(result[0]["reasons"])
    assert "Migration health unknown" in captured.output[0]
    print("Observation error health-report test passed.")


def test_health_evaluation_reports_healthy_metrics():
    from migration.monitor import MonitorConfig

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.config = MonitorConfig(
        latency_threshold_ms=100.0,
        failure_rate_threshold=0.05,
    )

    result = monitor.evaluate_health({
        "latency_ms": 50.0,
        "failure_rate": 0.01,
    })

    assert result["status"] == "healthy"
    assert result["healthy"] is True
    assert result["degraded"] is False
    assert result["reasons"] == []
    print("Healthy metrics evaluation test passed.")


def test_health_evaluation_detects_threshold_breaches():
    from migration.monitor import MonitorConfig

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.config = MonitorConfig(
        latency_threshold_ms=100.0,
        failure_rate_threshold=0.05,
    )

    result = monitor.evaluate_health({
        "latency_ms": 150.0,
        "failure_rate": 0.10,
    })

    assert result["status"] == "degraded"
    assert result["healthy"] is False
    assert result["degraded"] is True
    assert len(result["reasons"]) == 2
    print("Threshold breach evaluation test passed.")


def test_health_evaluation_reports_missing_metrics():
    from migration.monitor import MonitorConfig

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.config = MonitorConfig(
        latency_threshold_ms=100.0,
        failure_rate_threshold=0.05,
    )

    result = monitor.evaluate_health({})

    assert result["status"] == "unknown"
    assert result["healthy"] is False
    assert result["degraded"] is False
    assert len(result["reasons"]) == 2
    print("Missing metrics evaluation test passed.")


def test_unified_health_report():
    from migration.monitor import MonitorConfig

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.config = MonitorConfig(
        latency_threshold_ms=100.0,
        failure_rate_threshold=0.05,
    )

    metrics = {"latency_ms": 25.0, "failure_rate": 0.01}
    connected = {"state": "Hybrid", "status": "connected"}

    report = monitor.build_switch_health_report(
        "fake-switch-1", connected, metrics
    )

    assert report["status"] == "healthy"
    assert report["healthy"] is True
    assert report["connectivity"] == "connected"

    disconnected = {"state": "Hybrid", "status": "disconnected"}
    report = monitor.build_switch_health_report(
        "fake-switch-1", disconnected, metrics
    )

    assert report["status"] == "degraded"
    assert "Hybrid controller disconnected" in report["reasons"]

    unknown = {"state": "Hybrid", "status": "observation_error"}
    report = monitor.build_switch_health_report(
        "fake-switch-1", unknown, {}
    )

    assert report["status"] == "unknown"
    assert report["healthy"] is False

    print("Unified health report tests passed.")




def test_metric_provider_returns_simulated_metrics():
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.metric_provider = lambda dpid: {
        "latency_ms": 32.5,
        "failure_rate": 0.02,
    }

    result = monitor.collect_metrics("fake-switch-1")

    assert result["latency_ms"] == 32.5
    assert result["failure_rate"] == 0.02
    print("Simulated metric provider test passed.")


def test_missing_metric_provider_returns_empty_metrics():
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.metric_provider = None

    assert monitor.collect_metrics("fake-switch-1") == {}
    print("Missing metric provider test passed.")


def test_invalid_metric_provider_output_is_rejected():
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)
    monitor.metric_provider = lambda dpid: None

    try:
        monitor.collect_metrics("fake-switch-1")
    except TypeError:
        pass
    else:
        raise AssertionError("Invalid metric output was not rejected")

    print("Invalid metric provider output test passed.")


def test_recovery_decisions_do_not_execute_rollback():
    from migration.monitor import MigrationMonitor

    monitor = MigrationMonitor.__new__(MigrationMonitor)

    def forbidden_rollback(*args, **kwargs):
        raise AssertionError("Recovery decision must not execute rollback")

    monitor.rollback = forbidden_rollback

    cases = [
        (
            {"dpid": "fake-healthy", "status": "healthy", "reasons": []},
            "no_action",
        ),
        (
            {
                "dpid": "fake-degraded",
                "status": "degraded",
                "reasons": ["simulated threshold breach"],
            },
            "rollback_candidate",
        ),
        (
            {
                "dpid": "fake-unknown",
                "status": "unknown",
                "reasons": ["metrics unavailable"],
            },
            "unknown",
        ),
    ]

    for report, expected in cases:
        result = monitor.decide_recovery(report)
        assert result["decision"] == expected
        assert result["action_taken"] is False
        assert result["dpid"] == report["dpid"]

    print("Non-executing recovery decision tests passed.")


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print("All monitor tests passed.")
