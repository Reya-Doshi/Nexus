"""
Stage 6 tests -- advanced network scenarios and resilience evaluation.

Covers the scenario framework (presets, application, reset), the phased
scenario runner, resilience / recovery measurement, route-stability tracking,
multi-run experiments, the sensitivity experiment, scenario comparison,
reproducibility with deterministic seeds and CSV / DataFrame export.

Every expectation is derived from real simulation output: the numbers asserted
here are produced by ``simulator.py`` / ``scenarios.py``, never written by hand.
"""

import os

import pytest

from experiments import DEFAULT_ROUTING_WEIGHTS, WorkloadSpec, build_simulator
from scenarios import (
    SENSITIVITY_LINKS,
    SENSITIVITY_PARAMETERS,
    SCENARIO_BY_KEY,
    SCENARIO_PRESETS,
    FailureSpec,
    ScenarioPreset,
    apply_scenario,
    clear_failures,
    compare_scenarios,
    export_frames,
    export_result,
    get_scenario,
    reset_scenario,
    run_multi_run_experiment,
    run_scenario,
    run_sensitivity_experiment,
    scenario_keys,
    scenario_labels,
    sensitivity_scenario,
    workload_route_timeline,
)
from simulator import NetworkSimulator

SMALL = WorkloadSpec(packets_per_class=4, packet_size=1200, pps=50.0, seed=42)

LOSSY_SCENARIOS = ("high_congestion", "high_packet_loss", "loss_and_congestion")


# --------------------------------------------------------------------------
# Scenario framework
# --------------------------------------------------------------------------


def test_scenario_presets_cover_every_required_kind():
    keys = scenario_keys()

    assert len(keys) == len(set(keys)) == len(SCENARIO_PRESETS)

    for required in (
        "normal",
        "high_congestion",
        "high_packet_loss",
        "reduced_bandwidth",
        "link_failure",
        "router_failure",
        "multiple_link_failures",
        "congestion_and_failure",
        "loss_and_congestion",
        "bandwidth_degradation_and_congestion",
        "combined_degraded_network",
    ):
        assert required in SCENARIO_BY_KEY

    for preset in SCENARIO_PRESETS:
        assert isinstance(preset, ScenarioPreset)
        assert preset.name and preset.description
        assert preset.seed == 42
        assert scenario_labels()[preset.key] == preset.name
        assert preset.describe()["conditions"]["label"] == preset.conditions.label


def test_get_scenario_lookup_and_unknown_key():
    assert get_scenario("link_failure") is SCENARIO_BY_KEY["link_failure"]

    with pytest.raises(ValueError):
        get_scenario("does-not-exist")


def test_failure_spec_labels_and_components():
    assert FailureSpec().empty
    assert FailureSpec().components() == []
    assert FailureSpec().label() == "none"

    spec = FailureSpec(links=(("R3", "R5"),), nodes=("R3",))
    assert not spec.empty
    assert spec.components() == ["R3-R5", "R3"]
    assert spec.label() == "R3-R5, node R3"


def test_scenario_presets_define_real_conditions_and_failures():
    congestion = get_scenario("high_congestion")
    assert congestion.conditions.groups[0].congestion == 0.75
    assert congestion.conditions.groups[0].links == tuple(
        get_scenario("high_packet_loss").conditions.groups[0].links
    )

    assert get_scenario("high_packet_loss").conditions.groups[0].packet_loss == 0.10
    assert get_scenario("reduced_bandwidth").conditions.groups[0].bandwidth == 10.0
    assert get_scenario("reduced_bandwidth").workload.packet_size == 8000

    combined = get_scenario("combined_degraded_network")
    assert len(combined.conditions.groups) == 2
    assert combined.has_failures
    assert len(combined.failures.links) == 2

    assert get_scenario("normal").conditions.groups == ()
    assert not get_scenario("normal").has_failures


def test_apply_scenario_changes_link_conditions():
    sim = NetworkSimulator(seed=42)

    apply_scenario(sim, "high_congestion")
    assert sim.topology.graph["R1"]["R3"]["congestion"] == pytest.approx(0.75)
    assert sim.topology.graph["H1"]["R1"]["congestion"] == pytest.approx(0.75)

    apply_scenario(sim, "reduced_bandwidth")
    assert sim.topology.graph["R1"]["R3"]["bandwidth"] == pytest.approx(10.0)
    # Applying a new scenario must clear the previous degradation.
    assert sim.topology.graph["R1"]["R3"]["congestion"] == pytest.approx(0.0)

    apply_scenario(sim, "normal")
    assert sim.topology.graph["R1"]["R3"]["bandwidth"] == pytest.approx(80.0)
    assert sim.topology.graph["R1"]["R3"]["packet_loss"] == pytest.approx(0.0)


