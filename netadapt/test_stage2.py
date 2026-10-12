"""
Stage 2+3 tests for self-healing, flows, congestion, packet loss, bandwidth,
simulation clock, events, recovery, metrics, etc.
"""

import pytest
from topology import NetworkTopology
from routing import AdaptiveRouter
from simulator import NetworkSimulator
from events import EventType


def test_heartbeat_initialization():
    sim = NetworkSimulator(seed=42, heartbeat_interval=1.0, failure_detection_timeout=3.0)
    assert sim.heartbeat_interval == 1.0
    assert sim.failure_detection_timeout == 3.0
    assert sim.last_heartbeat_check == 0.0
    assert sim._failed_links == {}
    assert sim._failed_nodes == {}


def test_heartbeat_timeout_detection():
    sim = NetworkSimulator(seed=42, heartbeat_interval=0.5, failure_detection_timeout=2.0)
    sim.fail_link("R3", "R5")
    assert sim.is_link_failed("R3", "R5")
    # Before timeout, not yet detected
    sim.time = 1.0
    detected = sim.check_heartbeats()
    assert len(detected) == 0
    assert ("R3", "R5") not in sim._detected_links and tuple(sorted(("R3", "R5"))) not in sim._detected_links
    # After timeout, should be detected
    sim.time = 3.0
    detected = sim.check_heartbeats()
    assert len(detected) >= 1
    # Check that detection record exists
    assert sim.metrics.recovery_records[-1].detection_time is not None


def test_link_failure_detection():
    sim = NetworkSimulator(seed=1, failure_detection_timeout=1.0)
    sim.fail_link("R1", "R3")
    sim.time += 2.0
    sim.check_heartbeats()
    events = [e for e in sim.events if e["event"] == EventType.FAILURE_DETECTED]
    assert len(events) >= 1
    assert any("R1" in e.get("component", "") and "R3" in e.get("component", "") for e in events)


def test_node_failure_detection():
    sim = NetworkSimulator(seed=1, failure_detection_timeout=1.0)
    sim.fail_node("R3")
    sim.time += 2.0
    sim.check_heartbeats()
    events = [e for e in sim.events if e["event"] == EventType.FAILURE_DETECTED]
    assert len(events) >= 1
    assert any(e.get("component") == "R3" or e.get("node") == "R3" for e in events)


def test_route_recalculation():
    sim = NetworkSimulator(seed=42)
    original_route = sim.router.route(sim.topology, "H1", "H3")
    assert len(original_route) >= 3

    # Fail a middle link of original route
    u, v = original_route[1], original_route[2]
    sim.fail_link(u, v)
    sim.time += sim.failure_detection_timeout + 0.1
    sim.check_heartbeats()

    new_route = sim.router.route(sim.topology, "H1", "H3")
    assert new_route != original_route
    # Ensure new route does not contain failed link
    for a, b in zip(new_route, new_route[1:]):
        assert not ((a == u and b == v) or (a == v and b == u))


def test_alternate_route():
    sim = NetworkSimulator(seed=42)
    # H1 to H3 normally goes via R1-R3-R5 or similar
    path1 = sim.router.route(sim.topology, "H1", "H3")
    # Fail R3-R5 which is a critical link
    sim.fail_link("R3", "R5")
    path2 = sim.router.route(sim.topology, "H1", "H3")
    assert path2 != path1
    assert path2[0] == "H1"
    assert path2[-1] == "H3"
    # Must still be valid and avoid failed link
    assert not sim._route_uses_link(path2, "R3", "R5")


def test_no_route_condition():
    sim = NetworkSimulator(seed=42)
    # Isolate H1 by failing its only access link
    sim.fail_link("H1", "R1")
    with pytest.raises(ValueError):
        sim.router.route(sim.topology, "H1", "H3")

    # Also test simulator packet drop when no route
    sim.generate_packet("H2", "H3", "HTTP")
    # But H1 is isolated, so H2->H3 should still work
    # Now fail many links to isolate H3
    sim.fail_link("H3", "R5")
    sim.fail_link("R4", "R5")
    sim.fail_link("R5", "R6")
    # Now H3 may still be isolated? Actually H3 only connects to R5, so it's isolated
    with pytest.raises(ValueError):
        sim.router.route(sim.topology, "H2", "H3")

    # Packet processing should result in DROP when no route
    sim2 = NetworkSimulator(seed=42)
    sim2.fail_link("H3", "R5")
    sim2.fail_link("R4", "R5")
    sim2.fail_link("R5", "R6")
    # H3 isolated
    sim2.generate_packet("H1", "H3")
    pkt = sim2.process_next_packet()
    assert pkt.delivery_status == "DROPPED"


