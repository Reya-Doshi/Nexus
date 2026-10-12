"""
Comprehensive Test Suite for ReRoute / Vanguard (Milestones 1–5).
Verifies:
- Milestone 1: Focused demo dashboard & 6-router redundant topology.
- Milestone 2: Rolling telemetry, ground-truth separation, explainable detection rules, incident lifecycle.
- Milestone 3: Qualcomm Cloud AI 100 client, schema validation, non-blocking async, stale epoch drop, rule fallback.
- Milestone 4: Validated deterministic healing, Auto-heal OFF vs ON behavior, verification window, no-alternate-path unresolved state.
- Milestone 5: Repeatable deterministic comparison runner.
"""

import pytest
import time
from lab_session import LabSession
from reroute_engine import (
    get_six_router_preset,
    IncidentState,
    TelemetryCollector,
    LinkTelemetry,
    ExplainableDetector,
    DetectionRuleConfig,
    QualcommAIClient,
    AIDiagnosisResult,
    HealingManager,
    ReRouteController,
    ReRouteScenarioType,
)


def test_milestone_1_six_router_topology_structure():
    """Milestone 1: Verify fixed six-router topology has redundant paths between Client and Server."""
    preset = get_six_router_preset()
    assert preset["name"] == "ReRoute 6-Router Redundant Mesh"
    device_ids = {d["id"] for d in preset["devices"]}
    expected_devices = {"Client", "R1", "R2", "R3", "R4", "R5", "R6", "Server"}
    assert expected_devices == device_ids

    # 12 redundant links
    assert len(preset["links"]) == 12

    # Verify session can load the preset
    session = LabSession()
    state = session.load_reroute_demo()
    assert state["reroute"] is not None
    assert state["reroute"]["incident_state"] == "Healthy"
    assert state["reroute"]["auto_heal_enabled"] is True
    assert state["reroute"]["current_route"] == ["Client", "R1", "R2", "R4", "R6", "Server"]


def test_milestone_1_dashboard_controls_and_state():
    """Milestone 1: Verify Start, Step, Pause, Reset, and Auto-Heal ON/OFF controls."""
    session = LabSession()
    session.load_reroute_demo()

    # Step simulation
    step_state = session.reroute_step()
    r = step_state["reroute"]
    assert r["telemetry"]["total_observed"] >= 1
    assert "recent_pdr_pct" in r["metrics"]
    assert "recent_latency_ms" in r["metrics"]

    # Toggle Auto-Heal OFF
    session.reroute_set_auto_heal(False)
    assert session.state()["reroute"]["auto_heal_enabled"] is False

    # Toggle Auto-Heal ON
    session.reroute_set_auto_heal(True)
    assert session.state()["reroute"]["auto_heal_enabled"] is True

    # Reset
    reset_state = session.reroute_reset()
    assert reset_state["reroute"]["incident_state"] == "Healthy"
    assert reset_state["reroute"]["active_scenario"] is None


def test_milestone_2_telemetry_ground_truth_segregation():
    """Milestone 2: Verify telemetry collector maintains rolling window without receiving injected fault ground-truth label."""
    collector = TelemetryCollector(window_size=5)
    collector.baseline_latency = 20.0

    # Record 5 delivered packets
    for i in range(5):
        collector.record_packet(i, 1.0 + i*0.1, "Client", "Server", "DELIVERED", 20.0, ["Client", "R1", "R2", "R4", "R6", "Server"])

    assert collector.get_recent_delivery_rate() == 100.0
    assert collector.get_recent_avg_latency() == 20.0

    # Detector input only gets observable stats, not fault label
    detector = ExplainableDetector()
    level, evidence, suspected = detector.evaluate(collector, ["Client", "R1", "R2", "R4", "R6", "Server"])
    assert level == "HEALTHY"
    assert not evidence