def test_reset_scenario_restores_defaults_and_clears_failures():
    sim = NetworkSimulator(seed=42)
    apply_scenario(sim, "combined_degraded_network")
    sim.fail_link("R3", "R5")
    sim.fail_node("R2")

    assert sim.is_link_failed("R3", "R5")
    assert sim.is_node_failed("R2")
    assert sim.topology.graph["R3"]["R5"]["packet_loss"] > 0.0

    reset_scenario(sim)

    assert not sim.is_link_failed("R3", "R5")
    assert not sim.is_node_failed("R2")
    assert sim.get_failed_links() == []
    assert sim.get_failed_nodes() == []
    assert sim.topology.active_link("R3", "R5")
    assert sim.topology.active_node("R2")
    for u, v, data in sim.topology.graph.edges(data=True):
        assert data["congestion"] == pytest.approx(0.0)
        assert data["packet_loss"] == pytest.approx(0.0)
        assert data["bandwidth"] == pytest.approx(
            sim.topology.get_original_bandwidth(u, v)
        )


def test_clear_failures_leaves_conditions_untouched():
    sim = NetworkSimulator(seed=42)
    apply_scenario(sim, "high_congestion")
    sim.fail_link("R1", "R3")

    clear_failures(sim)

    assert not sim.is_link_failed("R1", "R3")
    assert sim.topology.graph["R1"]["R3"]["congestion"] == pytest.approx(0.75)


# --------------------------------------------------------------------------
# Phased scenario execution
# --------------------------------------------------------------------------


def test_run_normal_scenario_has_no_failures_and_real_metrics():
    result = run_scenario("normal")

    assert result.scenario.key == "normal"
    assert result.seed == 42
    assert result.simulator is not None
    assert result.overall["packets_sent"] == result.workload.total_packets
    assert result.overall["packets_delivered"] > 0
    assert result.overall["average_latency"] > 0.0
    assert result.latency_std > 0.0

    # No failures -> no route change, no recovery.
    assert result.resilience["Route Changes"] == 0
    assert result.resilience["Outage Time (s)"] == 0.0
    assert result.resilience["Detection Delay (s)"] == 0.0
    assert result.resilience["Initial Route"] == result.resilience["Final Route"]
    assert result.resilience["Failed Flows"] == 0
    assert result.resilience["Successful Flows"] == len(result.workload.classes)


def test_scenario_phases_split_the_workload():
    result = run_scenario("normal")

    frame = result.phase_frame()
    assert list(frame["Phase"]) == ["Before failure", "During failure", "After recovery"]
    assert set(result.phases) == {"before", "during", "after"}

    # before 35% / during 30% / after the rest of 50 packets.
    assert list(frame["Packets"]) == [17, 15, 18]
    assert int(frame["Packets"].sum()) == result.workload.total_packets
    assert list(frame["Delivered"]) == [17, 15, 18]

    # Cumulative metrics of every phase were measured separately.
    for phase in ("before", "during", "after"):
        values = result.phases[phase]
        assert values["packets_sent"] > 0
        assert values["average_latency"] > 0.0
        assert values["throughput"] > 0.0
        assert result.phase_durations[phase] > 0.0


def test_run_scenario_respects_custom_workload():
    spec = WorkloadSpec(packets_per_class=6, packet_size=900, pps=25.0, seed=7)
    result = run_scenario("high_packet_loss", spec=spec, seed=11)

    assert result.workload.packets_per_class == 6
    assert result.workload.packet_size == 900
    assert result.workload.seed == 11
    assert result.seed == 11
    assert result.overall["packets_sent"] == spec.total_packets


def test_scenario_degradation_is_actually_applied_to_the_simulator():
    congested = run_scenario("high_congestion", spec=SMALL)
    sim = congested.simulator
    assert sim is not None
    assert sim.topology.graph["R1"]["R3"]["congestion"] == pytest.approx(0.75)
    assert congested.overall["packet_loss"] > 0.0
    assert congested.overall["packet_delivery_ratio"] < 100.0


