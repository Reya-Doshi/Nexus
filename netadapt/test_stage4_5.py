"""
Stage 4 + Stage 5 tests.

Covers the advanced QoS laboratory (FIFO / Priority / WFQ, per-class metrics,
queue statistics) and the advanced adaptive routing system (Dijkstra,
Bellman-Ford, configurable route-cost weights) plus the QoS, routing and
combined experiments. All expectations are derived from real simulation output.
"""

import pytest

from topology import NetworkTopology
from routing import AdaptiveRouter, DEFAULT_ROUTING_WEIGHTS
from qos import (
    PRIORITIES,
    TRAFFIC_CLASSES,
    Packet,
    configure_priorities,
    create_scheduler,
    reset_priorities,
)
from metrics import jitter_from_latencies
from simulator import NetworkSimulator
from experiments import (
    COMPARISON_CONDITIONS,
    COMBINED_CONFIGURATIONS,
    WEIGHT_SCENARIO_CONDITIONS,
    WorkloadSpec,
    apply_conditions,
    build_simulator,
    class_metrics_frame,
    run_combined_experiment,
    run_qos_experiment,
    run_qos_stress_test,
    run_routing_comparison,
    run_routing_weight_experiment,
    run_workload,
)

SMALL_WORKLOAD = WorkloadSpec(packets_per_class=10, pps=50.0)
TINY_WORKLOAD = WorkloadSpec(packets_per_class=4, pps=50.0)


def _packet(packet_id: int, traffic_class: str) -> Packet:
    return Packet(packet_id, "H1", "H3", traffic_class, size=1000, creation_time=0.0)


# --------------------------------------------------------------------------
# Stage 5 -- routing algorithms
# --------------------------------------------------------------------------


def test_dijkstra_shortest_path():
    network = NetworkTopology()
    router = AdaptiveRouter(algorithm="dijkstra")

    path, cost = router.shortest_path(network, "H1", "H3")

    assert path == ["H1", "R1", "R3", "R5", "H3"]
    assert cost == pytest.approx(router.route_cost(network, path))
    assert router.get_algorithm() == "dijkstra"

    # Every hop must exist in the topology and be active.
    for u, v in zip(path, path[1:]):
        assert network.active_link(u, v)


def test_dijkstra_avoids_failed_link():
    network = NetworkTopology()
    router = AdaptiveRouter(algorithm="dijkstra")

    original = router.route(network, "H1", "H3")
    network.fail_link(original[1], original[2])

    rerouted = router.route(network, "H1", "H3")

    assert rerouted != original
    assert not any(
        (u == original[1] and v == original[2]) or (u == original[2] and v == original[1])
        for u, v in zip(rerouted, rerouted[1:])
    )


def test_bellman_ford_routes_and_avoids_failed_links():
    network = NetworkTopology()
    router = AdaptiveRouter(algorithm="bellman_ford")

    assert router.get_algorithm() == "bellman_ford"

    path, cost = router.shortest_path(network, "H1", "H3")
    assert path == ["H1", "R1", "R3", "R5", "H3"]
    assert cost == pytest.approx(70.5)

    network.fail_link("R3", "R5")
    rerouted, rerouted_cost = router.shortest_path(network, "H1", "H3")

    assert rerouted != path
    assert rerouted[0] == "H1" and rerouted[-1] == "H3"
    assert not any(
        (u, v) in {("R3", "R5"), ("R5", "R3")}
        for u, v in zip(rerouted, rerouted[1:])
    )
    assert rerouted_cost > cost
    assert rerouted_cost == pytest.approx(router.route_cost(network, rerouted))