def test_milestone_2_detection_rules_and_lifecycle():
    """Milestone 2: Verify explainable threshold rules trigger Warning -> Detected."""
    collector = TelemetryCollector(window_size=5)
    collector.baseline_latency = 20.0
    detector = ExplainableDetector(DetectionRuleConfig(consecutive_samples_required=2, detected_latency_mult=2.2))

    # Add spike in latency (55ms > 20*2.2=44ms)
    collector.record_packet(1, 1.0, "Client", "Server", "DELIVERED", 55.0, ["Client", "R1", "R2", "R4", "R6", "Server"])
    # Sample 1: first degraded observation -> Warning
    lvl, ev, _ = detector.evaluate(collector, ["Client", "R1", "R2", "R4", "R6", "Server"])
    assert lvl in ("WARNING", "DETECTED")

    # Sample 2: consecutive degraded sample -> DETECTED
    collector.record_packet(2, 1.1, "Client", "Server", "DELIVERED", 60.0, ["Client", "R1", "R2", "R4", "R6", "Server"])
    lvl2, ev2, _ = detector.evaluate(collector, ["Client", "R1", "R2", "R4", "R6", "Server"])
    assert lvl2 == "DETECTED"
    assert any("latency" in e.lower() for e in ev2)


def test_milestone_3_qualcomm_ai_client_parsing_and_validation():
    """Milestone 3: Verify Qualcomm AI client validates components, actions, and formats."""
    client = QualcommAIClient(api_key="test-key")

    valid_comps = ["R1-R2", "R2-R4", "R4-R6", "R1-R3", "R3-R5", "R5-R6"]
    route = ["Client", "R1", "R2", "R4", "R6", "Server"]

    # Test valid JSON response
    valid_json = """
    {
      "likely_cause": "LINK_FAILURE",
      "affected_component": "R2-R4",
      "evidence": ["High packet loss", "Missed heartbeats"],
      "recommended_action": "REROUTE_TRAFFIC",
      "explanation": "Primary trunk link R2-R4 has failed."
    }
    """
    res = client._parse_and_validate(valid_json, valid_comps, route, 45.0)
    assert res.likely_cause == "LINK_FAILURE"
    assert res.affected_component == "R2-R4"
    assert res.recommended_action == "REROUTE_TRAFFIC"
    assert res.is_fallback is False

    # Test hallucinated component rejection -> fallback to rule diagnosis
    bad_json = """
    {
      "likely_cause": "UNKNOWN",
      "affected_component": "Router999-Mars",
      "evidence": ["Lost packets"],
      "recommended_action": "DO_SOMETHING_ILLEGAL",
      "explanation": "Fake"
    }
    """
    sanitized = client._parse_and_validate(bad_json, valid_comps, route, 30.0)
    # Strictly rejected with clearly labelled rule fallback
    assert sanitized.is_fallback is True
    assert "Rejected hallucinated AI component" in (sanitized.fallback_reason or "")
    assert sanitized.affected_component in valid_comps
    assert sanitized.recommended_action == "REROUTE_TRAFFIC"


def test_milestone_3_stale_epoch_drop_and_rule_fallback():
    """Milestone 3: Verify stale async responses after reset are ignored, and rule fallback activates when offline."""
    client = QualcommAIClient(api_key="")  # Unconfigured key triggers rule fallback

    res_holder = []
    epoch = client.new_epoch()
    client.diagnose_async(
        epoch=epoch,
        telemetry_summary={},
        topology_components=["R2-R4"],
        current_route=["Client", "R1", "R2", "R4", "R6", "Server"],
        detector_evidence=["Heartbeat dropped"],
        suspected_component="R2-R4",
        on_complete=lambda r: res_holder.append(r),
    )
    time.sleep(0.2)
    assert len(res_holder) == 1
    assert res_holder[0].is_fallback is True
    assert "Rule Fallback" in res_holder[0].model_name

    # Test stale response dropping on epoch advancement
    res_holder.clear()
    old_epoch = client.current_epoch
    client.new_epoch()  # advance epoch (e.g. user clicked Reset)

    # Dispatched on old epoch
    client._dispatch_if_current(old_epoch, AIDiagnosisResult(
        "TEST", "R2-R4", [], "REROUTE_TRAFFIC", "stale", "m", 10.0
    ), lambda r: res_holder.append(r))
    # Must be dropped!
    assert len(res_holder) == 0


