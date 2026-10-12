"""
Experiment engine for NetAdapt (Stage 4 + Stage 5).

Every experiment in this module runs the *real* simulation engine
(:class:`simulator.NetworkSimulator`). Nothing is hardcoded: all reported values
are read back from packet records, queue counters and the routing engine after a
workload has actually been simulated.

Guarantees that make the comparisons meaningful:

* the same topology,
* the same traffic workload (identical packet count, size, classes, timings),
* the same network conditions,
* the same random seed, and
* the same pre-drawn packet-loss realisation (see ``Packet.loss_roll``),

are used for every configuration being compared. Only the dimension under test
(scheduler / routing algorithm / routing weights) changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from qos import (
    SCHEDULER_KEYS,
    TRAFFIC_CLASSES,
    normalize_scheduler_name,
)
from routing import (
    ALGORITHM_LABELS,
    DEFAULT_ROUTING_WEIGHTS,
    normalize_algorithm,
)
from simulator import NetworkSimulator

# --------------------------------------------------------------------------
# Dimensions under test
# --------------------------------------------------------------------------

QOS_SCHEDULERS: Tuple[str, ...] = tuple(SCHEDULER_KEYS)
ROUTING_ALGORITHMS: Tuple[str, ...] = ("dijkstra", "bellman_ford")

QOS_LABELS: Dict[str, str] = {
    "fifo": "FIFO",
    "priority": "Priority Queue",
    "wfq": "WFQ",
}

#: The six routing x QoS combinations required by the combined experiment.
COMBINED_CONFIGURATIONS: Tuple[Tuple[str, str], ...] = tuple(
    (algorithm, scheduler)
    for algorithm in ROUTING_ALGORITHMS
    for scheduler in QOS_SCHEDULERS
)

#: Named routing weight configurations used to demonstrate weight sensitivity.
ROUTING_WEIGHT_PRESETS: Dict[str, Dict[str, float]] = {
    "Balanced (default)": dict(DEFAULT_ROUTING_WEIGHTS),
    "Latency-optimised": {
        "latency": 10.0,
        "loss": 0.0,
        "congestion": 0.0,
        "bandwidth": 0.0,
        "hop": 0.0,
    },
    "Loss-avoiding": {
        "latency": 0.001,
        "loss": 500.0,
        "congestion": 0.0,
        "bandwidth": 0.0,
        "hop": 0.0,
    },
    "Congestion-avoiding": {
        "latency": 0.001,
        "loss": 0.0,
        "congestion": 500.0,
        "bandwidth": 0.0,
        "hop": 0.0,
    },
    "Bandwidth-optimised": {
        "latency": 0.0,
        "loss": 0.0,
        "congestion": 0.0,
        "bandwidth": 500.0,
        "hop": 0.0,
    },
    "Hop-minimising": {
        "latency": 0.0,
        "loss": 0.0,
        "congestion": 0.0,
        "bandwidth": 0.0,
        "hop": 1.0,
    },
}


# --------------------------------------------------------------------------
# Workload and network conditions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkloadSpec:
    """The traffic workload offered to every configuration under test."""

    source: str = "H1"
    destination: str = "H3"
    classes: Tuple[str, ...] = tuple(TRAFFIC_CLASSES)
    packets_per_class: int = 20
    packet_size: int = 1200
    pps: float = 50.0
    seed: int = 42

    @property
    def total_packets(self) -> int:
        return len(self.classes) * self.packets_per_class

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "traffic_classes": list(self.classes),
            "packets_per_class": self.packets_per_class,
            "total_packets": self.total_packets,
            "packet_size_bytes": self.packet_size,
            "packets_per_second": self.pps,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class LinkConditionGroup:
    """One group of links sharing the same condition values."""

    links: Tuple[Tuple[str, str], ...]
    congestion: float = 0.0
    packet_loss: float = 0.0
    bandwidth: Optional[float] = None

    def describe(self) -> Dict[str, Any]:
        return {
            "links": [f"{u}-{v}" for u, v in self.links],
            "congestion": self.congestion,
            "packet_loss": self.packet_loss,
            "bandwidth_mbps": self.bandwidth,
        }


@dataclass(frozen=True)
class NetworkConditions:
    """
    Link conditions applied identically to every configuration under test.

    Links not covered by any group keep their topology defaults.
    """

    label: str = "Default conditions"
    groups: Tuple[LinkConditionGroup, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "groups": [group.describe() for group in self.groups],
        }


def uniform_conditions(
    links: Sequence[Tuple[str, str]],
    congestion: float = 0.0,
    packet_loss: float = 0.0,
    bandwidth: Optional[float] = None,
    label: str = "Custom conditions",
) -> NetworkConditions:
    """Convenience builder: one condition group applied to every given link."""
    return NetworkConditions(
        label=label,
        groups=(
            LinkConditionGroup(
                links=tuple((u, v) for u, v in links),
                congestion=congestion,
                packet_loss=packet_loss,
                bandwidth=bandwidth,
            ),
        ),
    )


CORE_LINKS: Tuple[Tuple[str, str], ...] = (
    ("H1", "R1"),
    ("R1", "R2"),
    ("R1", "R3"),
    ("R2", "R4"),
    ("R3", "R4"),
    ("R3", "R5"),
    ("R4", "R5"),
    ("R4", "R6"),
    ("R5", "R6"),
    ("H3", "R5"),
)

#: Moderate congestion used by the QoS comparison and the combined lab.
COMPARISON_CONDITIONS = uniform_conditions(
    links=(("R1", "R3"), ("R3", "R5"), ("R2", "R4"), ("R4", "R5")),
    congestion=0.6,
    bandwidth=10.0,
    label="Congested core (60% congestion, 10 Mbps on alternate link)",
)

#: Heavy congestion used by the QoS stress test -- every class competes.
STRESS_CONDITIONS = uniform_conditions(
    links=CORE_LINKS,
    congestion=0.9,
    packet_loss=0.05,
    bandwidth=25.0,
    label="Stress: 90% congestion, 5% loss, 25 Mbps",
)

#: Weight-sensitivity scenario: degraded links so routing weights matter.
WEIGHT_SCENARIO_CONDITIONS = NetworkConditions(
    label="Degraded core (60% congestion + 35% loss, 10 Mbps alternate link)",
    groups=(
        LinkConditionGroup(
            links=(("R1", "R3"), ("R3", "R5")),
            congestion=0.6,
            packet_loss=0.35,
        ),
        LinkConditionGroup(
            links=(("R2", "R4"), ("R4", "R5"), ("R4", "R6")),
            bandwidth=10.0,
        ),
    ),
)


DEFAULT_SPEC = WorkloadSpec()
DEFAULT_WEIGHT_SPEC = WorkloadSpec(packets_per_class=5, pps=50.0)


# --------------------------------------------------------------------------
# Simulation helpers
# --------------------------------------------------------------------------


def apply_conditions(sim: NetworkSimulator, conditions: NetworkConditions) -> None:
    """Apply identical link conditions to a freshly built simulator."""
    for group in conditions.groups:
        for u, v in group.links:
            sim.set_link_conditions(
                u,
                v,
                congestion=group.congestion,
                packet_loss=group.packet_loss,
                bandwidth=group.bandwidth,
            )


def build_simulator(
    spec: WorkloadSpec,
    scheduler: str = "fifo",
    algorithm: str = "dijkstra",
    conditions: Optional[NetworkConditions] = None,
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
    failure_detection_timeout: Optional[float] = None,
    heartbeat_interval: Optional[float] = None,
) -> NetworkSimulator:
    """
    Create a configured simulator (deterministic for ``spec.seed``).

    ``failure_detection_timeout`` / ``heartbeat_interval`` are only forwarded
    when supplied, so configurations that never inject failures keep the engine
    defaults and behave exactly as before.
    """
    options: Dict[str, Any] = {
        "seed": spec.seed,
        "scheduler": normalize_scheduler_name(scheduler),
        "algorithm": normalize_algorithm(algorithm),
    }

    if failure_detection_timeout is not None:
        options["failure_detection_timeout"] = float(failure_detection_timeout)

    if heartbeat_interval is not None:
        options["heartbeat_interval"] = float(heartbeat_interval)

    sim = NetworkSimulator(**options)

    if conditions is not None:
        apply_conditions(sim, conditions)

    if routing_weights:
        sim.router.configure_weights(routing_weights)

    if wfq_weights:
        sim.set_wfq_weights(wfq_weights)

    if priorities:
        sim.set_priority_config(priorities)

    return sim


def run_workload(sim: NetworkSimulator, spec: WorkloadSpec) -> NetworkSimulator:
    """
    Offer ``spec`` to ``sim``: one flow per traffic class, packets emitted
    round-robin in arrival order so all classes compete simultaneously, then
    drain the scheduler completely.
    """
    sim.generate_interleaved_traffic(
        spec.source,
        spec.destination,
        classes=list(spec.classes),
        packets_per_class=spec.packets_per_class,
        packet_size=spec.packet_size,
        pps=spec.pps,
    )

    sim.run_until_empty()
    return sim


# --------------------------------------------------------------------------
# Result shaping
# --------------------------------------------------------------------------


def class_metrics_frame(sim: NetworkSimulator, spec: WorkloadSpec) -> pd.DataFrame:
    """Per traffic-class QoS metrics for a simulated workload."""
    metrics = sim.metrics.class_metrics(sim.time, list(spec.classes))

    rows: List[Dict[str, Any]] = []
    for traffic_class in spec.classes:
        values = metrics.get(traffic_class, {})
        rows.append(
            {
                "Traffic Class": traffic_class,
                "Packets Sent": int(values.get("packets_sent", 0)),
                "Delivered": int(values.get("packets_delivered", 0)),
                "Dropped": int(values.get("packets_dropped", 0)),
                "Avg Latency (ms)": values.get("average_latency", 0.0) * 1000.0,
                "Jitter (ms)": values.get("jitter", 0.0) * 1000.0,
                "Throughput (B/s)": values.get("throughput", 0.0),
                "Packet Loss (%)": values.get("packet_loss", 0.0),
                "PDR (%)": values.get("packet_delivery_ratio", 0.0),
                "Avg Queue Wait (ms)": values.get("average_queue_wait", 0.0) * 1000.0,
                "Max Queue Wait (ms)": values.get("max_queue_wait", 0.0) * 1000.0,
            }
        )

    return pd.DataFrame(rows)


def queue_statistics_frame(sim: NetworkSimulator, label: str) -> Dict[str, Any]:
    """Queue statistics of the scheduler that actually ran the workload."""
    stats = sim.scheduler_statistics()
    return {
        "Configuration": label,
        "Scheduler": stats.get("scheduler", sim.scheduler_name),
        "Packets Enqueued": int(stats.get("packets_enqueued", 0)),
        "Packets Served": int(stats.get("packets_served", 0)),
        "Current Queue Length": int(stats.get("current_queue_length", 0)),
        "Max Queue Length": int(stats.get("max_queue_length", 0)),
        "Avg Waiting Time (ms)": stats.get("average_waiting_time", 0.0) * 1000.0,
        "Max Waiting Time (ms)": stats.get("max_waiting_time", 0.0) * 1000.0,
    }


def queue_class_frame(sim: NetworkSimulator) -> pd.DataFrame:
    """Per traffic-class queue occupancy / service counters of the scheduler."""
    stats = sim.scheduler_class_statistics()
    rows = [
        {
            "Traffic Class": traffic_class,
            "Enqueued": int(values["enqueued"]),
            "Served": int(values["served"]),
            "Current Queue Length": int(values["current_length"]),
        }
        for traffic_class, values in stats.items()
    ]
    return pd.DataFrame(rows)


def _route_snapshot(sim: NetworkSimulator, spec: WorkloadSpec) -> Dict[str, Any]:
    """Route selected by the engine for the workload's source/destination."""
    try:
        info = sim.router.route_info(
            sim.topology, spec.source, spec.destination, sim.router.get_algorithm()
        )
    except ValueError as error:
        return {
            "Route": "no route",
            "Route Cost": float("inf"),
            "Hops": 0,
            "Route Latency (ms)": 0.0,
            "Route Loss (%)": 0.0,
            "Route Congestion (%)": 0.0,
            "Route Error": str(error),
        }

    return {
        "Route": " → ".join(info["route"]),
        "Route Cost": info["cost"],
        "Hops": info["hops"],
        "Route Latency (ms)": info["latency_ms"],
        "Route Loss (%)": info["packet_loss"] * 100.0,
        "Route Congestion (%)": info["congestion"] * 100.0,
    }