def test_dijkstra_and_bellman_ford_agree_on_optimum():
    network = NetworkTopology()
    dij = AdaptiveRouter(algorithm="dijkstra")
    bf = AdaptiveRouter(algorithm="bellman_ford")

    for source, destination in [
        ("H1", "H3"),
        ("H1", "H4"),
        ("H2", "H3"),
        ("H1", "H2"),
        ("H2", "H4"),
    ]:
        dij_path, dij_cost = dij.shortest_path(network, source, destination)
        bf_path, bf_cost = bf.shortest_path(network, source, destination)

        # Both algorithms must find the same optimal cost ...
        assert dij_cost == pytest.approx(bf_cost)
        # ... and the same route through the shared topology.
        assert dij_path == bf_path

        for path in (dij_path, bf_path):
            for u, v in zip(path, path[1:]):
                assert network.active_link(u, v)


def test_invalid_routing_algorithm_rejected():
    with pytest.raises(ValueError):
        AdaptiveRouter(algorithm="floyd_warshall")

    router = AdaptiveRouter()
    with pytest.raises(ValueError):
        router.set_algorithm("bogus")

    network = NetworkTopology()
    with pytest.raises(ValueError):
        router.shortest_path(network, "H1", "H3", algorithm="nope")


# --------------------------------------------------------------------------
# Stage 5 -- routing weights
# --------------------------------------------------------------------------


def test_routing_weights_change_route_selection():
    sim = NetworkSimulator(seed=42, algorithm="dijkstra")
    apply_conditions(sim, WEIGHT_SCENARIO_CONDITIONS)

    # Default weights avoid the degraded links and take the longer bypass.
    default_route, _ = sim.router.shortest_path(sim.topology, "H1", "H3")

    # Hop-only cost ignores the degraded links and takes the 4-hop path.
    sim.set_routing_weights(latency=0.0, loss=0.0, congestion=0.0, bandwidth=0.0, hop=1.0)
    hop_route, hop_cost = sim.router.shortest_path(sim.topology, "H1", "H3")

    assert default_route != hop_route
    assert len(hop_route) - 1 == 4
    assert len(default_route) - 1 > 4

    # The condition-aware route must avoid both degraded links.
    degraded = {("R1", "R3"), ("R3", "R5")}
    used = {tuple(sorted(pair)) for pair in zip(default_route, default_route[1:])}
    assert not (degraded & used)

    # Bellman-Ford must follow the same weight changes.
    bf = NetworkSimulator(seed=42, algorithm="bellman_ford")
    apply_conditions(bf, WEIGHT_SCENARIO_CONDITIONS)
    assert bf.router.route(bf.topology, "H1", "H3") == default_route
    bf.set_routing_weights(latency=0.0, loss=0.0, congestion=0.0, bandwidth=0.0, hop=1.0)
    assert bf.router.route(bf.topology, "H1", "H3") == hop_route


def test_routing_weight_configuration_api():
    router = AdaptiveRouter()

    assert router.get_weights() == DEFAULT_ROUTING_WEIGHTS

    router.set_weights(loss=50.0)
    assert router.get_weights()["loss"] == 50.0
    assert router.get_weights()["hop"] == DEFAULT_ROUTING_WEIGHTS["hop"]

    router.configure_weights({"congestion": 0.0, "bandwidth": 12.5})
    assert router.get_weights()["congestion"] == 0.0
    assert router.get_weights()["bandwidth"] == 12.5

    with pytest.raises(ValueError):
        router.configure_weights({"jitter": 3.0})

    with pytest.raises(ValueError):
        router.set_weights(latency=-1.0)

    router.reset_weights()
    assert router.get_weights() == DEFAULT_ROUTING_WEIGHTS

    sim = NetworkSimulator(seed=42)
    sim.set_routing_weights(loss=99.0)
    assert sim.get_routing_weights()["loss"] == 99.0
    with pytest.raises(ValueError):
        sim.set_routing_weights(unknown_weight=1.0)

    # Route cost must react to a weight change.
    network = NetworkTopology()
    path = router.route(network, "H1", "H3")
    base = router.route_cost(network, path)
    network.set_congestion("H1", "R1", 0.5)
    router.set_weights(congestion=1000.0)
    assert router.route_cost(network, path) > base