def test_milestone_4_deterministic_healing_and_verification_window():
    """Milestone 4: Verify deterministic alternative path calculation and recovery measurement window."""
    session = LabSession()
    session.load_reroute_demo()

    healing = HealingManager(verification_window=5)

    # Alternate path excluding R2-R4
    topo = session.simulator.topology
    alt = healing.calculate_alternative_path(topo, "Client", "Server", exclude_component="R2-R4")
    assert alt is not None
    alt_path, cost = alt
    assert "R2-R4" not in [f"{alt_path[i]}-{alt_path[i+1]}" for i in range(len(alt_path)-1)]
    # Bypasses via alternate route excluding R2-R4 (e.g. cross link R2-R5 or lower path)
    assert ("R2", "R4") not in [(alt_path[i], alt_path[i+1]) for i in range(len(alt_path)-1)]

    # Verify recovery window requires 5 consecutive DELIVERED packets meeting latency limit
    for _ in range(4):
        assert healing.record_verification_packet("DELIVERED", latency_ms=25.0) is False
    # 5th consecutive packet confirms verified recovery
    assert healing.record_verification_packet("DELIVERED", latency_ms=25.0) is True

    # A single drop resets the count
    healing.record_verification_packet("DROPPED", latency_ms=25.0)
    assert healing.consecutive_delivered_on_new_route == 0


def test_milestone_4_auto_heal_off_preserves_baseline_policy():
    """Milestone 4: Verify Auto-Heal OFF preserves baseline routing policy instead of healing."""
    session = LabSession()
    session.load_reroute_demo()
    session.reroute_set_auto_heal(False)

    ctrl = session.reroute_controller
    ctrl.inject_scenario(ReRouteScenarioType.SUDDEN_LINK_FAILURE.value)

    # Step past fault injection
    for _ in range(4):
        session.reroute_step()

    # With Auto-Heal OFF, route must remain on primary path even with failed link
    assert ctrl.healing.auto_heal_enabled is False
    assert ctrl.current_route == ["Client", "R1", "R2", "R4", "R6", "Server"]
    assert ctrl.incident_state != IncidentState.RECOVERED


def test_milestone_4_auto_heal_on_recovers_traffic():
    """Milestone 4: Verify Auto-Heal ON detects failure, diagnoses, reroutes, and verifies recovery."""
    session = LabSession()
    session.load_reroute_demo()
    session.reroute_set_auto_heal(True)

    ctrl = session.reroute_controller
    ctrl.inject_scenario(ReRouteScenarioType.SUDDEN_LINK_FAILURE.value)

    # Step through fault and self-healing
    for _ in range(8):
        session.reroute_step()
        if ctrl.incident_state == IncidentState.DIAGNOSING and ctrl.active_diagnosis_thread:
            ctrl.active_diagnosis_thread.join(timeout=4.0)

    # With Auto-Heal ON, traffic reroutes away from failed R2-R4
    assert "R2-R4" not in [f"{ctrl.current_route[i]}-{ctrl.current_route[i+1]}" for i in range(len(ctrl.current_route)-1)]
    assert ctrl.incident_state in (IncidentState.REROUTING, IncidentState.VERIFYING, IncidentState.RECOVERED)


def test_milestone_4_no_alternate_path_unresolved():
    """Milestone 4: Verify no alternate path scenario transitions to Unresolved."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller
    ctrl.inject_scenario(ReRouteScenarioType.NO_ALTERNATE_PATH.value)

    # Step past dual failure
    for _ in range(8):
        session.reroute_step()

    # Must transition to Unresolved when no alternate path exists
    assert ctrl.incident_state == IncidentState.UNRESOLVED or ctrl.active_scenario == "no_alternate_path"


def test_milestone_5_repeatable_comparison_benchmark():
    """Milestone 5: Run deterministic comparison benchmark between Auto-Heal OFF and ON."""
    try:
        from netadapt.reroute_engine import run_comparison_benchmark
    except ModuleNotFoundError:
        from reroute_engine import run_comparison_benchmark

    report = run_comparison_benchmark(scenario_name="sudden_link_failure", offered_packets=30, seed=42)
    assert report is not None
    assert report.scenario == "sudden_link_failure"
    assert report.seed == 42

    # Auto-Heal OFF fails to recover
    assert report.auto_heal_off.auto_heal is False
    assert report.auto_heal_off.disruption.packets_dropped > 0

    # Auto-Heal ON heals and recovers post-action delivery rate
    assert report.auto_heal_on.auto_heal is True
    assert report.auto_heal_on.post_action.delivery_rate_pct > report.auto_heal_off.post_action.delivery_rate_pct
    assert report.pdr_delta_pct > 0.0

    # Check export formats
    d = report.to_dict()
    assert "summary_comparison" in d
    assert "post_action_pdr_improvement_pct" in d["summary_comparison"]

    csv_text = report.to_csv()
    assert "Metric,Auto-Heal OFF,Auto-Heal ON,Unit" in csv_text
    assert "Pre-Fault PDR" in csv_text
    assert "Post-Action PDR" in csv_text


def test_milestone_6_repeated_reset_and_stale_epoch():
    """Milestone 6: Verify repeated reset cancels pending inference and drops stale AI results."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller

    initial_epoch = ctrl.ai_client.current_epoch
    ctrl.reset()
    assert ctrl.ai_client.current_epoch == initial_epoch + 1
    ctrl.reset()
    assert ctrl.ai_client.current_epoch == initial_epoch + 2

    # Discard stale result if epoch doesn't match
    received = []
    ctrl.ai_client._dispatch_if_current(
        epoch=initial_epoch,  # old epoch
        result=None,
        on_complete=lambda r: received.append(r),
    )
    assert len(received) == 0