def configuration_summary(
    sim: NetworkSimulator,
    spec: WorkloadSpec,
    label: str,
    scheduler: str,
    algorithm: str,
) -> Dict[str, Any]:
    """Overall metrics of a finished run, straight from the simulation."""
    overall = sim.metrics.calculate(sim.time, sim.average_congestion(), len(sim.active_flows))
    queue_stats = sim.scheduler_statistics()

    row: Dict[str, Any] = {
        "Configuration": label,
        "Algorithm": ALGORITHM_LABELS.get(algorithm, algorithm),
        "Scheduler": QOS_LABELS.get(scheduler, scheduler),
        "Packets Sent": int(overall["packets_sent"]),
        "Delivered": int(overall["packets_delivered"]),
        "Dropped": int(overall["packets_dropped"]),
        "Avg Latency (ms)": overall["average_latency"] * 1000.0,
        "Jitter (ms)": overall["jitter"] * 1000.0,
        "Throughput (B/s)": overall["throughput"],
        "Packet Loss (%)": overall["packet_loss"],
        "PDR (%)": overall["packet_delivery_ratio"],
        "Sim Time (s)": sim.time,
        "Max Queue Length": int(queue_stats.get("max_queue_length", 0)),
        "Avg Queue Wait (ms)": queue_stats.get("average_waiting_time", 0.0) * 1000.0,
        "Packets Served": int(queue_stats.get("packets_served", 0)),
    }
    row.update(_route_snapshot(sim, spec))
    return row