def test_analyze_route_reports_route_metrics():
    network = NetworkTopology()
    router = AdaptiveRouter()
    path = router.route(network, "H1", "H3")

    info = router.analyze_route(network, path)

    assert info["route"] == path
    assert info["hops"] == len(path) - 1 == 4
    assert info["latency_ms"] == pytest.approx(40.0)
    assert info["packet_loss"] == 0.0
    assert info["congestion"] == 0.0
    assert info["min_bandwidth_mbps"] == 80.0
    assert info["cost"] == pytest.approx(router.route_cost(network, path))
    assert info["reachable"] is True

    # Loss and congestion along the route must show up in the analysis.
    network.set_packet_loss("R3", "R5", 0.5)
    network.set_congestion("R3", "R5", 1.0)
    degraded = router.analyze_route(network, path)
    assert degraded["packet_loss"] > 0.5
    assert degraded["congestion"] > 0.0

    route_info = router.route_info(network, "H1", "H3", "bellman_ford")
    assert route_info["algorithm"] == "Bellman-Ford"
    assert route_info["source"] == "H1"
    assert route_info["destination"] == "H3"
    assert route_info["cost"] > 0

    # Unreachable route is reported instead of raising.
    isolated = NetworkTopology()
    isolated.fail_link("H1", "R1")
    assert router.analyze_route(isolated, [])["reachable"] is False

    broken = AdaptiveRouter().analyze_route(isolated, ["H1", "R1", "H3"])
    assert broken["reachable"] is False


# --------------------------------------------------------------------------
# Stage 4 -- scheduler behaviour
# --------------------------------------------------------------------------


def test_fifo_ordering():
    queue = create_scheduler("fifo")

    packets = [_packet(1, "FTP"), _packet(2, "Emergency"), _packet(3, "HTTP")]
    for index, packet in enumerate(packets):
        queue.push(packet, now=index * 0.1)

    assert [queue.pop(now=0.3).packet_id for _ in range(3)] == [1, 2, 3]
    assert queue.service_order() == [1, 2, 3]
    assert queue.class_served_order() == ["FTP", "Emergency", "HTTP"]
    assert queue.empty()


def test_priority_queue_ordering():
    queue = create_scheduler("priority")

    for index, traffic_class in enumerate(["FTP", "HTTP", "Video", "VoIP", "Emergency"]):
        queue.push(_packet(index + 1, traffic_class), now=0.0)

    served = [queue.pop(now=0.0).traffic_type for _ in range(5)]

    assert served == ["Emergency", "VoIP", "Video", "HTTP", "FTP"]
    assert len(queue) == 0

    # Ties inside a class are broken by arrival order (stable queueing).
    ties = create_scheduler("priority")
    for packet_id in (10, 11, 12):
        ties.push(_packet(packet_id, "Video"))
    assert [ties.pop().packet_id for _ in range(3)] == [10, 11, 12]


def test_wfq_weighted_service_order():
    queue = create_scheduler("wfq")

    traffic_classes = ["Emergency", "VoIP", "Video", "HTTP", "FTP"]
    packet_id = 0
    for _ in range(10):
        for traffic_class in traffic_classes:
            packet_id += 1
            queue.push(_packet(packet_id, traffic_class), now=0.0)

    served = [queue.pop(now=0.0).traffic_type for _ in range(len(queue) + 0)]

    first_served = served[:20]
    assert first_served.count("Emergency") > first_served.count("FTP")
    assert first_served.count("VoIP") >= first_served.count("HTTP")

    # Every class is eventually served: no starvation.
    assert set(served) == set(traffic_classes)
    assert len(served) == 50