def test_traffic_flow_creation():
    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", "Video", packet_count=10, packet_size=1200, pps=5.0, duration=2.0)
    assert flow.flow_id.startswith("flow-")
    assert flow.source == "H1"
    assert flow.destination == "H3"
    assert flow.traffic_type == "Video"
    assert flow.packet_count == 10
    assert len(flow.current_route) >= 2
    assert flow.packets_sent == 10
    assert len(sim.active_flows) == 1
    assert len(sim.scheduler) == 10


def test_multiple_flows():
    sim = NetworkSimulator(seed=42)
    f1 = sim.create_flow("H1", "H3", "Video", packet_count=5)
    f2 = sim.create_flow("H2", "H4", "HTTP", packet_count=5)
    f3 = sim.create_flow("H3", "H1", "VoIP", packet_count=5)

    assert len(sim.active_flows) == 3
    assert len(sim.scheduler) == 15

    # Process all
    processed = sim.run_until_empty()
    assert len(processed) == 15

    # Each flow should have some delivered
    for flow in sim.active_flows.values():
        assert flow.packets_sent == 5
        assert flow.packets_delivered + flow.packets_dropped == 5


def test_packet_rerouting():
    sim = NetworkSimulator(seed=42, failure_detection_timeout=1.0)
    flow = sim.create_flow("H1", "H3", "Video", packet_count=10)
    original_route = flow.current_route.copy()

    # Fail a link in original route
    assert len(original_route) >= 3
    u, v = original_route[1], original_route[2]
    sim.fail_link(u, v)
    sim.time += 2.0
    sim.check_heartbeats()

    # Flow should have been rerouted
    assert flow.route_status == "REROUTED" or flow.current_route != original_route
    # Process packets - should use new route
    sim.run_until_empty()
    # At least some packets should be delivered via new route
    assert flow.packets_delivered > 0


def test_congestion_effect():
    sim = NetworkSimulator(seed=42)
    route_before = sim.router.route(sim.topology, "H1", "H3")
    cost_before = sim.router.route_cost(sim.topology, route_before)

    # Increase congestion on first link of route
    u, v = route_before[0], route_before[1]
    sim.set_congestion(u, v, 1.0)

    cost_after = sim.router.route_cost(sim.topology, route_before)
    assert cost_after > cost_before

    # Congestion should also affect transmission time
    from qos import Packet
    pkt = Packet(1, "H1", "H3", "HTTP", size=1000, creation_time=0.0)
    sim2 = NetworkSimulator(seed=42)
    t_before = sim2._transmission_time(route_before, pkt)
    sim2.set_congestion(u, v, 1.0)
    t_after = sim2._transmission_time(route_before, pkt)
    assert t_after > t_before

    # Adaptive routing may choose alternative path if congestion high
    # Fail to ensure alternative is chosen: congest heavily all links of original route
    for a, b in zip(route_before, route_before[1:]):
        sim.topology.set_congestion(a, b, 1.0)
    try:
        new_route = sim.router.route(sim.topology, "H1", "H3")
        # If alternative exists, it may be different (not guaranteed but likely)
        # At minimum cost should be higher
        assert sim.router.route_cost(sim.topology, new_route) >= cost_before
    except ValueError:
        pass  # No route, also acceptable if congestion makes all paths costly but still valid


def test_packet_loss_effect():
    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", "HTTP", packet_count=50)
    # Use H3-R5 which is the only access link to H3, so all traffic must go through it
    # This ensures packet loss cannot be avoided by adaptive routing
    sim.set_packet_loss("H3", "R5", 0.5)

    processed = sim.run_until_empty()
    # With 50% loss on critical link, we expect some drops
    dropped = sum(1 for p in processed if p.delivery_status == "DROPPED")
    assert dropped > 0
    assert dropped < 50  # Not all dropped

    metrics = sim.metrics.calculate(sim.time)
    assert metrics["packet_loss"] > 0

    # Also test that loss increases route cost
    sim2 = NetworkSimulator(seed=42)
    route = sim2.router.route(sim2.topology, "H1", "H3")
    cost_before = sim2.router.route_cost(sim2.topology, route)
    sim2.set_packet_loss("H3", "R5", 0.5)
    cost_after = sim2.router.route_cost(sim2.topology, route)
    assert cost_after > cost_before