# --------------------------------------------------------------------------
# Experiment results
# --------------------------------------------------------------------------


@dataclass
class QoSExperimentResult:
    """FIFO vs Priority Queue vs WFQ on one identical workload."""

    kind: str
    spec: WorkloadSpec
    conditions: NetworkConditions
    schedulers: Tuple[str, ...]
    summary: pd.DataFrame
    queue_statistics: pd.DataFrame
    class_metrics: Dict[str, pd.DataFrame]
    queue_class_statistics: Dict[str, pd.DataFrame]
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": self.kind,
            "workload": self.spec.describe(),
            "conditions": self.conditions.describe(),
            "schedulers": list(self.schedulers),
        }


@dataclass
class RoutingComparisonResult:
    """Dijkstra vs Bellman-Ford on one identical workload."""

    spec: WorkloadSpec
    conditions: NetworkConditions
    algorithms: Tuple[str, ...]
    weights: Dict[str, float]
    summary: pd.DataFrame
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def routes_agree(self) -> bool:
        routes = {row["Route"] for _, row in self.summary.iterrows()}
        return len(routes) == 1

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Routing algorithm comparison",
            "workload": self.spec.describe(),
            "conditions": self.conditions.describe(),
            "algorithms": [ALGORITHM_LABELS.get(a, a) for a in self.algorithms],
            "weights": dict(self.weights),
        }