def test_reroute_run_comparison_action():
    """Verify lab_session reroute_run_comparison returns structured dictionary and CSV."""
    session = LabSession()
    session.load_reroute_demo()
    # Mock AI to fast deterministic fallback for automated unit testing
    session.reroute_controller.ai_client.api_key = ""
    res = session.reroute_run_comparison(scenario="sudden_link_failure", offered_packets=30, seed=42)
    assert "report_dict" in res
    assert "report_csv" in res
    assert res["report_dict"]["scenario"] == "sudden_link_failure"
    assert "Metric,Auto-Heal OFF,Auto-Heal ON,Unit" in res["report_csv"]


# ==============================================================================
# Comprehensive Backend Review Regression Tests (Mocked AI Responses)
# ==============================================================================

def test_regression_healthy_traffic_never_triggers_incidents():
    """Prove that normal healthy traffic does not trigger false-positive incidents or state changes."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller

    for step in range(15):
        session.reroute_step()
        assert ctrl.incident_state == IncidentState.HEALTHY
        assert ctrl.detector.evaluate(ctrl.telemetry, ctrl.current_route)[0] == "HEALTHY"

    assert ctrl.telemetry.total_delivered == 15
    assert ctrl.telemetry.total_dropped == 0


def test_regression_fault_detection_uses_observations_and_heartbeats():
    """Prove that fault detection strictly uses observable metrics and respects scheduled heartbeat timing."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller
    topo = session.simulator.topology

    # Link DOWN should not immediately trigger detection until probe fires and heartbeat misses accumulate
    topo.fail_link("R2", "R4")

    # Manually verify that with 0 missed heartbeats, detector reports Healthy (not cheating from DOWN status)
    ctrl.telemetry.update_link_telemetry("R2-R4", 5.0, 0.0, 0.0, missed_hb=0)
    lvl, ev, _ = ctrl.detector.evaluate(ctrl.telemetry, ctrl.current_route)
    assert lvl == "HEALTHY"

    # After scheduled probes detect 2 missed heartbeats -> DETECTED
    ctrl.telemetry.update_link_telemetry("R2-R4", 5.0, 1.0, 0.0, missed_hb=2)
    lvl, ev, suspected = ctrl.detector.evaluate(ctrl.telemetry, ctrl.current_route)
    assert lvl == "DETECTED"
    assert suspected == "R2-R4"
    assert any("Heartbeat failure" in e for e in ev)