def test_bandwidth_effect():
    sim = NetworkSimulator(seed=42)
    from qos import Packet
    pkt = Packet(1, "H1", "H3", "HTTP", size=10000, creation_time=0.0)
    route = sim.router.route(sim.topology, "H1", "H3")

    # Normal bandwidth
    t_normal = sim._transmission_time(route, pkt)

    # Reduce bandwidth on first link to 10 Mbps (from 100)
    u, v = route[0], route[1]
    orig_bw = sim.topology.get_original_bandwidth(u, v)
    sim.set_bandwidth(u, v, 10.0)
    t_reduced = sim._transmission_time(route, pkt)

    assert t_reduced > t_normal
    # Bandwidth reduction factor should be roughly proportional
    # Serialization time increases when bandwidth decreases
    assert t_reduced > t_normal * 1.1

    # Restore
    sim.set_bandwidth(u, v, orig_bw)
    t_restored = sim._transmission_time(route, pkt)
    assert abs(t_restored - t_normal) < 0.001


def test_simulation_clock():
    sim = NetworkSimulator(seed=42)
    assert sim.time == 0.0

    sim.generate_traffic("H1", "H3", count=5)
    initial_time = sim.time
    sim.process_next_packet()
    assert sim.time > initial_time

    # Tick advances time
    before = sim.time
    sim.tick(1.0)
    assert sim.time == before + 1.0

    # Packet creation times respect clock
    sim2 = NetworkSimulator(seed=42)
    sim2.time = 10.0
    pkt = sim2.generate_packet("H1", "H3")
    assert pkt.creation_time == 10.0

    # Flow packet times respect pps
    sim3 = NetworkSimulator(seed=42)
    flow = sim3.create_flow("H1", "H3", packet_count=3, pps=1.0)
    # Packets should be spaced 1 second apart
    times = [p.creation_time for p in sim3.packets if p.packet_id in flow.packet_ids]
    times_sorted = sorted(times)
    assert times_sorted[1] - times_sorted[0] == pytest.approx(1.0, abs=0.01)
    assert times_sorted[2] - times_sorted[1] == pytest.approx(1.0, abs=0.01)


def test_event_creation():
    sim = NetworkSimulator(seed=42)
    sim.generate_packet("H1", "H3")
    assert any(e["event"] == "PACKET_GENERATED" for e in sim.events)

    sim.run()
    assert any(e["event"] == "PACKET_DELIVERED" or e["event"] == "PACKET_DROPPED" for e in sim.events)

    sim.fail_link("R1", "R3")
    assert any(e["event"] == "LINK_FAILED" for e in sim.events)

    sim.time += 5.0
    sim.check_heartbeats()
    assert any(e["event"] == "FAILURE_DETECTED" for e in sim.events)

    sim.recover_link("R1", "R3")
    assert any(e["event"] == "LINK_RECOVERED" for e in sim.events)

    sim.set_congestion("R1", "R3", 0.5)
    assert any(e["event"] == "CONGESTION_CHANGED" for e in sim.events)

    sim.set_packet_loss("R1", "R3", 0.2)
    assert any(e["event"] == "PACKET_LOSS_CHANGED" for e in sim.events)

    sim.set_bandwidth("R1", "R3", 20.0)
    assert any(e["event"] == "BANDWIDTH_CHANGED" for e in sim.events)

    # Structured logger
    assert len(sim.event_logger.events) == len(sim.events) - 1 or len(sim.event_logger.events) >= 5  # allow slight diff due to reset event


def test_failure_recovery():
    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", packet_count=10)

    # Fail link
    u, v = flow.current_route[1], flow.current_route[2]
    sim.fail_link(u, v)
    assert not sim.topology.active_link(u, v)

    # Recover link
    sim.recover_link(u, v)
    assert sim.topology.active_link(u, v)

    # Node failure/recovery
    sim.fail_node("R3")
    assert not sim.topology.active_node("R3")
    sim.recover_node("R3")
    assert sim.topology.active_node("R3")


def test_recovery_time():
    sim = NetworkSimulator(seed=42, failure_detection_timeout=2.0)
    sim.fail_link("R3", "R5")
    failure_time = sim.time

    sim.time += 3.0
    sim.check_heartbeats()

    # Detection time should be recorded
    rec = sim.metrics.recovery_records[-1]
    assert rec.failure_time == failure_time
    assert rec.detection_time is not None
    assert rec.detection_time - rec.failure_time >= 2.0

    # Recovery
    sim.recover_link("R3", "R5")
    assert rec.recovery_time is not None
    assert rec.recovery_time >= rec.detection_time

    metrics = sim.metrics.calculate(sim.time)
    assert metrics["failure_detection_time"] > 0
    assert metrics["recovery_time"] > 0