@dataclass
class RoutingWeightResult:
    """How routing weight configurations change the selected route."""

    spec: WorkloadSpec
    conditions: NetworkConditions
    algorithm: str
    summary: pd.DataFrame
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def distinct_routes(self) -> List[str]:
        return sorted({row["Route"] for _, row in self.summary.iterrows()})

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Routing weight sensitivity",
            "workload": self.spec.describe(),
            "conditions": self.conditions.describe(),
            "algorithm": ALGORITHM_LABELS.get(self.algorithm, self.algorithm),
            "distinct_routes": len(self.distinct_routes),
        }


@dataclass
class CombinedExperimentResult:
    """The six routing x QoS combinations on one identical workload."""

    spec: WorkloadSpec
    conditions: NetworkConditions
    configurations: Tuple[Tuple[str, str], ...]
    summary: pd.DataFrame
    queue_statistics: pd.DataFrame
    class_metrics: Dict[str, pd.DataFrame]
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Combined routing + QoS",
            "workload": self.spec.describe(),
            "conditions": self.conditions.describe(),
            "configurations": [
                f"{ALGORITHM_LABELS[a]} + {QOS_LABELS[s]}" for a, s in self.configurations
            ],
        }


# --------------------------------------------------------------------------
# QoS experiments (Stage 4)
# --------------------------------------------------------------------------