def test_wfq_configurable_weights_change_service():
    default_queue = create_scheduler("wfq")
    assert default_queue.get_weights()["Emergency"] > default_queue.get_weights()["FTP"]

    inverted = create_scheduler(
        "wfq",
        weights={
            "Emergency": 1.0,
            "VoIP": 1.0,
            "Video": 1.0,
            "HTTP": 1.0,
            "FTP": 9.0,
        },
    )
    assert inverted.get_weights()["FTP"] == 9.0

    packet_id = 0
    for _ in range(6):
        for traffic_class in ["Emergency", "VoIP", "Video", "HTTP", "FTP"]:
            packet_id += 1
            default_queue.push(_packet(packet_id, traffic_class), now=0.0)
            inverted.push(_packet(packet_id, traffic_class), now=0.0)

    default_order = [default_queue.pop(now=0.0).traffic_type for _ in range(10)]
    inverted_order = [inverted.pop(now=0.0).traffic_type for _ in range(10)]

    assert default_order.count("Emergency") > default_order.count("FTP")
    assert inverted_order.count("FTP") > inverted_order.count("Emergency")
    assert inverted_order.count("FTP") >= 5

    inverted.set_weights({"FTP": 1.0})
    assert inverted.get_weights()["FTP"] == 1.0


def test_queue_statistics_fifo():
    queue = create_scheduler("fifo")

    for packet_id in (1, 2, 3):
        queue.push(_packet(packet_id, "HTTP"), now=0.0)

    assert len(queue) == 3
    first = queue.pop(now=0.5)
    assert first.queue_wait_time == pytest.approx(0.5)

    stats = queue.queue_statistics()

    assert stats["scheduler"] == "FIFO"
    assert stats["packets_enqueued"] == 3
    assert stats["packets_served"] == 1
    assert stats["current_queue_length"] == 2
    assert stats["max_queue_length"] == 3
    assert stats["total_waiting_time"] == pytest.approx(0.5)
    assert stats["average_waiting_time"] == pytest.approx(0.5)
    assert stats["max_waiting_time"] == pytest.approx(0.5)

    per_class = queue.class_statistics()
    assert per_class["HTTP"]["enqueued"] == 3.0
    assert per_class["HTTP"]["served"] == 1.0
    assert per_class["HTTP"]["current_length"] == 2.0
    assert per_class["FTP"]["enqueued"] == 0.0

    queue.reset_stats()
    assert queue.queue_statistics()["packets_served"] == 0


def test_queue_statistics_priority_and_wfq():
    for scheduler_name in ("priority", "wfq"):
        queue = create_scheduler(scheduler_name)

        packet_id = 0
        for _ in range(4):
            for traffic_class in TRAFFIC_CLASSES:
                packet_id += 1
                queue.push(_packet(packet_id, traffic_class), now=0.0)

        total = len(queue)
        assert total == 20

        while not queue.empty():
            queue.pop(now=0.0)

        stats = queue.queue_statistics()
        assert stats["packets_enqueued"] == total
        assert stats["packets_served"] == total
        assert stats["current_queue_length"] == 0
        assert stats["max_queue_length"] == total
        assert stats["max_waiting_time"] >= stats["average_waiting_time"]

        per_class = queue.class_statistics()
        assert sum(values["served"] for values in per_class.values()) == total
        assert sum(values["enqueued"] for values in per_class.values()) == total
        assert all(values["served"] == 4.0 for values in per_class.values())


def test_configurable_traffic_class_priorities():
    original = dict(PRIORITIES)
    try:
        configure_priorities({"Emergency": 1.0, "FTP": 5.0})
        assert PRIORITIES["FTP"] == 5.0
        assert PRIORITIES["Emergency"] == 1.0

        low = Packet(1, "H1", "H3", "Emergency")
        high = Packet(2, "H1", "H3", "FTP")
        assert high.priority > low.priority

        queue = create_scheduler("priority")
        queue.push(low)
        queue.push(high)
        assert queue.pop().packet_id == 2

        with pytest.raises(ValueError):
            configure_priorities({"Gaming": 9.0})
    finally:
        reset_priorities()

    assert PRIORITIES == original
    assert Packet(3, "H1", "H3", "Emergency").priority == original["Emergency"]


# --------------------------------------------------------------------------
# Stage 4 -- per-class / per-queue metrics from the simulation
# --------------------------------------------------------------------------


