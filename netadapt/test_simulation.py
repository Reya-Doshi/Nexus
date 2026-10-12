from topology import NetworkTopology
from routing import AdaptiveRouter
from qos import Packet, FIFOQueue, PriorityQueue
from simulator import NetworkSimulator
from metrics import Metrics


def test_topology():
    network = NetworkTopology()

    assert network.graph.number_of_nodes() == 10
    assert network.graph.number_of_edges() > 0


def test_network_connected():
    network = NetworkTopology()
    assert network.active_graph().number_of_nodes() == 10


def test_basic_route():
    network = NetworkTopology()
    router = AdaptiveRouter()

    path = router.route(network, "H1", "H3")

    assert path[0] == "H1"
    assert path[-1] == "H3"
    assert len(path) >= 2


def test_failed_link_avoided():
    network = NetworkTopology()
    router = AdaptiveRouter()

    original = router.route(network, "H1", "H3")

    assert len(original) >= 4

    # Fail an internal link, not the source/destination access link.
    u, v = original[1], original[2]
    network.fail_link(u, v)

    new_path = router.route(network, "H1", "H3")

    assert new_path != original
    assert u not in new_path or v not in new_path


def test_failed_node_avoided():
    network = NetworkTopology()
    router = AdaptiveRouter()

    network.fail_node("R2")

    path = router.route(network, "H1", "H3")

    assert "R2" not in path


def test_congestion_changes_cost():
    network = NetworkTopology()
    router = AdaptiveRouter()

    path = router.route(network, "H1", "H3")
    base_cost = router.route_cost(network, path)

    for u, v in zip(path, path[1:]):
        network.set_congestion(u, v, 1.0)
        break

    changed_cost = router.route_cost(network, path)

    assert changed_cost > base_cost


def test_fifo():
    queue = FIFOQueue()

    p1 = Packet(1, "H1", "H2", "HTTP")
    p2 = Packet(2, "H1", "H2", "FTP")

    queue.push(p1)
    queue.push(p2)

    assert queue.pop().packet_id == 1
    assert queue.pop().packet_id == 2


def test_priority():
    queue = PriorityQueue()

    low = Packet(1, "H1", "H2", "FTP")
    high = Packet(2, "H1", "H2", "Emergency")

    queue.push(low)
    queue.push(high)

    assert queue.pop().packet_id == 2
    assert queue.pop().packet_id == 1


def test_packet_generation():
    sim = NetworkSimulator(seed=42)

    packet = sim.generate_packet(
        "H1",
        "H3",
        "HTTP",
    )

    assert packet.packet_id == 1
    assert packet.source == "H1"
    assert packet.destination == "H3"


def test_packet_delivery():
    sim = NetworkSimulator(seed=42)

    sim.generate_traffic(
        "H1",
        "H3",
        count=5,
        traffic_type="HTTP",
    )

    processed = sim.run()

    assert len(processed) == 5
    assert all(
        p.delivery_status in {"DELIVERED", "DROPPED"}
        for p in processed
    )


def test_packet_loss_metric():
    metrics = Metrics()

    metrics.record(
        1, 0.0, 1.0, 1.0, 1000, "DELIVERED"
    )
    metrics.record(
        2, 0.0, None, None, 1000, "DROPPED"
    )

    result = metrics.calculate(1.0)

    assert result["packets_sent"] == 2
    assert result["packets_delivered"] == 1
    assert result["packets_dropped"] == 1
    assert result["packet_delivery_ratio"] == 50.0
    assert result["packet_loss"] == 50.0


def test_latency():
    metrics = Metrics()

    metrics.record(
        1, 0.0, 0.5, 0.5, 1000, "DELIVERED"
    )

    result = metrics.calculate(1.0)

    assert result["average_latency"] == 0.5


def test_throughput():
    metrics = Metrics()

    metrics.record(
        1, 0.0, 1.0, 1.0, 1000, "DELIVERED"
    )

    result = metrics.calculate(1.0)

    assert result["throughput"] == 1000.0


def test_simulator_reset():
    sim = NetworkSimulator(seed=42)

    sim.generate_packet("H1", "H3")
    sim.run()

    sim.reset()

    assert sim.time == 0.0
    assert sim.next_packet_id == 1
    assert len(sim.packets) == 0
    assert len(sim.metrics.records) == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))