# --------------------------------------------------------------------------
# Resilience evaluation
# --------------------------------------------------------------------------


def test_link_failure_resilience_measurements_are_real():
    result = run_scenario("link_failure")
    resilience = result.resilience

    assert resilience["Failed Components"] == "R3-R5"
    assert resilience["Failure Time (s)"] > 0.0
    assert resilience["Detection Time (s)"] > resilience["Failure Time (s)"]
    # The runner advances the clock past the detection timeout (1.0s + 0.5s).
    assert resilience["Detection Delay (s)"] == pytest.approx(1.5)
    assert resilience["Recovery Time (s)"] > resilience["Detection Time (s)"]
    assert resilience["Outage Time (s)"] == pytest.approx(
        resilience["Recovery Time (s)"] - resilience["Failure Time (s)"]
    )
    assert resilience["Route Changes"] >= 1
    assert resilience["Packets Affected"] > 0
    # Everything the engine processed during the outage still arrived intact.
    assert resilience["Packets Delivered After Recovery"] > 0


def test_router_failure_and_multiple_link_failures_recover():
    for key in ("router_failure", "multiple_link_failures"):
        result = run_scenario(key)
        resilience = result.resilience

        assert result.scenario.has_failures
        assert resilience["Outage Time (s)"] > 0.0
        assert resilience["Route Changes"] >= 1
        assert resilience["Final Route"]
        # The engine detected, rerouted and never lost the destination.
        assert result.overall["packets_delivered"] > 0
        assert result.overall["packet_delivery_ratio"] == pytest.approx(100.0)


def test_during_failure_performance_differs_from_before():
    result = run_scenario("link_failure")

    before = result.phases["before"]
    during = result.phases["during"]
    after = result.phases["after"]

    # The outage must be visible in the measured phase metrics.
    assert during["average_latency"] > before["average_latency"]
    assert during["throughput"] < before["throughput"]
    assert after["throughput"] > during["throughput"]

    for phase in (before, during, after):
        assert phase["packets_dropped"] == 0


def test_multi_link_failure_reroutes_around_both_failed_links():
    result = run_scenario("multiple_link_failures")

    timeline = list(result.route_timeline["Route"])
    assert len(timeline) >= 2
    assert timeline[0] != timeline[1]

    failed = {("R3", "R5"), ("R4", "R5")}
    for a, b in zip(timeline[1].split(" → "), timeline[1].split(" → ")[1:]):
        assert tuple(sorted((a, b))) not in failed

    # After both links recover the engine returns to the optimal route.
    assert timeline[-1] == timeline[0]
    assert result.resilience["Route Changes"] >= 2


# --------------------------------------------------------------------------
# Route stability
# --------------------------------------------------------------------------


def test_route_history_records_real_decisions():
    result = run_scenario("link_failure")
    history = result.route_history

    assert set(
        ["Time (s)", "Flow", "Change", "Previous Route", "Route", "Route Cost"]
    ) <= set(history.columns)
    assert len(history) > 0

    initial = history[history["Change"] == "INITIAL"]
    recalculated = history[history["Change"] == "RECALCULATED"]

    assert len(initial) == len(result.workload.classes)
    assert (initial["Route"] == result.resilience["Initial Route"]).all()
    assert len(recalculated) > 0
    assert (recalculated["Time (s)"] > 0).all()
    assert (recalculated["Route Cost"] > 0).all()


def test_route_timeline_and_per_flow_history():
    result = run_scenario("multiple_link_failures")

    timeline = list(result.route_timeline["Route"])
    assert timeline[0] == "H1 → R1 → R3 → R5 → H3"
    assert len(timeline) == result.resilience["Route Changes"] + 1

    timeline_with_ids = workload_route_timeline(
        result.simulator, list(result.flow_routes.keys())
    )
    assert [route for _, route in timeline_with_ids] == timeline

    # Every flow of the workload observed the same number of route changes.
    lengths = {len(routes) for routes in result.flow_routes.values()}
    assert len(result.flow_routes) == len(result.workload.classes)
    assert lengths == {len(timeline)}


