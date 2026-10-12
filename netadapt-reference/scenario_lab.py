"""
Stage 6 -- Scenario Lab tab for the NetAdapt dashboard.

The tab body lives here so the (already large) ``app.py`` only has to call
:func:`render_scenario_lab`. The Streamlit / Plotly helpers and the shared
workload controls are passed in from the dashboard so both stay in sync and no
UI code is duplicated.

Everything displayed comes from ``scenarios.py``, which in turn drives the real
simulation engine -- there are no hardcoded results.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from experiments import QOS_SCHEDULERS
from qos import SCHEDULER_LABELS
from routing import ALGORITHM_LABELS
from scenarios import (
    SENSITIVITY_LABELS,
    SENSITIVITY_PARAMETERS,
    compare_scenarios,
    get_scenario,
    run_multi_run_experiment,
    run_scenario,
    run_sensitivity_experiment,
    scenario_keys,
    scenario_labels,
)


def _format_value(value: Any) -> str:
    """Render a resilience measurement as display text.

    Mixing numbers and route strings in one column upsets Arrow's type
    inference, so every cell is normalised to text first.
    """
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_scenario_lab(
    *,
    bar_chart: Callable[..., Any],
    grouped_chart: Callable[..., Any],
    topology_figure: Callable[..., Any],
    workload_controls: Callable[..., Any],
    download_frame: Callable[..., None],
) -> None:
    """Render the Scenario Lab (Stage 6) tab."""
    st.markdown("## 🧨 Scenario Laboratory — Advanced Scenarios & Resilience")
    st.markdown(
        "Run a **whole network scenario** end to end — link conditions, injected failures and "
        "recovery — through the simulation engine, then measure resilience, recoverability, "
        "route stability and parameter sensitivity. Every value is measured by the engine."
    )

    scenario_names = scenario_labels()
    scenario_left, scenario_right = st.columns([1, 2])

    with scenario_left:
        st.markdown("### ⚙️ Scenario configuration")
        with st.container(border=True):
            scenario_key = st.selectbox(
                "Scenario preset",
                options=scenario_keys(),
                format_func=lambda key: scenario_names.get(key, key),
                key="scenario_preset",
            )
            selected_scenario = get_scenario(scenario_key)
            st.caption(selected_scenario.description)
            st.caption(f"**Conditions:** {selected_scenario.conditions.label}")
            st.caption(f"**Failures:** {selected_scenario.failures.label()}")
            st.caption(
                f"**Detection timeout:** {selected_scenario.failure_detection_timeout:g}s · "
                f"**default seed:** {selected_scenario.seed}"
            )

            scenario_spec = workload_controls("scenario", selected_scenario.workload)

            scenario_scheduler = st.selectbox(
                "QoS scheduler",
                options=list(QOS_SCHEDULERS),
                format_func=lambda key: SCHEDULER_LABELS.get(key, key),
                key="scenario_scheduler",
            )
            scenario_algorithm = st.selectbox(
                "Routing algorithm",
                options=list(ALGORITHM_LABELS.keys()),
                format_func=lambda key: ALGORITHM_LABELS[key],
                key="scenario_algorithm",
            )

            run_one = st.button(
                "▶️ Run scenario", type="primary", use_container_width=True
            )

            seeds_input = st.text_input(
                "Seeds for repeated experiments (comma separated)",
                value="1, 2, 3, 4, 5",
                key="scenario_seeds",
            )
            run_many = st.button(
                "🔁 Run repeated experiments", use_container_width=True
            )

            sensitivity_parameter = st.selectbox(
                "Sensitivity parameter",
                options=list(SENSITIVITY_PARAMETERS.keys()),
                format_func=lambda name: SENSITIVITY_LABELS.get(name, name),
                key="sensitivity_parameter",
            )
            run_sensitivity = st.button(
                "📈 Run sensitivity analysis", use_container_width=True
            )

            comparison_selection = st.multiselect(
                "Scenarios to compare",
                options=scenario_keys(),
                default=scenario_keys()[:4],
                format_func=lambda key: scenario_names.get(key, key),
                key="comparison_scenarios",
            )
            run_comparison = st.button(
                "⚖️ Compare scenarios", use_container_width=True
            )

    def parsed_seeds() -> List[int]:
        seeds = [
            int(part)
            for part in seeds_input.replace(";", ",").split(",")
            if part.strip().lstrip("-").isdigit()
        ]
        return seeds or [scenario_spec.seed]

    if run_one:
        with st.spinner(f"Simulating scenario '{selected_scenario.name}'..."):
            st.session_state.scenario_result = run_scenario(
                scenario_key,
                spec=scenario_spec,
                scheduler=scenario_scheduler,
                algorithm=scenario_algorithm,
            )

    if run_many:
        with st.spinner("Running the scenario repeatedly with different seeds..."):
            st.session_state.scenario_multi_result = run_multi_run_experiment(
                scenario_key,
                seeds=tuple(parsed_seeds()),
                spec=scenario_spec,
                scheduler=scenario_scheduler,
                algorithm=scenario_algorithm,
            )

    if run_sensitivity:
        with st.spinner("Sweeping the network parameter through the simulator..."):
            st.session_state.sensitivity_result = run_sensitivity_experiment(
                sensitivity_parameter,
                spec=scenario_spec,
                scheduler=scenario_scheduler,
                algorithm=scenario_algorithm,
            )

    if run_comparison:
        if not comparison_selection:
            st.error("Select at least one scenario to compare.")
        else:
            with st.spinner("Measuring every scenario on the same workload..."):
                st.session_state.scenario_comparison_result = compare_scenarios(
                    comparison_selection,
                    spec=scenario_spec,
                    scheduler=scenario_scheduler,
                    algorithm=scenario_algorithm,
                )

    with scenario_right:
        single_result = st.session_state.get("scenario_result")
        if single_result is not None:
            resilience: Dict[str, Any] = single_result.resilience
            st.markdown(f"### 📊 {single_result.scenario.name}")
            st.caption(
                f"seed {single_result.seed} · {single_result.workload.total_packets} packets · "
                f"conditions: {single_result.scenario.conditions.label} · "
                f"failures: {single_result.scenario.failures.label()}"
            )

            stat1, stat2, stat3, stat4, stat5 = st.columns(5)
            stat1.metric(
                "Avg Latency",
                f"{single_result.overall['average_latency'] * 1000:.1f} ms",
            )
            stat2.metric("PDR", f"{single_result.overall['packet_delivery_ratio']:.1f}%")
            stat3.metric("Packet Loss", f"{single_result.overall['packet_loss']:.1f}%")
            stat4.metric("Route Changes", int(resilience["Route Changes"]))
            stat5.metric("Outage Time", f"{resilience['Outage Time (s)']:.2f} s")

            st.markdown("#### ⏱️ Before failure / during failure / after recovery")
            phase_frame = single_result.phase_frame()
            st.dataframe(
                phase_frame.round(3), use_container_width=True, hide_index=True
            )
            st.plotly_chart(
                grouped_chart(
                    phase_frame.melt(
                        id_vars=["Phase"],
                        value_vars=["Avg Latency (ms)", "Throughput (B/s)"],
                        var_name="Metric",
                        value_name="Value",
                    ),
                    category_column="Metric",
                    value_column="Value",
                    group_column="Phase",
                    title="Measured behaviour in each phase",
                    y_title="Value",
                ),
                use_container_width=True,
                key="scenario_phase_chart",
            )

            st.markdown("#### 🧱 Resilience / recovery measurements")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Measurement": name, "Value": _format_value(value)}
                        for name, value in resilience.items()
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("#### 🛣️ Route history")
            if single_result.route_history.empty:
                st.info("No route decisions were recorded for this run.")
            else:
                st.dataframe(
                    single_result.route_history.round(3),
                    use_container_width=True,
                    hide_index=True,
                )
                if len(single_result.route_timeline) >= 2:
                    timeline_figure = go.Figure(
                        go.Scatter(
                            x=list(single_result.route_timeline["Time (s)"]),
                            y=list(single_result.route_timeline["Route"]),
                            mode="lines+markers",
                            line=dict(color="#FF6692"),
                        )
                    )
                    timeline_figure.update_layout(
                        title="Selected route over simulation time",
                        xaxis_title="Simulation time (s)",
                        height=320,
                        margin=dict(l=20, r=20, t=50, b=20),
                    )
                    st.plotly_chart(
                        timeline_figure,
                        use_container_width=True,
                        key="scenario_route_timeline_chart",
                    )

            if single_result.simulator is not None and resilience["Final Route"]:
                st.markdown("#### 🗺️ Final route on the topology")
                st.plotly_chart(
                    topology_figure(
                        single_result.simulator,
                        resilience["Final Route"].split(" → "),
                    ),
                    use_container_width=True,
                    key="scenario_topology_chart",
                )

            with st.expander("Per-class QoS metrics of this scenario run"):
                st.dataframe(
                    single_result.class_metrics.round(3),
                    use_container_width=True,
                    hide_index=True,
                )

            download_frame(
                single_result.summary_frame(),
                "scenario summary",
                "netadapt_scenario_summary.csv",
                "scenario_summary_download",
            )
            download_frame(
                single_result.route_history,
                "route history",
                "netadapt_scenario_route_history.csv",
                "scenario_route_history_download",
            )
            st.divider()

        multi_result = st.session_state.get("scenario_multi_result")
        if multi_result is not None:
            st.markdown("### 🔁 Repeated experiments (multi-seed)")
            st.caption(
                f"{multi_result.scenario.name} · {len(multi_result.seeds)} runs · seeds: "
                + ", ".join(str(seed) for seed in multi_result.seeds)
            )
            st.dataframe(
                multi_result.per_run.round(3), use_container_width=True, hide_index=True
            )

            st.markdown("#### 📐 Aggregated statistics")
            st.dataframe(
                multi_result.aggregate.round(4),
                use_container_width=True,
                hide_index=True,
            )

            latency_row = multi_result.aggregate[
                multi_result.aggregate["Metric"] == "Avg Latency (ms)"
            ]
            if not latency_row.empty:
                st.caption(
                    f"Average latency across runs: {latency_row['Mean'].iloc[0]:.2f} ms "
                    f"± {latency_row['Std Dev'].iloc[0]:.2f} ms "
                    f"(min {latency_row['Min'].iloc[0]:.2f}, "
                    f"max {latency_row['Max'].iloc[0]:.2f})"
                )

            st.plotly_chart(
                bar_chart(
                    [str(seed) for seed in multi_result.seeds],
                    list(multi_result.per_run["Avg Latency (ms)"]),
                    "Average latency per run seed",
                    "ms",
                    color="#636EFA",
                ),
                use_container_width=True,
                key="scenario_multi_run_chart",
            )
            download_frame(
                multi_result.per_run,
                "per-run results",
                "netadapt_multi_run_per_run.csv",
                "multi_run_download",
            )
            st.divider()

        sensitivity_result = st.session_state.get("sensitivity_result")
        if sensitivity_result is not None:
            parameter_label = SENSITIVITY_LABELS.get(
                sensitivity_result.parameter, sensitivity_result.parameter
            )
            st.markdown(f"### 📈 Sensitivity analysis — {parameter_label}")
            st.caption(
                "One network parameter is varied while every other condition stays "
                f"identical · workload {sensitivity_result.workload.total_packets} packets · "
                "seeds: "
                + ", ".join(str(seed) for seed in sensitivity_result.seeds)
            )
            st.dataframe(
                sensitivity_result.summary.round(3),
                use_container_width=True,
                hide_index=True,
            )

            sensitivity_chart = go.Figure()
            for column_name, color, axis in (
                ("Avg Latency (ms)", "#636EFA", "y"),
                ("PDR (%)", "#00CC96", "y2"),
            ):
                sensitivity_chart.add_trace(
                    go.Scatter(
                        x=list(sensitivity_result.summary["Value"]),
                        y=list(sensitivity_result.summary[column_name]),
                        mode="lines+markers",
                        name=column_name,
                        line=dict(color=color),
                        yaxis=axis,
                    )
                )
            sensitivity_chart.update_layout(
                title=f"Impact of {parameter_label}",
                xaxis_title="Parameter value",
                yaxis=dict(title="Avg Latency (ms)"),
                yaxis2=dict(title="PDR (%)", overlaying="y", side="right"),
                height=380,
                margin=dict(l=20, r=20, t=50, b=20),
            )
            st.plotly_chart(
                sensitivity_chart,
                use_container_width=True,
                key="scenario_sensitivity_chart",
            )

            download_frame(
                sensitivity_result.summary,
                "sensitivity results",
                "netadapt_sensitivity.csv",
                "sensitivity_download",
            )
            st.divider()

        comparison_result = st.session_state.get("scenario_comparison_result")
        if comparison_result is not None:
            st.markdown("### ⚖️ Scenario comparison (identical workload and seed)")
            st.caption(
                f"{comparison_result.workload.total_packets} packets · seed "
                f"{comparison_result.seed} · conditions differ only by scenario"
            )
            st.dataframe(
                comparison_result.summary.round(3),
                use_container_width=True,
                hide_index=True,
            )
            st.plotly_chart(
                bar_chart(
                    list(comparison_result.summary["Scenario"]),
                    list(comparison_result.summary["Avg Latency (ms)"]),
                    "Average latency per scenario",
                    "ms",
                    color="#AB63FA",
                    height=380,
                ),
                use_container_width=True,
                key="scenario_comparison_latency_chart",
            )
            st.plotly_chart(
                bar_chart(
                    list(comparison_result.summary["Scenario"]),
                    list(comparison_result.summary["PDR (%)"]),
                    "Packet delivery ratio per scenario",
                    "%",
                    color="#00CC96",
                    height=380,
                ),
                use_container_width=True,
                key="scenario_comparison_pdr_chart",
            )

            st.markdown("#### 🛣️ Route changes per scenario")
            st.dataframe(
                comparison_result.route_changes,
                use_container_width=True,
                hide_index=True,
            )
            download_frame(
                comparison_result.summary,
                "scenario comparison",
                "netadapt_scenario_comparison.csv",
                "scenario_comparison_download",
            )
            st.divider()

        if all(
            st.session_state.get(key) is None
            for key in (
                "scenario_result",
                "scenario_multi_result",
                "sensitivity_result",
                "scenario_comparison_result",
            )
        ):
            st.info(
                "Pick a scenario preset and press **Run scenario** to simulate it end to end. "
                "Use **Run repeated experiments** for multi-seed statistics, **Run sensitivity "
                "analysis** to sweep one network parameter, and **Compare scenarios** to measure "
                "several scenarios on the same workload."
            )