def run_qos_experiment(
    spec: WorkloadSpec = DEFAULT_SPEC,
    conditions: NetworkConditions = COMPARISON_CONDITIONS,
    schedulers: Sequence[str] = QOS_SCHEDULERS,
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
) -> QoSExperimentResult:
    """
    Run the *same* workload through FIFO, Priority Queue and WFQ and report the
    actual simulation results for each.
    """
    return _run_qos_suite(
        kind="QoS scheduler comparison",
        spec=spec,
        conditions=conditions,
        schedulers=schedulers,
        algorithm=algorithm,
        routing_weights=routing_weights,
        wfq_weights=wfq_weights,
        priorities=priorities,
    )


def run_qos_stress_test(
    spec: Optional[WorkloadSpec] = None,
    conditions: NetworkConditions = STRESS_CONDITIONS,
    schedulers: Sequence[str] = QOS_SCHEDULERS,
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
) -> QoSExperimentResult:
    """
    Congestion stress test: every traffic class competes for a heavily
    congested path, exposing scheduler differences in waiting time, jitter,
    latency and loss.
    """
    if spec is None:
        spec = WorkloadSpec(packets_per_class=30, packet_size=1200, pps=100.0)

    return _run_qos_suite(
        kind="QoS congestion stress test",
        spec=spec,
        conditions=conditions,
        schedulers=schedulers,
        algorithm=algorithm,
        routing_weights=routing_weights,
        wfq_weights=wfq_weights,
        priorities=priorities,
    )


def _run_qos_suite(
    kind: str,
    spec: WorkloadSpec,
    conditions: NetworkConditions,
    schedulers: Sequence[str],
    algorithm: str,
    routing_weights: Optional[Dict[str, float]],
    wfq_weights: Optional[Dict[str, float]],
    priorities: Optional[Dict[str, float]],
) -> QoSExperimentResult:
    rows: List[Dict[str, Any]] = []
    queue_rows: List[Dict[str, Any]] = []
    class_frames: Dict[str, pd.DataFrame] = {}
    queue_class_frames: Dict[str, pd.DataFrame] = {}
    runs: Dict[str, Dict[str, Any]] = {}
    canonical: List[str] = []

    for scheduler in schedulers:
        key = normalize_scheduler_name(scheduler)
        canonical.append(key)
        label = QOS_LABELS.get(key, key)

        sim = build_simulator(
            spec,
            scheduler=key,
            algorithm=algorithm,
            conditions=conditions,
            routing_weights=routing_weights,
            wfq_weights=wfq_weights,
            priorities=priorities,
        )
        run_workload(sim, spec)

        rows.append(
            configuration_summary(sim, spec, label, key, normalize_algorithm(algorithm))
        )
        queue_rows.append(queue_statistics_frame(sim, label))
        class_frames[label] = class_metrics_frame(sim, spec)
        queue_class_frames[label] = queue_class_frame(sim)
        runs[label] = sim

    return QoSExperimentResult(
        kind=kind,
        spec=spec,
        conditions=conditions,
        schedulers=tuple(canonical),
        summary=pd.DataFrame(rows),
        queue_statistics=pd.DataFrame(queue_rows),
        class_metrics=class_frames,
        queue_class_statistics=queue_class_frames,
        runs=runs,
    )


# --------------------------------------------------------------------------
# Routing experiments (Stage 5)
# --------------------------------------------------------------------------


def run_routing_comparison(
    spec: WorkloadSpec = DEFAULT_WEIGHT_SPEC,
    conditions: NetworkConditions = WEIGHT_SCENARIO_CONDITIONS,
    algorithms: Sequence[str] = ROUTING_ALGORITHMS,
    routing_weights: Optional[Dict[str, float]] = None,
) -> RoutingComparisonResult:
    """
    Run the same workload with Dijkstra and Bellman-Ford and compare the
    selected route, route cost, hop count and the resulting performance.
    """
    weights = dict(DEFAULT_ROUTING_WEIGHTS)
    if routing_weights:
        weights.update(routing_weights)

    rows: List[Dict[str, Any]] = []
    runs: Dict[str, Dict[str, Any]] = {}
    canonical: List[str] = []

    for algorithm in algorithms:
        key = normalize_algorithm(algorithm)
        canonical.append(key)
        label = ALGORITHM_LABELS.get(key, key)

        sim = build_simulator(
            spec,
            scheduler="fifo",
            algorithm=key,
            conditions=conditions,
            routing_weights=weights,
        )
        run_workload(sim, spec)

        row = configuration_summary(sim, spec, label, "fifo", key)
        stats = sim.router.solve(
            sim.topology, spec.source, spec.destination, key
        )[2]
        row["Algorithm Iterations"] = int(stats.get("iterations", 0))
        row["Algorithm Relaxations"] = int(stats.get("relaxations", 0))
        row["Algorithm Nodes Visited"] = int(stats.get("nodes_visited", 0))
        rows.append(row)
        runs[label] = sim

    return RoutingComparisonResult(
        spec=spec,
        conditions=conditions,
        algorithms=tuple(canonical),
        weights=weights,
        summary=pd.DataFrame(rows),
        runs=runs,
    )