def test_route_changes_count_is_zero_when_there_are_no_failures():
    for key in ("normal", "high_congestion", "loss_and_congestion"):
        result = run_scenario(key, spec=SMALL)
        assert result.resilience["Route Changes"] == 0
        assert len(result.route_timeline) == 1
        assert result.resilience["Initial Route"] == result.resilience["Final Route"]


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------


def test_same_seed_and_configuration_is_reproducible():
    first = run_scenario("link_failure", spec=SMALL)
    second = run_scenario("link_failure", spec=SMALL)

    assert first.summary_row() == second.summary_row()
    assert first.resilience == second.resilience
    assert first.phases == second.phases
    assert first.phase_durations == second.phase_durations
    assert first.route_history.to_dict("records") == second.route_history.to_dict(
        "records"
    )
    assert first.route_timeline.to_dict("records") == second.route_timeline.to_dict(
        "records"
    )
    assert first.class_metrics.to_dict("records") == second.class_metrics.to_dict(
        "records"
    )


def test_same_seed_reproduces_the_engine_exactly():
    """Same topology + conditions + workload + seed + configuration => same result."""
    preset = get_scenario("combined_degraded_network")

    def simulate() -> dict:
        sim = build_simulator(
            SMALL,
            scheduler="fifo",
            algorithm="dijkstra",
            conditions=preset.conditions,
            routing_weights=dict(DEFAULT_ROUTING_WEIGHTS),
            failure_detection_timeout=preset.failure_detection_timeout,
        )
        sim.generate_interleaved_traffic(
            SMALL.source,
            SMALL.destination,
            classes=list(SMALL.classes),
            packets_per_class=SMALL.packets_per_class,
            packet_size=SMALL.packet_size,
            pps=SMALL.pps,
        )
        sim.run_until_empty()
        return {
            "metrics": sim.metrics.calculate(sim.time, sim.average_congestion()),
            "loss_rolls": [packet.loss_roll for packet in sim.packets],
            "routes": {flow.flow_id: list(flow.current_route) for flow in sim.active_flows.values()},
        }

    assert simulate() == simulate()


def test_deterministic_seeds_through_the_scenario_runner():
    repeat = run_multi_run_experiment("link_failure", seeds=(42, 42, 42), spec=SMALL)

    assert len(repeat.per_run) == 3
    assert repeat.per_run["Avg Latency (ms)"].nunique() == 1
    assert repeat.per_run["PDR (%)"].nunique() == 1
    assert repeat.per_run["Recovery Time (s)"].nunique() == 1
    # Identical configuration and seed must give an identical aggregate.
    assert (repeat.aggregate["Std Dev"].abs() < 1e-9).all()
    assert (repeat.aggregate["Min"] == repeat.aggregate["Max"]).all()


def test_different_seeds_change_the_random_realisation_for_lossy_scenarios():
    runs = run_multi_run_experiment(
        "high_packet_loss", seeds=(1, 2, 3, 4), spec=SMALL
    )

    assert len(runs.per_run) == 4
    # The same workload/schedule, but drawing different loss rolls can deliver a
    # different number of packets.
    assert runs.per_run["Packets Sent"].nunique() == 1
    assert runs.per_run["Packet Loss (%)"].nunique() > 1
    assert runs.aggregate.loc[
        runs.aggregate["Metric"] == "Packet Loss (%)", "Std Dev"
    ].iloc[0] > 0.0


# --------------------------------------------------------------------------
# Multi-run experiments
# --------------------------------------------------------------------------


def test_multi_run_experiment_aggregates_every_requested_metric():
    result = run_multi_run_experiment(
        "congestion_and_failure", seeds=(1, 2, 3, 4, 5), spec=SMALL
    )

    assert len(result.seeds) == 5
    assert len(result.per_run) == 5
    assert len(result.runs) == 5

    for column in (
        "Avg Latency (ms)",
        "Latency Std (ms)",
        "Throughput (B/s)",
        "Packet Loss (%)",
        "PDR (%)",
        "Jitter (ms)",
        "Avg Queue Wait (ms)",
        "Max Queue Length",
        "Recovery Time (s)",
        "Route Changes",
        "Successful Flows",
        "Failed Flows",
        "Packets Affected",
    ):
        assert column in result.per_run.columns

    aggregate = result.aggregate.set_index("Metric")
    for metric in (
        "Avg Latency (ms)",
        "Throughput (B/s)",
        "PDR (%)",
        "Jitter (ms)",
        "Avg Queue Wait (ms)",
        "Max Queue Length",
        "Recovery Time (s)",
        "Route Changes",
    ):
        row = aggregate.loc[metric]
        assert row["Min"] - 1e-6 <= row["Mean"] <= row["Max"] + 1e-6
        assert row["Std Dev"] >= 0.0
        assert row["Runs"] == 5

    assert aggregate.loc["Avg Latency (ms)", "Mean"] > 0.0
    assert aggregate.loc["PDR (%)", "Mean"] < 100.0

    described = result.describe()
    assert described["runs"] == 5
    assert described["scenario"] == "congestion_and_failure"
    assert len(result.frames()) == 2