def test_traffic_class_metrics():
    sim = build_simulator(SMALL_WORKLOAD, scheduler="priority", conditions=COMPARISON_CONDITIONS)
    run_workload(sim, SMALL_WORKLOAD)

    metrics = sim.class_metrics()

    assert set(metrics) == set(TRAFFIC_CLASSES)

    for traffic_class in TRAFFIC_CLASSES:
        values = metrics[traffic_class]
        assert values["packets_sent"] == SMALL_WORKLOAD.packets_per_class
        assert values["packets_delivered"] + values["packets_dropped"] == values["packets_sent"]
        assert 0.0 <= values["packet_delivery_ratio"] <= 100.0
        assert 0.0 <= values["packet_loss"] <= 100.0
        assert values["average_latency"] > 0.0
        assert values["throughput"] > 0.0
        assert values["jitter"] >= 0.0
        assert values["average_queue_wait"] >= 0.0
        assert values["max_queue_wait"] >= values["average_queue_wait"]

    frame = class_metrics_frame(sim, SMALL_WORKLOAD)
    assert len(frame) == len(TRAFFIC_CLASSES)
    assert {"Traffic Class", "Avg Latency (ms)", "Jitter (ms)", "PDR (%)"} <= set(frame.columns)
    assert (frame["Packets Sent"] == SMALL_WORKLOAD.packets_per_class).all()

    # Aggregated metrics stay consistent with the per-class breakdown.
    overall = sim.metrics.calculate(sim.time, sim.average_congestion())
    assert overall["packets_sent"] == sum(v["packets_sent"] for v in metrics.values())
    assert overall["packets_delivered"] == sum(v["packets_delivered"] for v in metrics.values())
    assert "jitter" in overall


def test_jitter_and_queue_waiting_time_metrics():
    assert jitter_from_latencies([1.0, 2.0, 4.0]) == pytest.approx(1.5)
    assert jitter_from_latencies([0.5]) == 0.0
    assert jitter_from_latencies([]) == 0.0

    sim = build_simulator(SMALL_WORKLOAD, scheduler="priority", conditions=COMPARISON_CONDITIONS)
    run_workload(sim, SMALL_WORKLOAD)

    metrics = sim.class_metrics()

    # Congested conditions must produce real queueing, not zeros.
    queue_waits = [values["average_queue_wait"] for values in metrics.values()]
    assert max(queue_waits) > 0.0
    assert any(values["jitter"] > 0.0 for values in metrics.values())

    # Priority queueing must punish the lowest-priority class.
    assert metrics["Emergency"]["average_queue_wait"] < metrics["FTP"]["average_queue_wait"]

    queue_stats = sim.scheduler_statistics()
    assert queue_stats["packets_served"] == SMALL_WORKLOAD.total_packets
    assert queue_stats["max_queue_length"] > 0
    assert queue_stats["average_waiting_time"] > 0.0


# --------------------------------------------------------------------------
# Stage 4 -- QoS experiments
# --------------------------------------------------------------------------


def test_qos_experiment_same_workload_all_schedulers():
    result = run_qos_experiment(spec=SMALL_WORKLOAD)

    assert len(result.summary) == 3
    assert set(result.summary["Scheduler"]) == {"FIFO", "Priority Queue", "WFQ"}
    assert list(result.schedulers) == ["fifo", "priority", "wfq"]

    # Same workload and same conditions for every scheduler.
    assert result.summary["Packets Sent"].nunique() == 1
    assert result.summary["Packets Sent"].iloc[0] == SMALL_WORKLOAD.total_packets
    assert result.summary["Delivered"].nunique() == 1
    assert result.summary["Route"].nunique() == 1
    assert result.summary["Route Cost"].nunique() == 1

    assert set(result.class_metrics) == {"FIFO", "Priority Queue", "WFQ"}
    assert set(result.queue_statistics["Configuration"]) == {"FIFO", "Priority Queue", "WFQ"}

    for label, frame in result.class_metrics.items():
        assert len(frame) == len(TRAFFIC_CLASSES)
        assert (frame["Packets Sent"] == SMALL_WORKLOAD.packets_per_class).all()

    # Schedulers must actually treat classes differently.
    def wait(label, traffic_class):
        frame = result.class_metrics[label].set_index("Traffic Class")
        return frame.loc[traffic_class, "Avg Queue Wait (ms)"]

    priority_gap = wait("Priority Queue", "FTP") - wait("Priority Queue", "Emergency")
    fifo_gap = wait("FIFO", "FTP") - wait("FIFO", "Emergency")

    assert priority_gap > 0.0
    assert fifo_gap < priority_gap
    assert wait("WFQ", "Emergency") < wait("FIFO", "Emergency")
    assert wait("WFQ", "FTP") > wait("WFQ", "Emergency")

    assert result.describe()["workload"]["total_packets"] == SMALL_WORKLOAD.total_packets