def run_routing_weight_experiment(
    spec: WorkloadSpec = DEFAULT_WEIGHT_SPEC,
    conditions: NetworkConditions = WEIGHT_SCENARIO_CONDITIONS,
    algorithm: str = "dijkstra",
    presets: Optional[Dict[str, Dict[str, float]]] = None,
) -> RoutingWeightResult:
    """
    Evaluate several routing weight configurations on identical network
    conditions and show that the weights actually change route selection.
    """
    configs = presets or ROUTING_WEIGHT_PRESETS
    key = normalize_algorithm(algorithm)

    rows: List[Dict[str, Any]] = []
    runs: Dict[str, Dict[str, Any]] = {}

    for name, weights in configs.items():
        sim = build_simulator(
            spec,
            scheduler="fifo",
            algorithm=key,
            conditions=conditions,
            routing_weights=weights,
        )
        run_workload(sim, spec)

        row = configuration_summary(sim, spec, name, "fifo", key)
        row["Weight Config"] = name
        row["Weights"] = ", ".join(
            f"{w}={v:g}" for w, v in weights.items()
        )
        rows.append(row)
        runs[name] = sim

    return RoutingWeightResult(
        spec=spec,
        conditions=conditions,
        algorithm=key,
        summary=pd.DataFrame(rows),
        runs=runs,
    )


# --------------------------------------------------------------------------
# Combined experiment (Stage 4 + Stage 5)
# --------------------------------------------------------------------------


def run_combined_experiment(
    spec: WorkloadSpec = DEFAULT_SPEC,
    conditions: NetworkConditions = COMPARISON_CONDITIONS,
    configurations: Sequence[Tuple[str, str]] = COMBINED_CONFIGURATIONS,
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
) -> CombinedExperimentResult:
    """
    Run all six routing x QoS combinations (Dijkstra/Bellman-Ford crossed with
    FIFO/Priority/WFQ) on one identical workload and network conditions.
    """
    rows: List[Dict[str, Any]] = []
    queue_rows: List[Dict[str, Any]] = []
    class_frames: Dict[str, pd.DataFrame] = {}
    runs: Dict[str, Dict[str, Any]] = {}
    canonical: List[Tuple[str, str]] = []

    for algorithm, scheduler in configurations:
        algorithm_key = normalize_algorithm(algorithm)
        scheduler_key = normalize_scheduler_name(scheduler)
        canonical.append((algorithm_key, scheduler_key))

        label = f"{ALGORITHM_LABELS[algorithm_key]} + {QOS_LABELS[scheduler_key]}"

        sim = build_simulator(
            spec,
            scheduler=scheduler_key,
            algorithm=algorithm_key,
            conditions=conditions,
            routing_weights=routing_weights,
            wfq_weights=wfq_weights,
            priorities=priorities,
        )
        run_workload(sim, spec)

        rows.append(
            configuration_summary(sim, spec, label, scheduler_key, algorithm_key)
        )
        queue_rows.append(queue_statistics_frame(sim, label))
        class_frames[label] = class_metrics_frame(sim, spec)
        runs[label] = sim

    return CombinedExperimentResult(
        spec=spec,
        conditions=conditions,
        configurations=tuple(canonical),
        summary=pd.DataFrame(rows),
        queue_statistics=pd.DataFrame(queue_rows),
        class_metrics=class_frames,
        runs=runs,
    )