def test_multi_run_requires_at_least_one_seed():
    with pytest.raises(ValueError):
        run_multi_run_experiment("normal", seeds=())


# --------------------------------------------------------------------------
# Sensitivity experiment
# --------------------------------------------------------------------------


def test_sensitivity_scenario_varies_exactly_one_parameter():
    congestion = sensitivity_scenario("congestion", 0.4)
    group = congestion.conditions.groups[0]

    assert group.congestion == pytest.approx(0.4)
    assert group.packet_loss == pytest.approx(0.0)
    assert group.bandwidth is None
    assert group.links == SENSITIVITY_LINKS

    loss = sensitivity_scenario("packet_loss", 0.1)
    assert loss.conditions.groups[0].packet_loss == pytest.approx(0.1)
    assert loss.conditions.groups[0].congestion == pytest.approx(0.0)

    bandwidth = sensitivity_scenario("bandwidth", 25.0)
    assert bandwidth.conditions.groups[0].bandwidth == pytest.approx(25.0)
    assert bandwidth.conditions.groups[0].congestion == pytest.approx(0.0)
    assert bandwidth.conditions.groups[0].packet_loss == pytest.approx(0.0)

    with pytest.raises(ValueError):
        sensitivity_scenario("temperature", 10.0)


def test_congestion_sensitivity_uses_the_real_simulator():
    result = run_sensitivity_experiment(
        "congestion", values=(0.0, 0.4, 0.8), spec=SMALL
    )

    assert result.parameter == "congestion"
    assert list(result.summary["Value"]) == [0.0, 0.4, 0.8]
    assert list(result.values) == [0.0, 0.4, 0.8]

    # The workload really ran at every sensitivity point.
    assert (
        result.summary["Packets Sent"] == SMALL.total_packets
    ).all()

    latency = result.summary["Avg Latency (ms)"]
    pdr = result.summary["PDR (%)"]

    assert latency.is_monotonic_increasing
    assert latency.iloc[-1] > latency.iloc[0]
    assert pdr.iloc[-1] < pdr.iloc[0]
    assert result.summary["Avg Queue Wait (ms)"].iloc[-1] > 0.0
    assert result.summary["Route Changes"].nunique() >= 1

    described = result.describe()
    assert described["parameter"] == "congestion"
    assert described["values"] == [0.0, 0.4, 0.8]
    assert len(result.runs) == 3
    assert len(result.frames()) == 2


def test_packet_loss_sensitivity_increases_loss():
    result = run_sensitivity_experiment("packet_loss", spec=SMALL)
    summary = result.summary

    assert list(summary["Value"]) == list(SENSITIVITY_PARAMETERS["packet_loss"])
    assert summary["Packet Loss (%)"].iloc[0] == pytest.approx(0.0)
    assert summary["Packet Loss (%)"].is_monotonic_increasing
    assert summary["PDR (%)"].is_monotonic_decreasing
    assert summary["Packet Loss (%)"].iloc[-1] > 0.0
    # Same workload at every point, so the comparison is fair.
    assert summary["Packets Sent"].nunique() == 1


def test_bandwidth_sensitivity_degrades_performance():
    result = run_sensitivity_experiment("bandwidth", spec=SMALL)
    summary = result.summary

    assert list(summary["Value"]) == list(SENSITIVITY_PARAMETERS["bandwidth"])

    latency = summary["Avg Latency (ms)"]
    throughput = summary["Throughput (B/s)"]

    # Lower bandwidth must cost more latency and deliver less per second.
    assert latency.is_monotonic_increasing
    assert latency.iloc[-1] > latency.iloc[0]
    assert throughput.iloc[-1] < throughput.iloc[0]