def test_qos_experiment_deterministic_seed():
    first = run_qos_experiment(spec=TINY_WORKLOAD)
    second = run_qos_experiment(spec=TINY_WORKLOAD)

    assert first.summary.to_dict("records") == second.summary.to_dict("records")
    assert first.queue_statistics.to_dict("records") == second.queue_statistics.to_dict("records")

    for label in first.class_metrics:
        assert (
            first.class_metrics[label].to_dict("records")
            == second.class_metrics[label].to_dict("records")
        )

    # A different seed changes the random loss realisation, not the schedule.
    other = run_qos_experiment(spec=WorkloadSpec(packets_per_class=10, pps=50.0, seed=7))
    assert set(other.summary["Scheduler"]) == {"FIFO", "Priority Queue", "WFQ"}
    assert other.summary["Route"].nunique() == 1


def test_qos_stress_test_under_congestion():
    spec = WorkloadSpec(packets_per_class=10, packet_size=1200, pps=100.0)
    result = run_qos_stress_test(spec=spec)

    assert result.kind == "QoS congestion stress test"
    assert len(result.summary) == 3

    # The stress conditions must actually congest the network.
    assert (result.summary["Max Queue Length"] > 0).all()
    assert (result.summary["Avg Queue Wait (ms)"] > 0).all()
    assert (result.summary["Packet Loss (%)"] > 0).all()
    assert (result.summary["Packet Loss (%)"] < 100).all()

    # Identical workload/loss realisation -> identical delivery per scheduler.
    assert result.summary["Delivered"].nunique() == 1
    assert result.summary["Dropped"].nunique() == 1

    # Priority queueing protects the emergency class under congestion.
    priority_frame = result.class_metrics["Priority Queue"].set_index("Traffic Class")
    fifo_frame = result.class_metrics["FIFO"].set_index("Traffic Class")
    assert (
        priority_frame.loc["Emergency", "Avg Queue Wait (ms)"]
        < priority_frame.loc["FTP", "Avg Queue Wait (ms)"]
    )
    assert (
        priority_frame.loc["Emergency", "Avg Latency (ms)"]
        < fifo_frame.loc["Emergency", "Avg Latency (ms)"]
    )

    assert result.conditions.describe()["label"].startswith("Stress")


# --------------------------------------------------------------------------
# Stage 5 -- routing experiments
# --------------------------------------------------------------------------


def test_routing_experiment_dijkstra_vs_bellman_ford_and_deterministic():
    result = run_routing_comparison()

    assert len(result.summary) == 2
    assert list(result.algorithms) == ["dijkstra", "bellman_ford"]
    assert result.routes_agree is True
    assert result.summary["Route"].nunique() == 1
    assert result.summary["Route Cost"].nunique() == 1
    assert result.summary["Hops"].nunique() == 1
    assert (result.summary["Hops"] > 1).all()

    # Both algorithms actually did work.
    assert (result.summary["Algorithm Relaxations"] > 0).all()
    assert result.summary["Algorithm Iterations"].max() >= 1

    # Route metric columns are reported from the simulation / topology.
    for column in (
        "Route Cost",
        "Hops",
        "Route Latency (ms)",
        "Route Loss (%)",
        "Route Congestion (%)",
        "Avg Latency (ms)",
        "Packet Loss (%)",
        "PDR (%)",
        "Jitter (ms)",
        "Throughput (B/s)",
    ):
        assert column in result.summary.columns

    repeat = run_routing_comparison()
    assert repeat.summary.to_dict("records") == result.summary.to_dict("records")


