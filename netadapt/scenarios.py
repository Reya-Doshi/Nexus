"""
Stage 6 -- advanced network scenarios and resilience evaluation.

This module adds a reusable *scenario framework* on top of the Stage 4/5
experiment engine. A scenario is a reproducible description of a network
situation:

* the link conditions (congestion / packet loss / bandwidth degradation),
* the components that fail and later recover,
* the traffic workload, and
* the random seed.

Running a scenario drives the real :class:`simulator.NetworkSimulator` through
three phases -- *before* the failure, *during* the failure and *after recovery* --
and reports only values the engine actually produced: packet records, queue
counters, recovery records and the structured event log. Nothing in this module
hardcodes a result.

The same framework also powers:

* **multi-run experiments** -- the same scenario executed with several seeds,
  aggregated into mean / standard deviation / min / max,
* **resilience evaluation** -- failure, detection, recalculation and recovery
  timing plus the packets affected,
* **route stability tracking** -- the real route history and route timeline,
* **sensitivity experiments** -- one network parameter varied while everything
  else is held identical,
* **scenario comparison** -- several scenarios measured on the same workload,
* and **CSV / DataFrame export** of every result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from statistics import stdev
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from events import EventType
from experiments import (
    CORE_LINKS,
    LinkConditionGroup,
    NetworkConditions,
    WorkloadSpec,
    apply_conditions,
    build_simulator,
    class_metrics_frame,
    uniform_conditions,
)
from metrics import Metrics
from qos import SCHEDULER_LABELS
from routing import ALGORITHM_LABELS
from simulator import NetworkSimulator

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

DEFAULT_SCENARIO_SEED = 42
DEFAULT_FAILURE_DETECTION_TIMEOUT = 1.0

#: Fraction of the workload processed before the failure is injected ...
DEFAULT_BEFORE_FRACTION = 0.35
#: ... and the fraction processed while the network is degraded.
DEFAULT_DURING_FRACTION = 0.30

#: Workload used by every scenario unless it overrides it.
DEFAULT_SCENARIO_SPEC = WorkloadSpec(
    packets_per_class=10, packet_size=1200, pps=50.0, seed=DEFAULT_SCENARIO_SEED
)

#: Bulk-transfer workload: bigger packets make reduced bandwidth observable.
BULK_SCENARIO_SPEC = WorkloadSpec(
    packets_per_class=8, packet_size=8000, pps=40.0, seed=DEFAULT_SCENARIO_SEED
)

#: Links varied by the sensitivity experiments: every link the workload uses.
SENSITIVITY_LINKS: Tuple[Tuple[str, str], ...] = CORE_LINKS

#: Parameter sweeps used by the sensitivity experiment.
SENSITIVITY_PARAMETERS: Dict[str, Tuple[float, ...]] = {
    "congestion": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    "packet_loss": (0.0, 0.05, 0.10, 0.20),
    "bandwidth": (100.0, 50.0, 25.0, 10.0),
}

SENSITIVITY_LABELS: Dict[str, str] = {
    "congestion": "Congestion level",
    "packet_loss": "Packet loss probability",
    "bandwidth": "Link bandwidth (Mbps)",
}


# --------------------------------------------------------------------------
# Scenario definition
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureSpec:
    """Links and nodes taken down during the failure phase of a scenario."""

    links: Tuple[Tuple[str, str], ...] = ()
    nodes: Tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.links and not self.nodes

    def components(self) -> List[str]:
        return [f"{u}-{v}" for u, v in self.links] + list(self.nodes)

    def label(self) -> str:
        parts = [f"{u}-{v}" for u, v in self.links] + [
            f"node {node}" for node in self.nodes
        ]
        return ", ".join(parts) if parts else "none"


@dataclass(frozen=True)
class ScenarioPreset:
    """
    A named, reproducible network scenario.

    The preset carries the whole recipe -- conditions, failures, workload and
    seed -- so the same preset always produces the same simulation result.
    """

    key: str
    name: str
    description: str
    conditions: NetworkConditions
    workload: WorkloadSpec = DEFAULT_SCENARIO_SPEC
    failures: FailureSpec = FailureSpec()
    seed: int = DEFAULT_SCENARIO_SEED
    failure_detection_timeout: float = DEFAULT_FAILURE_DETECTION_TIMEOUT
    before_fraction: float = DEFAULT_BEFORE_FRACTION
    during_fraction: float = DEFAULT_DURING_FRACTION

    @property
    def has_failures(self) -> bool:
        return not self.failures.empty

    def describe(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "conditions": self.conditions.describe(),
            "failures": self.failures.components(),
            "workload": self.workload.describe(),
            "seed": self.seed,
            "failure_detection_timeout": self.failure_detection_timeout,
        }


def _uniform(
    label: str,
    congestion: float = 0.0,
    packet_loss: float = 0.0,
    bandwidth: Optional[float] = None,
    links: Sequence[Tuple[str, str]] = CORE_LINKS,
) -> NetworkConditions:
    """One condition group applied uniformly to the given links."""
    return uniform_conditions(
        links=links,
        congestion=congestion,
        packet_loss=packet_loss,
        bandwidth=bandwidth,
        label=label,
    )


def _default_conditions(label: str) -> NetworkConditions:
    """No degradation at all -- the topology defaults."""
    return NetworkConditions(label=label, groups=())


# --------------------------------------------------------------------------
# Scenario presets
# --------------------------------------------------------------------------

SCENARIO_PRESETS: Tuple[ScenarioPreset, ...] = (
    ScenarioPreset(
        key="normal",
        name="Normal network",
        description="No degradation and no failures - the reference scenario.",
        conditions=_default_conditions("Normal network (topology defaults)"),
    ),
    ScenarioPreset(
        key="high_congestion",
        name="High congestion",
        description="75% congestion on every link carrying the workload.",
        conditions=_uniform("High congestion (75% on all core links)", congestion=0.75),
    ),
    ScenarioPreset(
        key="high_packet_loss",
        name="High packet loss",
        description="10% packet loss on every link carrying the workload.",
        conditions=_uniform("High packet loss (10% on all core links)", packet_loss=0.10),
    ),
    ScenarioPreset(
        key="reduced_bandwidth",
        name="Reduced bandwidth",
        description="Every core link throttled to 10 Mbps with bulk-sized packets.",
        conditions=_uniform(
            "Reduced bandwidth (10 Mbps on all core links)", bandwidth=10.0
        ),
        workload=BULK_SCENARIO_SPEC,
    ),
    ScenarioPreset(
        key="link_failure",
        name="Link failure",
        description="R3-R5 (a link on the default route) fails mid-run, then recovers.",
        conditions=_default_conditions("Link failure scenario (topology defaults)"),
        failures=FailureSpec(links=(("R3", "R5"),)),
    ),
    ScenarioPreset(
        key="router_failure",
        name="Router (node) failure",
        description="Router R3 fails mid-run, then recovers.",
        conditions=_default_conditions("Node failure scenario (topology defaults)"),
        failures=FailureSpec(nodes=("R3",)),
    ),
    ScenarioPreset(
        key="multiple_link_failures",
        name="Multiple link failures",
        description="Both R3-R5 and R4-R5 fail mid-run, isolating the shortest path.",
        conditions=_default_conditions("Multiple link failures (topology defaults)"),
        failures=FailureSpec(links=(("R3", "R5"), ("R4", "R5"))),
    ),
    ScenarioPreset(
        key="congestion_and_failure",
        name="Congestion + link failure",
        description="75% congestion everywhere, and R3-R5 fails mid-run.",
        conditions=_uniform("Congestion + failure (75% congestion)", congestion=0.75),
        failures=FailureSpec(links=(("R3", "R5"),)),
    ),
    ScenarioPreset(
        key="loss_and_congestion",
        name="Packet loss + congestion",
        description="60% congestion and 12% packet loss on every core link.",
        conditions=_uniform(
            "Packet loss + congestion (60% congestion, 12% loss)",
            congestion=0.60,
            packet_loss=0.12,
        ),
    ),
    ScenarioPreset(
        key="bandwidth_degradation_and_congestion",
        name="Bandwidth degradation + congestion",
        description="Every core link limited to 15 Mbps while 60% congested.",
        conditions=_uniform(
            "Bandwidth degradation + congestion (15 Mbps, 60% congestion)",
            congestion=0.60,
            bandwidth=15.0,
        ),
    ),
    ScenarioPreset(
        key="combined_degraded_network",
        name="Combined degraded network",
        description=(
            "Congestion, packet loss and reduced bandwidth on the core, plus two "
            "link failures during the run (R3-R5 at 40% loss, R4-R6 throttled)."
        ),
        conditions=NetworkConditions(
            label="Combined degraded network (congestion + loss + throttling)",
            groups=(
                LinkConditionGroup(
                    links=(
                        ("H1", "R1"),
                        ("R1", "R2"),
                        ("R2", "R4"),
                        ("R4", "R5"),
                        ("R5", "R6"),
                        ("H3", "R5"),
                    ),
                    congestion=0.50,
                    packet_loss=0.08,
                    bandwidth=20.0,
                ),
                LinkConditionGroup(
                    links=(("R1", "R3"), ("R3", "R5")),
                    congestion=0.50,
                    packet_loss=0.40,
                    bandwidth=20.0,
                ),
            ),
        ),
        failures=FailureSpec(links=(("R3", "R5"), ("R4", "R5"))),
    ),
)

SCENARIO_BY_KEY: Dict[str, ScenarioPreset] = {
    preset.key: preset for preset in SCENARIO_PRESETS
}


def get_scenario(key: str) -> ScenarioPreset:
    """Look up a scenario preset by key."""
    try:
        return SCENARIO_BY_KEY[str(key)]
    except KeyError:
        raise ValueError(
            f"Unknown scenario: {key}. Available: {sorted(SCENARIO_BY_KEY)}"
        ) from None


def scenario_keys() -> List[str]:
    return [preset.key for preset in SCENARIO_PRESETS]


def scenario_labels() -> Dict[str, str]:
    """``{key: human readable name}`` for selectors in the dashboard."""
    return {preset.key: preset.name for preset in SCENARIO_PRESETS}


# --------------------------------------------------------------------------
# Applying / resetting scenarios on a live simulator
# --------------------------------------------------------------------------


def clear_failures(sim: NetworkSimulator) -> NetworkSimulator:
    """Bring every failed component back up without emitting recovery events."""
    for u, v in list(sim._failed_links.keys()):
        sim.topology.recover_link(u, v)
    sim._failed_links.clear()
    sim._detected_links.clear()

    for node in list(sim._failed_nodes.keys()):
        sim.topology.recover_node(node)
    sim._failed_nodes.clear()
    sim._detected_nodes.clear()

    return sim


def reset_scenario(sim: NetworkSimulator) -> NetworkSimulator:
    """Remove all scenario degradation and failures from a simulator."""
    clear_failures(sim)
    sim.topology.reset()
    return sim


def apply_scenario(
    sim: NetworkSimulator, scenario: ScenarioPreset | str
) -> NetworkSimulator:
    """
    Apply a scenario's link conditions to an existing simulator.

    The topology is first returned to its defaults, so applying a scenario never
    inherits degradation from a previous one.
    """
    preset = scenario if isinstance(scenario, ScenarioPreset) else get_scenario(scenario)
    reset_scenario(sim)
    apply_conditions(sim, preset.conditions)
    return sim


# --------------------------------------------------------------------------
# Phased scenario execution
# --------------------------------------------------------------------------


def _records_metrics(records: Sequence[Any], duration: float) -> Dict[str, float]:
    """
    Reuse the Stage 1/4 metrics implementation on a slice of packet records.

    ``duration`` is the wall-clock span of the phase, so throughput stays a real
    measurement for the phase instead of the whole run.
    """
    holder = Metrics()
    holder.records.extend(records)
    return holder.calculate(max(duration, 0.001), 0.0)


def _std(values: Sequence[float]) -> float:
    """Sample standard deviation, ``0.0`` when fewer than two samples exist."""
    if len(values) < 2:
        return 0.0
    return stdev(values)


def _latency_std(records: Sequence[Any]) -> float:
    latencies = [
        record.latency
        for record in records
        if record.status == "DELIVERED" and record.latency is not None
    ]
    return _std(latencies)


def _process(sim: NetworkSimulator, count: int) -> int:
    """Process at most ``count`` packets from the scheduler."""
    processed = 0
    for _ in range(max(0, count)):
        if sim.scheduler.empty():
            break
        sim.process_next_packet()
        processed += 1
    return processed


def _create_workload(sim: NetworkSimulator, spec: WorkloadSpec) -> Dict[str, Any]:
    """Emit the workload as interleaved, arrival-ordered packets (one flow/class)."""
    return sim.generate_interleaved_traffic(
        spec.source,
        spec.destination,
        classes=list(spec.classes),
        packets_per_class=spec.packets_per_class,
        packet_size=spec.packet_size,
        pps=spec.pps,
    )


def workload_route_timeline(
    sim: NetworkSimulator, flow_ids: Sequence[str]
) -> List[Tuple[float, str]]:
    """
    The route actually used by the workload over time.

    Built from the structured event log (``TRAFFIC_STARTED`` +
    ``ROUTE_RECALCULATED``) for the given flows, with repeated routes collapsed.
    """
    wanted = set(flow_ids)
    entries: List[Tuple[float, str]] = []

    for event in sim.event_logger.get_events():
        if event.flow_id not in wanted:
            continue
        if event.event_type == EventType.TRAFFIC_STARTED:
            route = event.details.get("route")
        elif event.event_type == EventType.ROUTE_RECALCULATED:
            route = event.details.get("new_route")
        else:
            continue

        if route:
            entries.append((float(event.timestamp), " → ".join(route)))

    entries.sort(key=lambda entry: entry[0])

    timeline: List[Tuple[float, str]] = []
    for timestamp, route in entries:
        if timeline and timeline[-1][1] == route:
            continue
        timeline.append((timestamp, route))

    return timeline


def route_history_frame(sim: NetworkSimulator) -> pd.DataFrame:
    """Every real route decision recorded in the event log."""
    rows: List[Dict[str, Any]] = []

    for event in sim.event_logger.get_events():
        if event.event_type == EventType.TRAFFIC_STARTED:
            route = event.details.get("route") or []
            rows.append(
                {
                    "Time (s)": event.timestamp,
                    "Flow": event.flow_id,
                    "Change": "INITIAL",
                    "Previous Route": "",
                    "Route": " → ".join(route),
                    "Route Cost": event.details.get("route_cost", 0.0),
                    "Component": event.component or "",
                }
            )
        elif event.event_type == EventType.ROUTE_RECALCULATED:
            new_route = event.details.get("new_route") or []
            old_route = event.details.get("old_route") or []
            rows.append(
                {
                    "Time (s)": event.timestamp,
                    "Flow": event.flow_id,
                    "Change": "RECALCULATED",
                    "Previous Route": " → ".join(old_route),
                    "Route": " → ".join(new_route),
                    "Route Cost": event.details.get("route_cost", 0.0),
                    "Component": event.component or "",
                }
            )

    return pd.DataFrame(
        rows,
        columns=[
            "Time (s)",
            "Flow",
            "Change",
            "Previous Route",
            "Route",
            "Route Cost",
            "Component",
        ],
    )


def flow_route_history(sim: NetworkSimulator) -> Dict[str, List[str]]:
    """Route history per flow, in the order the routes were selected."""
    history: Dict[str, List[str]] = {}

    for event in sim.event_logger.get_events():
        if event.flow_id is None:
            continue
        if event.event_type == EventType.TRAFFIC_STARTED:
            route = event.details.get("route")
        elif event.event_type == EventType.ROUTE_RECALCULATED:
            route = event.details.get("new_route")
        else:
            continue

        if not route:
            continue

        rendered = " → ".join(route)
        routes = history.setdefault(event.flow_id, [])
        if not routes or routes[-1] != rendered:
            routes.append(rendered)

    return history


@dataclass
class ScenarioRunResult:
    """One execution of one scenario through the real simulation engine."""

    scenario: ScenarioPreset
    seed: int
    workload: WorkloadSpec
    phases: Dict[str, Dict[str, float]]
    phase_durations: Dict[str, float]
    overall: Dict[str, float]
    queue_statistics: Dict[str, float]
    resilience: Dict[str, Any]
    class_metrics: pd.DataFrame
    route_history: pd.DataFrame
    route_timeline: pd.DataFrame
    flow_routes: Dict[str, List[str]]
    latency_std: float
    simulator: Optional[NetworkSimulator] = field(
        default=None, repr=False, compare=False
    )

    # -- presentation ------------------------------------------------------
    def summary_row(self) -> Dict[str, Any]:
        overall = self.overall
        queue_stats = self.queue_statistics
        resilience = self.resilience

        return {
            "Scenario": self.scenario.name,
            "Scenario Key": self.scenario.key,
            "Seed": self.seed,
            "Conditions": self.scenario.conditions.label,
            "Failures": self.scenario.failures.label(),
            "Packets Sent": int(overall["packets_sent"]),
            "Delivered": int(overall["packets_delivered"]),
            "Dropped": int(overall["packets_dropped"]),
            "Avg Latency (ms)": overall["average_latency"] * 1000.0,
            "Latency Std (ms)": self.latency_std * 1000.0,
            "Throughput (B/s)": overall["throughput"],
            "Packet Loss (%)": overall["packet_loss"],
            "PDR (%)": overall["packet_delivery_ratio"],
            "Jitter (ms)": overall["jitter"] * 1000.0,
            "Avg Queue Wait (ms)": queue_stats.get("average_waiting_time", 0.0) * 1000.0,
            "Max Queue Length": int(queue_stats.get("max_queue_length", 0)),
            "Failure Detection Time (s)": resilience["Detection Delay (s)"],
            "Route Recalculation Time (s)": resilience["Recalculation Delay (s)"],
            "Recovery Time (s)": resilience["Outage Time (s)"],
            "Route Changes": resilience["Route Changes"],
            "Successful Flows": resilience["Successful Flows"],
            "Failed Flows": resilience["Failed Flows"],
            "Packets Affected": resilience["Packets Affected"],
            "Final Route": resilience["Final Route"],
        }

    def summary_frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.summary_row()])

    def phase_frame(self) -> pd.DataFrame:
        rows = []
        for phase, label in (
            ("before", "Before failure"),
            ("during", "During failure"),
            ("after", "After recovery"),
        ):
            values = self.phases[phase]
            rows.append(
                {
                    "Phase": label,
                    "Duration (s)": self.phase_durations[phase],
                    "Packets": int(values["packets_sent"]),
                    "Delivered": int(values["packets_delivered"]),
                    "Dropped": int(values["packets_dropped"]),
                    "Avg Latency (ms)": values["average_latency"] * 1000.0,
                    "Jitter (ms)": values["jitter"] * 1000.0,
                    "Throughput (B/s)": values["throughput"],
                    "Packet Loss (%)": values["packet_loss"],
                    "PDR (%)": values["packet_delivery_ratio"],
                }
            )
        return pd.DataFrame(rows)

    def resilience_frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.resilience])

    def frames(self) -> Dict[str, pd.DataFrame]:
        """Every result table of this run, ready for display or CSV export."""
        return {
            "scenario summary": self.summary_frame(),
            "before during after": self.phase_frame(),
            "resilience": self.resilience_frame(),
            "route history": self.route_history,
            "route timeline": self.route_timeline,
            "per class metrics": self.class_metrics,
            "queue statistics": pd.DataFrame([self.queue_statistics]),
        }


def run_scenario(
    scenario: ScenarioPreset | str,
    *,
    seed: Optional[int] = None,
    spec: Optional[WorkloadSpec] = None,
    scheduler: str = "fifo",
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
    failure_detection_timeout: Optional[float] = None,
) -> ScenarioRunResult:
    """
    Execute one scenario end to end on a freshly built simulator.

    Timeline:

    1. *before*   -- the workload runs on the scenario's conditions,
    2. *during*   -- the scenario's components fail, the clock advances past the
       detection timeout so the engine detects, recalculates and reroutes, and
       more of the workload is processed,
    3. *after*    -- the failed components recover and the rest of the workload
       is drained.

    Every reported value comes from packet records, queue counters, recovery
    records or the event log of that simulator.
    """
    preset = scenario if isinstance(scenario, ScenarioPreset) else get_scenario(scenario)
    run_seed = preset.seed if seed is None else int(seed)
    workload = replace(spec or preset.workload, seed=run_seed)
    timeout = (
        preset.failure_detection_timeout
        if failure_detection_timeout is None
        else float(failure_detection_timeout)
    )

    sim = build_simulator(
        workload,
        scheduler=scheduler,
        algorithm=algorithm,
        conditions=preset.conditions,
        routing_weights=routing_weights,
        wfq_weights=wfq_weights,
        priorities=priorities,
        failure_detection_timeout=timeout,
    )

    flows = _create_workload(sim, workload)
    flow_ids = [flow.flow_id for flow in flows.values()]

    total = workload.total_packets
    before_packets = min(max(1, int(total * preset.before_fraction)), total)
    during_packets = min(
        max(1, int(total * preset.during_fraction)), max(0, total - before_packets)
    )

    # -- phase 1: before the failure --------------------------------------
    phase_start = sim.time
    _process(sim, before_packets)
    before_records = list(sim.metrics.records)
    before_duration = max(sim.time - phase_start, 0.001)

    # -- phase 2: failure injected ----------------------------------------
    failure_time = sim.time
    for u, v in preset.failures.links:
        sim.fail_link(u, v)
    for node in preset.failures.nodes:
        sim.fail_node(node)

    during_start = sim.time
    if preset.has_failures:
        # Advance the simulation clock past the detection timeout so the
        # heartbeat engine really detects, recalculates and reroutes.
        sim.tick(timeout + 0.5)

    _process(sim, during_packets)
    during_records = list(sim.metrics.records[len(before_records) :])
    during_duration = max(sim.time - during_start, 0.001)

    # -- phase 3: recovery -------------------------------------------------
    for u, v in preset.failures.links:
        if sim.is_link_failed(u, v):
            sim.recover_link(u, v)
    for node in preset.failures.nodes:
        if sim.is_node_failed(node):
            sim.recover_node(node)
    recovery_time = sim.time

    after_start = sim.time
    if not sim.scheduler.empty():
        sim.run_until_empty()
    after_records = list(
        sim.metrics.records[len(before_records) + len(during_records) :]
    )
    after_duration = max(sim.time - after_start, 0.001)

    # -- results -----------------------------------------------------------
    overall = sim.metrics.calculate(
        sim.time, sim.average_congestion(), len(sim.active_flows)
    )
    queue_statistics = sim.scheduler_statistics()
    timeline = workload_route_timeline(sim, flow_ids)

    initial_route = timeline[0][1] if timeline else ""
    final_routes = sorted(
        {
            " → ".join(flow.current_route)
            for flow in sim.active_flows.values()
            if flow.current_route
        }
    )
    final_route = " / ".join(final_routes)

    detection_delays = [
        record.detection_time - record.failure_time
        for record in sim.metrics.recovery_records
        if record.detection_time is not None
    ]
    recalculation_delays = [
        record.recalculation_time - record.detection_time
        for record in sim.metrics.recovery_records
        if record.recalculation_time is not None and record.detection_time is not None
    ]
    recalculation_times = [
        record.recalculation_time
        for record in sim.metrics.recovery_records
        if record.recalculation_time is not None
    ]
    detection_times = [
        record.detection_time
        for record in sim.metrics.recovery_records
        if record.detection_time is not None
    ]

    resilience: Dict[str, Any] = {
        "Failure Time (s)": failure_time if preset.has_failures else 0.0,
        "Detection Time (s)": detection_times[0] if detection_times else 0.0,
        "Detection Delay (s)": (
            sum(detection_delays) / len(detection_delays) if detection_delays else 0.0
        ),
        "Recalculation Time (s)": (
            recalculation_times[0] if recalculation_times else 0.0
        ),
        "Recalculation Delay (s)": (
            sum(recalculation_delays) / len(recalculation_delays)
            if recalculation_delays
            else 0.0
        ),
        "Recovery Time (s)": recovery_time if preset.has_failures else 0.0,
        "Outage Time (s)": overall["recovery_time"],
        "Packets Affected": len(during_records),
        "Packets Delivered During Failure": sum(
            record.status == "DELIVERED" for record in during_records
        ),
        "Packets Dropped During Failure": sum(
            record.status == "DROPPED" for record in during_records
        ),
        "Packets Delivered After Recovery": sum(
            record.status == "DELIVERED" for record in after_records
        ),
        "Route Changes": max(0, len(timeline) - 1),
        "Successful Flows": sum(
            flow.status == "COMPLETED" for flow in sim.active_flows.values()
        ),
        "Failed Flows": sum(
            flow.status == "FAILED" for flow in sim.active_flows.values()
        ),
        "Failed Components": preset.failures.label(),
        "Initial Route": initial_route,
        "Final Route": final_route,
    }

    route_timeline = pd.DataFrame(timeline, columns=["Time (s)", "Route"])

    return ScenarioRunResult(
        scenario=preset,
        seed=run_seed,
        workload=workload,
        phases={
            "before": _records_metrics(before_records, before_duration),
            "during": _records_metrics(during_records, during_duration),
            "after": _records_metrics(after_records, after_duration),
        },
        phase_durations={
            "before": before_duration,
            "during": during_duration,
            "after": after_duration,
        },
        overall=overall,
        queue_statistics=queue_statistics,
        resilience=resilience,
        class_metrics=class_metrics_frame(sim, workload),
        route_history=route_history_frame(sim),
        route_timeline=route_timeline,
        flow_routes=flow_route_history(sim),
        latency_std=_latency_std(sim.metrics.records),
        simulator=sim,
    )


# --------------------------------------------------------------------------
# Multi-run experiments
# --------------------------------------------------------------------------

#: Columns of a run summary that are not numeric metrics.
NON_METRIC_COLUMNS: Tuple[str, ...] = (
    "Scenario",
    "Scenario Key",
    "Seed",
    "Conditions",
    "Failures",
    "Final Route",
)


@dataclass
class MultiRunResult:
    """The same scenario executed several times with configurable seeds."""

    scenario: ScenarioPreset
    seeds: Tuple[int, ...]
    scheduler: str
    algorithm: str
    per_run: pd.DataFrame
    aggregate: pd.DataFrame
    runs: List[ScenarioRunResult] = field(default_factory=list, repr=False)

    @property
    def metric_names(self) -> List[str]:
        return list(self.aggregate["Metric"])

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Multi-run scenario experiment",
            "scenario": self.scenario.key,
            "scenario_name": self.scenario.name,
            "runs": len(self.seeds),
            "seeds": list(self.seeds),
            "scheduler": SCHEDULER_LABELS.get(self.scheduler, self.scheduler),
            "algorithm": ALGORITHM_LABELS.get(self.algorithm, self.algorithm),
            "conditions": self.scenario.conditions.label,
        }

    def frames(self) -> Dict[str, pd.DataFrame]:
        return {
            "multi run per run": self.per_run,
            "multi run aggregate": self.aggregate,
        }


def aggregate_runs(per_run: pd.DataFrame, seeds: Tuple[int, ...]) -> pd.DataFrame:
    """Mean / standard deviation / min / max of every numeric metric."""
    rows: List[Dict[str, Any]] = []

    for column in per_run.columns:
        if column in NON_METRIC_COLUMNS:
            continue
        if not pd.api.types.is_numeric_dtype(per_run[column]):
            continue

        values = [float(value) for value in per_run[column]]
        rows.append(
            {
                "Metric": column,
                "Mean": sum(values) / len(values),
                "Std Dev": _std(values),
                "Min": min(values),
                "Max": max(values),
                "Runs": len(seeds),
            }
        )

    return pd.DataFrame(rows)


def run_multi_run_experiment(
    scenario: ScenarioPreset | str,
    seeds: Sequence[int] = (1, 2, 3, 4, 5),
    *,
    spec: Optional[WorkloadSpec] = None,
    scheduler: str = "fifo",
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
    wfq_weights: Optional[Dict[str, float]] = None,
    priorities: Optional[Dict[str, float]] = None,
) -> MultiRunResult:
    """Run one scenario repeatedly with different seeds and aggregate the results."""
    preset = scenario if isinstance(scenario, ScenarioPreset) else get_scenario(scenario)
    seed_tuple = tuple(int(value) for value in seeds)
    if not seed_tuple:
        raise ValueError("At least one seed is required.")

    runs: List[ScenarioRunResult] = []
    for seed in seed_tuple:
        runs.append(
            run_scenario(
                preset,
                seed=seed,
                spec=spec,
                scheduler=scheduler,
                algorithm=algorithm,
                routing_weights=routing_weights,
                wfq_weights=wfq_weights,
                priorities=priorities,
            )
        )

    per_run = pd.DataFrame([run.summary_row() for run in runs])
    return MultiRunResult(
        scenario=preset,
        seeds=seed_tuple,
        scheduler=runs[0].simulator.scheduler_name if runs[0].simulator else scheduler,
        algorithm=(
            runs[0].simulator.get_router_algorithm() if runs[0].simulator else algorithm
        ),
        per_run=per_run,
        aggregate=aggregate_runs(per_run, seed_tuple),
        runs=runs,
    )


# --------------------------------------------------------------------------
# Sensitivity experiment
# --------------------------------------------------------------------------


def sensitivity_scenario(
    parameter: str,
    value: float,
    spec: Optional[WorkloadSpec] = None,
) -> ScenarioPreset:
    """
    Build a scenario that varies exactly one network parameter.

    Every other condition keeps its neutral value, so the only difference
    between two sensitivity points is the parameter under test.
    """
    if parameter not in SENSITIVITY_PARAMETERS:
        raise ValueError(
            f"Unknown sensitivity parameter: {parameter}. "
            f"Available: {sorted(SENSITIVITY_PARAMETERS)}"
        )

    congestion = 0.0
    packet_loss = 0.0
    bandwidth: Optional[float] = None

    if parameter == "congestion":
        congestion = float(value)
    elif parameter == "packet_loss":
        packet_loss = float(value)
    else:
        bandwidth = float(value)

    label = f"{SENSITIVITY_LABELS[parameter]} = {value:g}"
    return ScenarioPreset(
        key=f"{parameter}-{value:g}",
        name=label,
        description=f"Sensitivity point varying {SENSITIVITY_LABELS[parameter]}.",
        conditions=_uniform(
            label,
            congestion=congestion,
            packet_loss=packet_loss,
            bandwidth=bandwidth,
        ),
        workload=spec or DEFAULT_SCENARIO_SPEC,
    )


@dataclass
class SensitivityResult:
    """How network performance changes as one parameter is swept."""

    parameter: str
    values: Tuple[float, ...]
    seeds: Tuple[int, ...]
    workload: WorkloadSpec
    scheduler: str
    algorithm: str
    summary: pd.DataFrame
    per_run: pd.DataFrame
    runs: List[ScenarioRunResult] = field(default_factory=list, repr=False)

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Sensitivity experiment",
            "parameter": self.parameter,
            "parameter_label": SENSITIVITY_LABELS.get(self.parameter, self.parameter),
            "values": list(self.values),
            "seeds": list(self.seeds),
            "workload": self.workload.describe(),
            "scheduler": SCHEDULER_LABELS.get(self.scheduler, self.scheduler),
            "algorithm": ALGORITHM_LABELS.get(self.algorithm, self.algorithm),
        }

    def frames(self) -> Dict[str, pd.DataFrame]:
        return {
            f"sensitivity {self.parameter} summary": self.summary,
            f"sensitivity {self.parameter} per run": self.per_run,
        }


def run_sensitivity_experiment(
    parameter: str = "congestion",
    values: Optional[Sequence[float]] = None,
    seeds: Sequence[int] = (DEFAULT_SCENARIO_SEED,),
    *,
    spec: Optional[WorkloadSpec] = None,
    scheduler: str = "fifo",
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
) -> SensitivityResult:
    """
    Sweep one network parameter while keeping every other condition identical.

    Each point is a full scenario run through the real simulator; the reported
    numbers are measured, never assumed.
    """
    if parameter not in SENSITIVITY_PARAMETERS:
        raise ValueError(
            f"Unknown sensitivity parameter: {parameter}. "
            f"Available: {sorted(SENSITIVITY_PARAMETERS)}"
        )

    requested = SENSITIVITY_PARAMETERS[parameter] if values is None else values
    sweep = tuple(float(value) for value in requested)
    if not sweep:
        raise ValueError("At least one parameter value is required.")

    seed_tuple = tuple(int(value) for value in seeds)
    workload = spec or DEFAULT_SCENARIO_SPEC

    runs: List[ScenarioRunResult] = []
    per_run_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for value in sweep:
        preset = sensitivity_scenario(parameter, value, workload)
        value_rows: List[Dict[str, Any]] = []

        for seed in seed_tuple:
            run = run_scenario(
                preset,
                seed=seed,
                spec=workload,
                scheduler=scheduler,
                algorithm=algorithm,
                routing_weights=routing_weights,
            )
            runs.append(run)
            row = run.summary_row()
            row["Parameter"] = parameter
            row["Value"] = value
            value_rows.append(row)
            per_run_rows.append(row)

        frame = pd.DataFrame(value_rows)

        summary_rows.append(
            {
                "Parameter": SENSITIVITY_LABELS.get(parameter, parameter),
                "Value": value,
                "Runs": len(seed_tuple),
                "Packets Sent": frame["Packets Sent"].mean(),
                "Delivered": frame["Delivered"].mean(),
                "Avg Latency (ms)": frame["Avg Latency (ms)"].mean(),
                "Latency Std (ms)": frame["Latency Std (ms)"].mean(),
                "Avg Latency Run Std Dev (ms)": _std(
                    [float(value) for value in frame["Avg Latency (ms)"]]
                ),
                "Jitter (ms)": frame["Jitter (ms)"].mean(),
                "Throughput (B/s)": frame["Throughput (B/s)"].mean(),
                "Packet Loss (%)": frame["Packet Loss (%)"].mean(),
                "PDR (%)": frame["PDR (%)"].mean(),
                "Avg Queue Wait (ms)": frame["Avg Queue Wait (ms)"].mean(),
                "Max Queue Length": frame["Max Queue Length"].mean(),
                "Avg Queue Wait Run Std Dev (ms)": _std(
                    [float(value) for value in frame["Avg Queue Wait (ms)"]]
                ),
                "Route Changes": frame["Route Changes"].mean(),
            }
        )

    return SensitivityResult(
        parameter=parameter,
        values=sweep,
        seeds=seed_tuple,
        workload=workload,
        scheduler=scheduler,
        algorithm=algorithm,
        summary=pd.DataFrame(summary_rows),
        per_run=pd.DataFrame(per_run_rows),
        runs=runs,
    )


# --------------------------------------------------------------------------
# Scenario comparison
# --------------------------------------------------------------------------


@dataclass
class ScenarioComparisonResult:
    """Several scenarios measured on one identical workload and seed."""

    scenario_keys: Tuple[str, ...]
    workload: WorkloadSpec
    seed: int
    scheduler: str
    algorithm: str
    summary: pd.DataFrame
    route_changes: pd.DataFrame
    runs: List[ScenarioRunResult] = field(default_factory=list, repr=False)

    def describe(self) -> Dict[str, Any]:
        return {
            "experiment": "Scenario comparison",
            "scenarios": list(self.scenario_keys),
            "workload": self.workload.describe(),
            "seed": self.seed,
            "scheduler": SCHEDULER_LABELS.get(self.scheduler, self.scheduler),
            "algorithm": ALGORITHM_LABELS.get(self.algorithm, self.algorithm),
        }

    def frames(self) -> Dict[str, pd.DataFrame]:
        return {
            "scenario comparison": self.summary,
            "scenario route changes": self.route_changes,
        }


def compare_scenarios(
    keys: Optional[Sequence[str]] = None,
    *,
    spec: Optional[WorkloadSpec] = None,
    seed: int = DEFAULT_SCENARIO_SEED,
    scheduler: str = "fifo",
    algorithm: str = "dijkstra",
    routing_weights: Optional[Dict[str, float]] = None,
) -> ScenarioComparisonResult:
    """
    Compare several scenarios under the *same* workload and seed.

    Only the scenario definition changes between rows, which makes the
    comparison fair.
    """
    selected = tuple(scenario_keys()) if keys is None else tuple(keys)
    if not selected:
        raise ValueError("At least one scenario is required for a comparison.")

    workload = replace(spec or DEFAULT_SCENARIO_SPEC, seed=int(seed))

    runs: List[ScenarioRunResult] = []
    for key in selected:
        runs.append(
            run_scenario(
                get_scenario(key),
                seed=int(seed),
                spec=workload,
                scheduler=scheduler,
                algorithm=algorithm,
                routing_weights=routing_weights,
            )
        )

    summary = pd.DataFrame([run.summary_row() for run in runs])
    route_changes = pd.DataFrame(
        [
            {
                "Scenario": run.scenario.name,
                "Scenario Key": run.scenario.key,
                "Route Changes": run.resilience["Route Changes"],
                "Initial Route": run.resilience["Initial Route"],
                "Final Route": run.resilience["Final Route"],
                "Detection Delay (s)": run.resilience["Detection Delay (s)"],
                "Outage Time (s)": run.resilience["Outage Time (s)"],
            }
            for run in runs
        ]
    )

    return ScenarioComparisonResult(
        scenario_keys=selected,
        workload=workload,
        seed=int(seed),
        scheduler=scheduler,
        algorithm=algorithm,
        summary=summary,
        route_changes=route_changes,
        runs=runs,
    )


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def _slug(name: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "_" for character in str(name)
    )
    return "_".join(part for part in cleaned.split("_") if part) or "frame"


def export_frames(frames: Dict[str, pd.DataFrame], directory: str) -> List[str]:
    """
    Write every frame of an experiment to ``<directory>/<name>.csv``.

    Returns the written paths. The CSV content is exactly the generated
    experiment data.
    """
    os.makedirs(directory, exist_ok=True)

    written: List[str] = []
    for name, frame in frames.items():
        path = os.path.join(directory, f"{_slug(name)}.csv")
        frame.to_csv(path, index=False)
        written.append(path)

    return written


def export_result(result: Any, directory: str) -> List[str]:
    """Export any Stage 6 result object that exposes ``frames()``."""
    return export_frames(result.frames(), directory)