def test_sensitivity_rejects_unknown_parameter_and_empty_values():
    with pytest.raises(ValueError):
        run_sensitivity_experiment("jitter", spec=SMALL)

    with pytest.raises(ValueError):
        run_sensitivity_experiment("congestion", values=(), spec=SMALL)


# --------------------------------------------------------------------------
# Scenario comparison
# --------------------------------------------------------------------------


def test_compare_scenarios_uses_identical_workload_and_seed():
    keys = ("normal", "high_congestion", "link_failure", "combined_degraded_network")
    result = compare_scenarios(keys, spec=SMALL, seed=9)

    assert list(result.scenario_keys) == list(keys)
    assert len(result.runs) == len(keys)
    assert len(result.summary) == len(keys)

    # Identical workload and seed for every scenario: only the scenario differs.
    assert result.summary["Packets Sent"].nunique() == 1
    assert result.summary["Seed"].nunique() == 1
    assert result.summary["Seed"].iloc[0] == 9
    assert (result.summary["Conditions"] != "").all()

    for column in (
        "Avg Latency (ms)",
        "Throughput (B/s)",
        "Packet Loss (%)",
        "PDR (%)",
        "Jitter (ms)",
        "Avg Queue Wait (ms)",
        "Recovery Time (s)",
        "Route Changes",
    ):
        assert result.summary[column].notna().all()

    # Degradation and failures must be visible in the comparison.
    indexed = result.summary.set_index("Scenario Key")
    assert indexed.loc["normal", "PDR (%)"] == pytest.approx(100.0)
    assert indexed.loc["high_congestion", "PDR (%)"] < indexed.loc["normal", "PDR (%)"]
    assert indexed.loc["high_congestion", "Avg Latency (ms)"] > indexed.loc[
        "normal", "Avg Latency (ms)"
    ]
    assert indexed.loc["link_failure", "Route Changes"] >= 1
    assert indexed.loc["normal", "Route Changes"] == 0
    assert indexed.loc["link_failure", "Recovery Time (s)"] > 0.0

    assert len(result.route_changes) == len(keys)
    assert set(result.frames()) == {
        "scenario comparison",
        "scenario route changes",
    }


def test_compare_scenarios_defaults_to_every_preset_and_rejects_empty():
    result = compare_scenarios(spec=SMALL)
    assert len(result.summary) == len(SCENARIO_PRESETS)
    assert list(result.summary["Scenario Key"]) == scenario_keys()

    with pytest.raises(ValueError):
        compare_scenarios([], spec=SMALL)

    # Repeating the comparison yields exactly the same measurements.
    repeat = compare_scenarios(spec=SMALL)
    assert repeat.summary.to_dict("records") == result.summary.to_dict("records")


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_export_writes_real_csv_data(tmp_path):
    result = run_scenario("link_failure", spec=SMALL)

    written = export_result(result, str(tmp_path))
    assert len(written) == len(result.frames())
    assert all(path.endswith(".csv") for path in written)

    for path in written:
        assert os.path.getsize(path) > 0

    summary_csv = (tmp_path / "scenario_summary.csv").read_text()
    assert "Scenario" in summary_csv.splitlines()[0]
    assert "H1 → R1" in summary_csv


def test_export_frames_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "exports"
    frames = {
        "sensitivity summary": run_scenario("normal", spec=SMALL).summary_frame(),
        "route timeline": run_scenario("link_failure", spec=SMALL).route_timeline,
    }

    written = export_frames(frames, str(target))

    assert target.is_dir()
    assert len(written) == 2
    assert (target / "sensitivity_summary.csv").exists()
    assert (target / "route_timeline.csv").exists()


def test_every_result_type_exposes_exportable_frames(tmp_path):
    results = [
        run_scenario("link_failure", spec=SMALL),
        run_multi_run_experiment("link_failure", seeds=(1, 2), spec=SMALL),
        run_sensitivity_experiment("congestion", values=(0.0, 0.5), spec=SMALL),
        compare_scenarios(["normal", "link_failure"], spec=SMALL),
    ]

    for index, result in enumerate(results):
        frames = result.frames()
        assert frames, f"result {index} exposes no frames"
        for name, frame in frames.items():
            assert not frame.empty, f"empty frame {name!r} in result {index}"

        directory = tmp_path / f"result-{index}"
        assert len(export_frames(frames, str(directory))) == len(frames)


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