def test_routing_weight_experiment_changes_selected_route():
    result = run_routing_weight_experiment()

    assert len(result.summary) == len(result.runs)
    assert result.distinct_routes.__len__() >= 2

    default_row = result.summary.set_index("Weight Config").loc["Balanced (default)"]
    hop_row = result.summary.set_index("Weight Config").loc["Hop-minimising"]
    bandwidth_row = result.summary.set_index("Weight Config").loc["Bandwidth-optimised"]

    assert default_row["Route"] != hop_row["Route"]
    assert hop_row["Hops"] == result.summary["Hops"].min() == 4
    assert default_row["Hops"] > hop_row["Hops"]

    # The condition-aware configuration must avoid the degraded links.
    degraded = ("R1", "R3", "R5")
    assert "R1 → R3 → R5" not in default_row["Route"]

    # Weight changes must alter the computed route cost too.
    assert default_row["Route Cost"] != hop_row["Route Cost"]
    assert hop_row["Route Cost"] == pytest.approx(4.0)
    assert bandwidth_row["Route Cost"] > 0

    repeat = run_routing_weight_experiment()
    assert repeat.summary.to_dict("records") == result.summary.to_dict("records")


# --------------------------------------------------------------------------
# Stage 4 + Stage 5 -- combined experiment
# --------------------------------------------------------------------------


def test_combined_experiment_six_configurations():
    spec = WorkloadSpec(packets_per_class=6, pps=50.0)
    result = run_combined_experiment(spec=spec)

    assert len(result.summary) == 6
    assert len(result.configurations) == 6
    assert set(result.configurations) == set(COMBINED_CONFIGURATIONS)

    expected_labels = {
        "Dijkstra + FIFO",
        "Dijkstra + Priority Queue",
        "Dijkstra + WFQ",
        "Bellman-Ford + FIFO",
        "Bellman-Ford + Priority Queue",
        "Bellman-Ford + WFQ",
    }
    assert set(result.summary["Configuration"]) == expected_labels
    assert set(result.class_metrics) == expected_labels

    # Every configuration ran the same workload on the same route.
    assert result.summary["Packets Sent"].nunique() == 1
    assert result.summary["Route"].nunique() == 1
    assert result.summary["Hops"].nunique() == 1
    assert result.summary["Route Cost"].nunique() == 1

    for column in ("Avg Latency (ms)", "Jitter (ms)", "Throughput (B/s)", "PDR (%)"):
        assert result.summary[column].notna().all()

    # The routing algorithm must not change the metrics of a given scheduler
    # (both find the same optimum), so results pair up across algorithms.
    indexed = result.summary.set_index("Configuration")
    for scheduler in ("FIFO", "Priority Queue", "WFQ"):
        dij = indexed.loc[f"Dijkstra + {scheduler}"]
        bf = indexed.loc[f"Bellman-Ford + {scheduler}"]
        assert dij["Avg Latency (ms)"] == pytest.approx(bf["Avg Latency (ms)"])
        assert dij["Throughput (B/s)"] == pytest.approx(bf["Throughput (B/s)"])
        assert dij["PDR (%)"] == pytest.approx(bf["PDR (%)"])

    # The QoS scheduler must change per-class treatment in every combination.
    fifo_frame = result.class_metrics["Bellman-Ford + FIFO"].set_index("Traffic Class")
    priority_frame = result.class_metrics["Bellman-Ford + Priority Queue"].set_index("Traffic Class")
    assert (
        priority_frame.loc["Emergency", "Avg Queue Wait (ms)"]
        < fifo_frame.loc["Emergency", "Avg Queue Wait (ms)"]
    )

    assert len(result.queue_statistics) == 6
    assert result.describe()["workload"]["total_packets"] == spec.total_packets

    repeat = run_combined_experiment(spec=spec)
    assert repeat.summary.to_dict("records") == result.summary.to_dict("records")


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