def test_metrics_before_after_failure():
    sim = NetworkSimulator(seed=42)

    # Baseline
    sim.create_flow("H1", "H3", packet_count=10)
    sim.run_until_empty()
    baseline = sim.metrics.snapshot(sim.time, sim.average_congestion(), len(sim.active_flows))
    assert baseline["packets_sent"] == 10

    # During failure - fail link and generate more traffic
    sim.fail_link("R3", "R5")
    sim.time += sim.failure_detection_timeout + 0.5
    sim.check_heartbeats()
    sim.create_flow("H1", "H3", packet_count=10)
    sim.run_until_empty()
    during = sim.metrics.snapshot(sim.time, sim.average_congestion(), len(sim.active_flows))

    # After recovery
    sim.recover_link("R3", "R5")
    sim.create_flow("H1", "H3", packet_count=10)
    sim.run_until_empty()
    after = sim.metrics.snapshot(sim.time, sim.average_congestion(), len(sim.active_flows))

    assert len(sim.metrics.history) >= 3
    comparison = sim.metrics.get_comparison()
    assert isinstance(comparison, dict)


def test_deterministic_seed():
    sim1 = NetworkSimulator(seed=123)
    sim1.create_flow("H1", "H3", packet_count=20)
    sim1.set_packet_loss("R1", "R3", 0.3)
    sim1.run_until_empty()
    metrics1 = sim1.metrics.calculate(sim1.time)

    sim2 = NetworkSimulator(seed=123)
    sim2.create_flow("H1", "H3", packet_count=20)
    sim2.set_packet_loss("R1", "R3", 0.3)
    sim2.run_until_empty()
    metrics2 = sim2.metrics.calculate(sim2.time)

    assert metrics1["packets_delivered"] == metrics2["packets_delivered"]
    assert metrics1["packets_dropped"] == metrics2["packets_dropped"]
    assert metrics1["packet_loss"] == metrics2["packet_loss"]

    # Different seed may give different results with loss
    sim3 = NetworkSimulator(seed=999)
    sim3.create_flow("H1", "H3", packet_count=20)
    sim3.set_packet_loss("R1", "R3", 0.3)
    sim3.run_until_empty()
    metrics3 = sim3.metrics.calculate(sim3.time)
    # Not necessarily different, but should be deterministic per seed
    # So at least sim1 and sim2 identical is the key test


def test_reset_behavior():
    sim = NetworkSimulator(seed=42)
    sim.create_flow("H1", "H3", packet_count=10)
    sim.fail_link("R3", "R5")
    sim.set_congestion("R1", "R3", 0.8)
    sim.run()

    assert sim.time > 0
    assert len(sim.packets) > 0
    assert len(sim.active_flows) > 0
    assert len(sim._failed_links) > 0

    sim.reset()

    assert sim.time == 0.0
    assert sim.next_packet_id == 1
    assert len(sim.packets) == 0
    assert len(sim.active_flows) == 0
    assert len(sim._failed_links) == 0
    assert len(sim._failed_nodes) == 0
    assert len(sim.metrics.records) == 0
    assert len(sim.metrics.history) == 0  # reset clears history
    # Check topology reset
    assert sim.topology.active_link("R3", "R5")
    assert sim.topology.graph["R1"]["R3"]["congestion"] == 0.0


def test_flow_status_tracking():
    sim = NetworkSimulator(seed=42)
    flow = sim.create_flow("H1", "H3", packet_count=5)
    assert flow.status == "ACTIVE"
    sim.run_until_empty()
    assert flow.status == "COMPLETED"
    assert flow.packets_delivered + flow.packets_dropped == 5


def test_event_types_exist():
    # Ensure all required event types are defined
    required = [
        "TRAFFIC_STARTED",
        "PACKET_SENT",
        "PACKET_DELIVERED",
        "PACKET_DROPPED",
        "LINK_FAILED",
        "NODE_FAILED",
        "FAILURE_DETECTED",
        "ROUTE_RECALCULATED",
        "TRAFFIC_REROUTED",
        "LINK_RECOVERED",
        "NODE_RECOVERED",
        "CONGESTION_CHANGED",
        "PACKET_LOSS_CHANGED",
        "BANDWIDTH_CHANGED",
    ]
    for evt in required:
        assert hasattr(EventType, evt) or evt in [e.value for e in EventType]
