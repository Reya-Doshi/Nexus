"""Focused tests for the engine-backed interactive network workspace."""

from network_workspace import (
    _WORKSPACE_HTML,
    advance_packet,
    build_workspace_snapshot,
)


def test_workspace_step_traces_actual_packet_route_and_event():
    from simulator import NetworkSimulator

    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", "VoIP", packet_count=2, packet_size=1200, pps=5)
    traces = []

    advanced, sequence = advance_packet(sim, traces, sequence=1)

    assert advanced is True
    assert sequence == 2
    assert sim.scheduler.qsize() == 1 if hasattr(sim.scheduler, "qsize") else len(sim.scheduler) == 1
    trace = traces[0]
    assert trace["id"] == sim.packets[0].packet_id
    assert trace["route"] == flow.current_route
    assert trace["source"] == "H1"
    assert trace["destination"] == "H3"
    assert trace["trafficType"] == "VoIP"
    assert trace["status"] in {"DELIVERED", "DROPPED"}
    assert any(event["event"] in {"PACKET_DELIVERED", "PACKET_DROPPED"} for event in trace["events"])


def test_workspace_drop_uses_real_loss_event_and_snapshot_state():
    from simulator import NetworkSimulator

    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", "HTTP", packet_count=1, packet_size=1000, pps=10)
    sim.set_packet_loss("H3", "R5", 1.0)
    traces = []
    advance_packet(sim, traces)

    trace = traces[0]
    assert trace["status"] == "DROPPED"
    assert any(
        event["event"] == "PACKET_DROPPED" and event.get("details", {}).get("reason") == "PACKET_LOSS"
        for event in trace["events"]
    )

    snapshot = build_workspace_snapshot(
        sim,
        traces=traces,
        active_route=flow.current_route,
    )
    h3_r5 = next(link for link in snapshot["links"] if link["id"] == "H3-R5")
    assert h3_r5["packetLoss"] == 1.0
    assert snapshot["metrics"]["dropped"] == 1
    assert snapshot["selectedPacket"]["status"] == "DROPPED"


def test_workspace_failure_detection_reroutes_then_animates_new_route():
    from simulator import NetworkSimulator

    sim = NetworkSimulator(seed=42, failure_detection_timeout=1.0)
    flow = sim.create_flow("H1", "H3", "Video", packet_count=2, packet_size=1000, pps=10)
    original_route = list(flow.current_route)

    sim.fail_link("R3", "R5")
    sim.tick(sim.failure_detection_timeout + 0.01)

    assert flow.current_route != original_route
    assert "FAILURE_DETECTED" in [event.event_type for event in sim.event_logger.events]
    assert "TRAFFIC_REROUTED" in [event.event_type for event in sim.event_logger.events]

    traces = []
    advance_packet(sim, traces)
    assert traces[0]["route"] == flow.current_route
    assert "R3-R5" not in {
        f"{a}-{b}" for a, b in zip(traces[0]["route"], traces[0]["route"][1:])
    }


def test_workspace_heartbeat_step_advances_real_clock_when_queue_empty():
    from simulator import NetworkSimulator

    sim = NetworkSimulator(seed=42, failure_detection_timeout=1.0)
    sim.fail_link("R1", "R3")
    traces = []

    for expected_sequence in range(2, 6):
        advanced, sequence = advance_packet(sim, traces, sequence=expected_sequence)
        assert advanced is True
        assert sequence == expected_sequence + 1

    assert sim.time >= 1.0
    assert traces[-1]["kind"] == "heartbeat"
    assert any(
        event["event"] == "FAILURE_DETECTED"
        for event in traces[-1]["events"]
    )


def test_workspace_snapshot_reflects_congestion_and_failed_topology():
    from simulator import NetworkSimulator

    sim = NetworkSimulator(seed=42)
    sim.set_congestion("R1", "R3", 0.85)
    sim.fail_node("R5")

    snapshot = build_workspace_snapshot(
        sim,
        active_route=["H1", "R1", "R2", "R4", "R6"],
    )
    r1_r3 = next(link for link in snapshot["links"] if link["id"] == "R1-R3")
    r5 = next(node for node in snapshot["nodes"] if node["id"] == "R5")

    assert r1_r3["congestion"] == 0.85
    assert r5["status"] == "FAILED"
    assert snapshot["queue"]["scheduler"] == "Priority"
    assert "animateMotion" in _WORKSPACE_HTML
    assert "packet.route.map" in _WORKSPACE_HTML