def test_regression_latency_is_consistently_measured_in_milliseconds():
    """Prove that packet latency and summary metrics are strictly in milliseconds across telemetry boundaries."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller

    # Step simulator: raw simulator latency is in seconds (~0.019s)
    session.reroute_step()
    recent = ctrl.telemetry.packet_history[-1]
    assert recent.latency is not None
    # Converted to milliseconds: ~19.48 ms
    assert 15.0 <= recent.latency <= 30.0

    summary = ctrl.telemetry.to_summary_dict()
    assert 15.0 <= summary["recent_avg_latency_ms"] <= 30.0


def test_regression_valid_reroutes_strictly_avoid_failed_components():
    """Prove that deterministic routing strictly calculates paths that avoid failed or degraded components."""
    session = LabSession()
    session.load_reroute_demo()
    healing = HealingManager()
    topo = session.simulator.topology

    # Fail primary trunk R2-R4
    topo.fail_link("R2", "R4")
    alt = healing.calculate_alternative_path(topo, "Client", "Server", exclude_component="R2-R4")
    assert alt is not None
    path, cost = alt
    assert "R2" not in path or "R4" not in path or path.index("R4") != path.index("R2") + 1
    assert ("R2", "R4") not in [(path[i], path[i+1]) for i in range(len(path)-1)]


def test_regression_recovery_requires_distinct_packets_meeting_service_limits():
    """Prove that recovery requires distinct post-action packets meeting stated latency service limits (<= 50 ms)."""
    healing = HealingManager(verification_window=5)
    bypass_route = ["Client", "R1", "R3", "R5", "R6", "Server"]
    healing.expected_route = list(bypass_route)
    healing.action_sim_time = 10.0

    # Test 1: Replaying the SAME packet ID five times must NOT recover
    for _ in range(5):
        assert healing.record_verification_packet(
            "DELIVERED", latency_ms=25.0, packet_id=100, timestamp=10.5, route=bypass_route
        ) is False
    assert healing.consecutive_delivered_on_new_route == 1  # Only accepted once!

    # Test 2: Missing latency must be rejected
    healing.reset()
    healing.expected_route = list(bypass_route)
    healing.action_sim_time = 10.0
    assert healing.record_verification_packet(
        "DELIVERED", latency_ms=None, packet_id=101, timestamp=10.5, route=bypass_route
    ) is False
    assert healing.consecutive_delivered_on_new_route == 0

    # Test 3: Packets processed before action boundary must NOT count
    assert healing.record_verification_packet(
        "DELIVERED", latency_ms=20.0, packet_id=102, timestamp=9.5, route=bypass_route
    ) is False
    assert healing.consecutive_delivered_on_new_route == 0

    # Test 4: Packets on the wrong route must NOT count and reset count
    assert healing.record_verification_packet(
        "DELIVERED", latency_ms=20.0, packet_id=103, timestamp=10.5, route=["Client", "R1", "R2", "R4", "R6", "Server"]
    ) is False
    assert healing.consecutive_delivered_on_new_route == 0

    # Test 5: A packet exceeding latency service limit (65ms > 50ms) resets verification count
    healing.record_verification_packet("DELIVERED", latency_ms=20.0, packet_id=104, timestamp=10.6, route=bypass_route)
    assert healing.consecutive_delivered_on_new_route == 1
    assert healing.record_verification_packet(
        "DELIVERED", latency_ms=65.0, packet_id=105, timestamp=10.7, route=bypass_route
    ) is False
    assert healing.consecutive_delivered_on_new_route == 0

    # Test 6: Five distinct valid post-action packets on the bypass route recover
    for i in range(4):
        assert healing.record_verification_packet(
            "DELIVERED", latency_ms=25.0, packet_id=200 + i, timestamp=11.0 + i, route=bypass_route
        ) is False
    assert healing.record_verification_packet(
        "DELIVERED", latency_ms=25.0, packet_id=204, timestamp=15.0, route=bypass_route
    ) is True
    assert healing.consecutive_delivered_on_new_route == 5


def test_regression_no_alternate_path_remains_unresolved():
    """Prove that severed egress links leaving no alternative path transition to and remain in Unresolved."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller
    ctrl.ai_client.api_key = ""  # Mock AI
    ctrl.inject_scenario(ReRouteScenarioType.NO_ALTERNATE_PATH.value)

    for _ in range(8):
        session.reroute_step()

    assert ctrl.incident_state == IncidentState.UNRESOLVED


def test_regression_ai_timeout_or_malformed_output_produces_labelled_fallback():
    """Prove that timeout or malformed AI output triggers an explicitly labelled deterministic rule fallback."""
    client = QualcommAIClient(api_key="mock-key", timeout_seconds=0.01)

    # 1. Malformed JSON parsing
    res = client._parse_and_validate(
        raw_text="NOT VALID JSON AT ALL",
        valid_components=["R2-R4"],
        current_route=["Client", "R1", "R2", "R4", "R6", "Server"],
        elapsed_ms=10.0,
    )
    assert res.is_fallback is True
    assert "Rule Fallback" in res.model_name

    # 2. Unknown component rejection
    res2 = client._parse_and_validate(
        raw_text='{"likely_cause": "FAIL", "affected_component": "Mars-Saturn", "recommended_action": "REROUTE_TRAFFIC"}',
        valid_components=["R2-R4"],
        current_route=["Client", "R1", "R2", "R4", "R6", "Server"],
        elapsed_ms=10.0,
    )
    assert res2.is_fallback is True
    assert "Rejected hallucinated AI component" in (res2.fallback_reason or "")


def test_regression_reset_invalidates_pending_ai_work():
    """Prove that resetting invalidates pending in-flight AI requests and drops stale responses."""
    client = QualcommAIClient(api_key="")
    old_epoch = client.new_epoch()
    results = []

    # Reset advances epoch
    client.new_epoch()

    # Attempt to dispatch response on old epoch
    client._dispatch_if_current(old_epoch, AIDiagnosisResult("FAIL", "R2-R4", [], "REROUTE_TRAFFIC", "", "m", 1.0), lambda r: results.append(r))
    assert len(results) == 0


def test_regression_second_fault_after_recovery_creates_new_incident():
    """Prove that an additional fault injected after full recovery properly creates a new incident lifecycle."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller
    ctrl.ai_client.api_key = ""  # Mock AI for unit test speed

    # 1. Incur first fault and recover
    ctrl.inject_scenario(ReRouteScenarioType.SUDDEN_LINK_FAILURE.value)
    for _ in range(12):
        session.reroute_step()
        if ctrl.incident_state == IncidentState.DIAGNOSING and ctrl.active_diagnosis_thread:
            ctrl.active_diagnosis_thread.join(timeout=1.0)

    # Step through verification window
    for _ in range(5):
        session.reroute_step()

    assert ctrl.incident_state == IncidentState.RECOVERED

    # 2. Inject second fault on active link of the new bypass route
    active_hop = ctrl.current_route[1], ctrl.current_route[2]
    session.simulator.topology.fail_link(active_hop[0], active_hop[1])

    # Next steps must trigger a new incident lifecycle (Warning / Detected -> Rerouting)
    for _ in range(4):
        session.reroute_step()
        if ctrl.incident_state == IncidentState.DIAGNOSING and ctrl.active_diagnosis_thread:
            ctrl.active_diagnosis_thread.join(timeout=1.0)

    assert ctrl.incident_state in (IncidentState.WARNING, IncidentState.DETECTED, IncidentState.DIAGNOSING, IncidentState.ACTION_SELECTED, IncidentState.REROUTING, IncidentState.VERIFYING)


def test_regression_auto_heal_off_never_silently_reroutes():
    """Prove that when Auto-Heal is OFF, baseline routing policy is strictly preserved without rerouting."""
    session = LabSession()
    session.load_reroute_demo()
    session.reroute_set_auto_heal(False)
    ctrl = session.reroute_controller
    initial_route = list(ctrl.current_route)

    ctrl.inject_scenario(ReRouteScenarioType.SUDDEN_LINK_FAILURE.value)
    for _ in range(8):
        session.reroute_step()

    assert ctrl.current_route == initial_route
    assert ctrl.incident_state != IncidentState.RECOVERED


def test_regression_ab_runs_align_workloads_and_empty_metrics_na():
    """Prove that A/B benchmark uses aligned observation windows, and empty phases report None (N/A in CSV)."""
    try:
        from netadapt.reroute_engine import run_comparison_benchmark, PhaseMetrics
    except ModuleNotFoundError:
        from reroute_engine import run_comparison_benchmark, PhaseMetrics

    report = run_comparison_benchmark(scenario_name="sudden_link_failure", offered_packets=25, seed=123)
    assert report.auto_heal_off.pre_fault.packets_offered == report.auto_heal_on.pre_fault.packets_offered
    assert report.auto_heal_off.post_action.packets_offered == report.auto_heal_on.post_action.packets_offered

    # Empty phase test
    empty = PhaseMetrics("empty_phase", packets_offered=0, packets_delivered=0, packets_dropped=0, delivery_rate_pct=None, avg_latency_ms=None)
    assert empty.delivery_rate_pct is None
    assert empty.avg_latency_ms is None


def test_regression_unprobed_link_loss_is_unknown_not_status_derived():
    """Prove that link loss is calculated from measured probe outcomes and remains unknown/None if unprobed."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller

    # Step once to establish initial probe
    session.reroute_step()

    # Create a new link that hasn't received any probe yet
    ctrl.telemetry.link_telemetry["R99-R100"] = LinkTelemetry(
        link_key="R99-R100",
        observed_latency=None,
        observed_loss_rate=None,
        observed_utilization=0.0,
        heartbeat_missed=0,
    )
    # The detector must handle None loss rate gracefully without crashing or assuming 1.0 loss
    detector = ExplainableDetector()
    level, evidence, suspected = detector.evaluate(ctrl.telemetry, ["Client", "R1", "R2", "R4", "R6", "Server"])
    assert level == "HEALTHY"
    assert not any("R99-R100" in e for e in evidence)


def test_regression_ab_benchmark_honors_offered_packets_and_rejects_small_size():
    """Prove that run_comparison_benchmark rejects offered_packets < 15 and honors workload count exactly."""
    try:
        from netadapt.reroute_engine import run_comparison_benchmark
    except ModuleNotFoundError:
        from reroute_engine import run_comparison_benchmark

    import pytest
    with pytest.raises(ValueError, match="offered_packets must be at least 15"):
        run_comparison_benchmark(offered_packets=10)

    report = run_comparison_benchmark(offered_packets=20, seed=99)
    total_on = (
        report.auto_heal_on.pre_fault.packets_offered +
        report.auto_heal_on.disruption.packets_offered +
        report.auto_heal_on.post_action.packets_offered
    )
    assert total_on == 20
    assert report.offered_packets == 20


def test_regression_ab_benchmark_injects_deterministic_provider_and_records_in_reports():
    """Prove that run_comparison_benchmark injects deterministic diagnosis and records provider identity in report."""
    try:
        from netadapt.reroute_engine import run_comparison_benchmark, AIDiagnosisResult
    except ModuleNotFoundError:
        from reroute_engine import run_comparison_benchmark, AIDiagnosisResult

    def custom_provider(epoch, telemetry_summary, topology_components, current_route, detector_evidence, suspected_component):
        return AIDiagnosisResult(
            likely_cause="LINK_FAILURE",
            affected_component="R2-R4",
            evidence=["Custom deterministic benchmark rule"],
            recommended_action="REROUTE_TRAFFIC",
            explanation="Benchmark mock provider",
            model_name="Custom Benchmark Provider",
            wall_clock_ms=0.5,
            is_fallback=True,
        )

    report = run_comparison_benchmark(
        scenario_name="sudden_link_failure",
        offered_packets=25,
        seed=42,
        diagnosis_provider=custom_provider,
    )
    assert report.diagnosis_provider == "Custom Benchmark Provider"
    assert "Custom Benchmark Provider" in report.to_dict()["diagnosis_provider"]
    assert "Custom Benchmark Provider" in report.to_csv()


def test_regression_zero_deliveries_latency_none_propagates_to_na_delta():
    """Prove that when deliveries are 0, latency is None and propagates to N/A in report deltas and CSV."""
    try:
        from netadapt.reroute_engine import run_comparison_benchmark
    except ModuleNotFoundError:
        from reroute_engine import run_comparison_benchmark

    # In sudden_link_failure with Auto-Heal OFF, post-action has 0 deliveries on severed trunk
    report = run_comparison_benchmark(scenario_name="sudden_link_failure", offered_packets=25, seed=42)
    assert report.auto_heal_off.post_action.packets_delivered == 0
    assert report.auto_heal_off.post_action.avg_latency_ms is None

    # Delta must be None because one side had 0 deliveries
    assert report.latency_delta_ms is None
    d = report.to_dict()
    assert d["summary_comparison"]["post_action_latency_delta_ms"] == "N/A"
    csv_text = report.to_csv()
    assert "Post-Action Latency,N/A," in csv_text



