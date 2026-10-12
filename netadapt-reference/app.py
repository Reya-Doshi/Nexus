"""
NetAdapt — Adaptive and Self-Healing QoS-Based Network Routing System
Interactive network workspace + Stage 4/5 labs + Stage 6 scenario laboratory.

The dashboard only visualises data produced by the simulation engine
(``simulator.py`` / ``experiments.py`` / ``scenarios.py``). No metric is
hardcoded.
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from typing import Dict, List

from simulator import NetworkSimulator
from qos import (
    DEFAULT_WFQ_WEIGHTS,
    PRIORITIES,
    SCHEDULER_LABELS,
    TRAFFIC_CLASSES,
)
from routing import ALGORITHM_LABELS, DEFAULT_ROUTING_WEIGHTS, ROUTING_WEIGHT_NAMES
from experiments import (
    COMPARISON_CONDITIONS,
    DEFAULT_SPEC,
    NetworkConditions,
    QOS_SCHEDULERS,
    ROUTING_WEIGHT_PRESETS,
    STRESS_CONDITIONS,
    WEIGHT_SCENARIO_CONDITIONS,
    WorkloadSpec,
    run_combined_experiment,
    run_qos_experiment,
    run_qos_stress_test,
    run_routing_comparison,
    run_routing_weight_experiment,
)
from scenario_lab import render_scenario_lab
from network_workspace import render_network_workspace, render_workspace_controls


st.set_page_config(
    page_title="NetAdapt",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ------------------------------------------------------------------
# Session State Initialization
# ------------------------------------------------------------------
if "simulator" not in st.session_state:
    st.session_state.simulator = NetworkSimulator(
        seed=42, heartbeat_interval=1.0, failure_detection_timeout=2.0
    )

if "selected_source" not in st.session_state:
    st.session_state.selected_source = "H1"
if "selected_dest" not in st.session_state:
    st.session_state.selected_dest = "H3"
if "selected_flow_id" not in st.session_state:
    st.session_state.selected_flow_id = None
if "auto_rerun" not in st.session_state:
    st.session_state.auto_rerun = False

sim: NetworkSimulator = st.session_state.simulator

SCENARIOS: Dict[str, NetworkConditions] = {
    "Ideal (no degradation)": NetworkConditions(
        label="Ideal conditions (no degradation)", groups=()
    ),
    "Congested core (60% congestion, 10 Mbps link)": COMPARISON_CONDITIONS,
    "Heavy stress (90% congestion, 5% loss, 25 Mbps)": STRESS_CONDITIONS,
    "Degraded core (60% congestion, 35% loss)": WEIGHT_SCENARIO_CONDITIONS,
}


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------
def get_topology_figure(simulator: NetworkSimulator, highlight_route: List[str] | None = None):
    """Create Plotly figure for network topology."""
    pos = simulator.topology.get_positions()
    G = simulator.topology.graph

    edge_colors = []

    # Determine highlight edges set for quick lookup
    highlight_edges = set()
    if highlight_route and len(highlight_route) >= 2:
        for a, b in zip(highlight_route, highlight_route[1:]):
            highlight_edges.add(tuple(sorted((a, b))))

    # Collect all edges with their styling
    for u, v, data in G.edges(data=True):
        is_active = simulator.topology.active_link(u, v)
        key = tuple(sorted((u, v)))
        is_failed = key in simulator._failed_links
        is_highlight = key in highlight_edges

        # Edge styling
        if is_failed:
            color = "red"
            width = 4
            dash = "dash"
        elif is_highlight:
            color = "#00CC96"  # green for active route
            width = 5
            dash = "solid"
        elif not is_active:
            color = "gray"
            width = 2
            dash = "dot"
        else:
            # Color based on congestion
            congestion = data.get("congestion", 0.0)
            if congestion > 0.7:
                color = "orange"
                width = 3 + congestion * 2
            elif congestion > 0.3:
                color = "#FFA15A"
                width = 2 + congestion * 2
            else:
                color = "#636EFA"
                width = 2
            dash = "solid"

        edge_colors.append((color, width, dash, u, v, data, is_failed, is_highlight))

    fig = go.Figure()

    # Draw edges individually to allow different styles
    for color, width, dash, u, v, data, is_failed, is_highlight in edge_colors:
        x0, y0 = pos.get(u, (0, 0))
        x1, y1 = pos.get(v, (0, 0))
        status = "DOWN" if is_failed else data.get("status", "UP")
        hover = (
            f"{u} ↔ {v}<br>"
            f"Status: {status}<br>"
            f"Latency: {data.get('latency', 0)} ms<br>"
            f"Bandwidth: {data.get('bandwidth', 0)} Mbps<br>"
            f"Loss: {data.get('packet_loss', 0):.2%}<br>"
            f"Congestion: {data.get('congestion', 0):.2%}"
        )
        if is_highlight:
            hover += "<br><b>ACTIVE ROUTE</b>"

        fig.add_trace(
            go.Scatter(
                x=[x0, x1],
                y=[y0, y1],
                mode="lines",
                line=dict(color=color, width=width, dash=dash),
                hoverinfo="text",
                hovertext=hover,
                showlegend=False,
            )
        )

    # Nodes
    host_x, host_y, host_text, host_color, host_size, host_border = [], [], [], [], [], []
    router_x, router_y, router_text, router_color, router_size, router_border = [], [], [], [], [], []

    for node, data in G.nodes(data=True):
        x, y = pos.get(node, (0, 0))
        node_type = data.get("type", "router")
        status = data.get("status", "UP")
        is_failed = not simulator.topology.active_node(node)

        hover = f"{node} ({node_type})<br>Status: {status}"
        if is_failed:
            hover += "<br><b>FAILED</b>"

        if node_type == "host":
            host_x.append(x)
            host_y.append(y)
            host_text.append(f"{node}<br>{hover}")
            host_color.append("red" if is_failed else "#19D3F3")
            host_size.append(28)
            host_border.append("red" if is_failed else "black")
        else:
            router_x.append(x)
            router_y.append(y)
            router_text.append(f"{node}<br>{hover}")
            router_color.append(
                "red" if is_failed else "#FF6692" if node in (highlight_route or []) else "#AB63FA"
            )
            router_size.append(32 if node in (highlight_route or []) else 26)
            router_border.append("red" if is_failed else "black")

    fig.add_trace(
        go.Scatter(
            x=host_x,
            y=host_y,
            mode="markers+text",
            marker=dict(
                size=host_size,
                color=host_color,
                line=dict(width=2, color=host_border),
                symbol="square",
            ),
            text=[n for n in G.nodes if G.nodes[n].get("type") == "host"],
            textposition="top center",
            textfont=dict(size=12, color="black"),
            hovertext=host_text,
            hoverinfo="text",
            name="Hosts",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=router_x,
            y=router_y,
            mode="markers+text",
            marker=dict(
                size=router_size,
                color=router_color,
                line=dict(width=2, color=router_border),
                symbol="circle",
            ),
            text=[n for n in G.nodes if G.nodes[n].get("type") != "host"],
            textposition="top center",
            textfont=dict(size=12, color="black"),
            hovertext=router_text,
            hoverinfo="text",
            name="Routers",
        )
    )

    if highlight_route:
        mid_idx = len(highlight_route) // 2
        if mid_idx < len(highlight_route):
            mid_node = highlight_route[mid_idx]
            mx, my = pos.get(mid_node, (0, 0))
            fig.add_annotation(
                x=mx,
                y=my + 0.3,
                text=f"Route: {' → '.join(highlight_route)}",
                showarrow=False,
                bgcolor="rgba(0,204,150,0.2)",
                bordercolor="#00CC96",
                font=dict(size=11),
            )

    fig.update_layout(
        title="Network Topology — Active Route Highlighted",
        showlegend=True,
        hovermode="closest",
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor="white",
        height=500,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    return fig


def format_route(route: List[str]) -> str:
    return " → ".join(route) if route else "No route"


def bar_chart(
    labels: List[str],
    values: List[float],
    title: str,
    y_title: str,
    color: str = "#636EFA",
    height: int = 320,
    text_format: str = "%.2f",
):
    fig = go.Figure(
        go.Bar(
            x=labels,
            y=values,
            marker_color=color,
            text=[text_format % v for v in values],
            textposition="outside",
        )
    )
    fig.update_layout(
        title=title,
        yaxis_title=y_title,
        height=height,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def grouped_chart(
    frame: pd.DataFrame,
    category_column: str,
    value_column: str,
    group_column: str,
    title: str,
    y_title: str,
    height: int = 380,
):
    fig = go.Figure()
    for group in frame[group_column].unique():
        subset = frame[frame[group_column] == group]
        fig.add_trace(
            go.Bar(
                name=str(group),
                x=subset[category_column],
                y=subset[value_column],
            )
        )
    fig.update_layout(
        title=title,
        yaxis_title=y_title,
        barmode="group",
        height=height,
        margin=dict(l=20, r=20, t=50, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def workload_controls(prefix: str, defaults: WorkloadSpec = DEFAULT_SPEC) -> WorkloadSpec:
    """Shared workload configuration controls for the experiment labs."""
    col1, col2, col3, col4 = st.columns(4)
    packets_per_class = col1.number_input(
        "Packets per class",
        min_value=1,
        max_value=200,
        value=defaults.packets_per_class,
        step=1,
        key=f"{prefix}_packets",
    )
    packet_size = col2.number_input(
        "Packet size (bytes)",
        min_value=64,
        max_value=9000,
        value=defaults.packet_size,
        step=100,
        key=f"{prefix}_size",
    )
    pps = col3.number_input(
        "Packets/sec per class",
        min_value=1.0,
        max_value=500.0,
        value=float(defaults.pps),
        step=10.0,
        key=f"{prefix}_pps",
    )
    seed = col4.number_input(
        "Random seed",
        min_value=0,
        max_value=100000,
        value=int(defaults.seed),
        step=1,
        key=f"{prefix}_seed",
    )
    return WorkloadSpec(
        packets_per_class=int(packets_per_class),
        packet_size=int(packet_size),
        pps=float(pps),
        seed=int(seed),
    )


def scenario_selector(prefix: str, default_label: str) -> NetworkConditions:
    label = st.selectbox(
        "Network conditions",
        list(SCENARIOS.keys()),
        index=list(SCENARIOS.keys()).index(default_label),
        key=f"{prefix}_scenario",
        help="Applied identically to every configuration being compared.",
    )
    return SCENARIOS[label]


def current_priority_config() -> Dict[str, float]:
    """Traffic-class priorities currently selected in the QoS Lab."""
    return {
        traffic_class: float(
            st.session_state.get(f"prio_{traffic_class}", PRIORITIES[traffic_class])
        )
        for traffic_class in TRAFFIC_CLASSES
    }


def current_wfq_weights() -> Dict[str, float]:
    """WFQ service weights currently selected in the QoS Lab."""
    return {
        traffic_class: float(
            st.session_state.get(f"wfq_{traffic_class}", DEFAULT_WFQ_WEIGHTS[traffic_class])
        )
        for traffic_class in TRAFFIC_CLASSES
    }


def show_class_tables(class_frames: Dict[str, pd.DataFrame], expanded: bool = False):
    for label, frame in class_frames.items():
        with st.expander(f"Per-class QoS metrics — {label}", expanded=expanded):
            st.dataframe(frame.round(3), use_container_width=True, hide_index=True)


def download_frame(frame: pd.DataFrame, label: str, filename: str, key: str) -> None:
    """Offer a real DataFrame of experiment results as a CSV download."""
    st.download_button(
        f"⬇️ Download {label} (CSV)",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
        key=key,
        use_container_width=True,
    )


# ------------------------------------------------------------------
# Primary experience: live network workspace
# ------------------------------------------------------------------
st.markdown(
    """
    <style>
    .stApp { background: #080d14; color: #e6edf3; }
    section[data-testid="stSidebar"] { background: #0b111a; border-right: 1px solid #263448; }
    section[data-testid="stSidebar"] h3 { color: #67e8f9; letter-spacing: 0; }
    div[data-testid="stMetric"] { background: #0f1925; border: 1px solid #263448; padding: 10px 12px; }
    div[data-testid="stMetricLabel"] { color: #91a4ba; }
    div[data-testid="stMetricValue"] { color: #f8fafc; }
    div[data-testid="stMetricDelta"] { color: #67e8f9; }
    div[data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid #263448; }
    div[data-testid="stTabs"] [data-baseweb="tab"] { background: #0f1925; color: #9fb0c3; }
    div[data-testid="stTabs"] [aria-selected="true"] { color: #67e8f9; border-color: #22d3ee; }
    .stButton > button { min-height: 2.45rem; border: 1px solid #34465b; background: #101c2a; color: #dbeafe; }
    .stButton > button:hover { border-color: #22d3ee; color: #ffffff; }
    h1, h2, h3 { letter-spacing: 0; }
    </style>
    """,
    unsafe_allow_html=True,
)
with st.spinner("Loading network laboratory..."):
    render_workspace_controls(sim)
    st.markdown("## NetAdapt Network Simulator")
    st.caption(
        "LIVE WORKSPACE · packet animation, route selection, fault recovery and QoS state "
        "are driven by the Python simulation engine."
    )
    render_network_workspace(sim)

st.divider()
st.markdown("### Analysis & Experiments")
st.caption(
    "The original simulation controls and all analytical labs remain available below. "
    "The workspace above is the primary interactive view."
)

tab_network, tab_qos, tab_routing, tab_combined, tab_scenario = st.tabs(
    [
        "Legacy Traffic & Analytics",
        "QoS Lab",
        "Routing Lab",
        "Combined Lab",
        "Scenario Lab",
    ]
)

# The Scenario Lab body (Stage 6) is rendered directly after the tab bar is
# created; its implementation lives in ``scenario_lab.py``.
with tab_scenario:
    render_scenario_lab(
        bar_chart=bar_chart,
        grouped_chart=grouped_chart,
        topology_figure=get_topology_figure,
        workload_controls=workload_controls,
        download_frame=download_frame,
    )

# ==================================================================
# TAB 1 — Network & Traffic (Stage 1-3)
# ==================================================================
with tab_network:
    left, right = st.columns([1, 2])

    with left:
        st.markdown("### 🚦 Traffic Control")
        with st.container(border=True):
            nodes = sim.topology.nodes()
            source_options = nodes
            dest_options = nodes

            src = st.selectbox(
                "Source",
                source_options,
                index=source_options.index(st.session_state.selected_source)
                if st.session_state.selected_source in source_options
                else 0,
                key="src_select",
            )
            dst = st.selectbox(
                "Destination",
                dest_options,
                index=dest_options.index(st.session_state.selected_dest)
                if st.session_state.selected_dest in dest_options
                else 2,
                key="dst_select",
            )
            st.session_state.selected_source = src
            st.session_state.selected_dest = dst

            traffic_type = st.selectbox("Traffic Type", list(PRIORITIES.keys()), index=2)
            col_a, col_b = st.columns(2)
            packet_count = col_a.number_input(
                "Packet Count", min_value=1, max_value=1000, value=20, step=1
            )
            packet_size = col_b.number_input(
                "Packet Size (bytes)", min_value=64, max_value=9000, value=1000, step=100
            )

            col_c, col_d = st.columns(2)
            pps = col_c.number_input(
                "Packets/sec", min_value=1.0, max_value=100.0, value=10.0, step=1.0
            )
            duration = col_d.number_input(
                "Duration (s)", min_value=0.1, max_value=60.0, value=5.0, step=0.5
            )

            col_btn1, col_btn2 = st.columns(2)
            if col_btn1.button("Generate Traffic", type="primary", use_container_width=True):
                if src == dst:
                    st.error("Source and destination must differ.")
                else:
                    try:
                        flow = sim.create_flow(
                            src, dst, traffic_type, packet_count, packet_size, pps, duration
                        )
                        st.session_state.selected_flow_id = flow.flow_id
                        st.success(f"Flow {flow.flow_id} created: {src}→{dst} [{traffic_type}]")
                    except Exception as e:
                        st.error(f"Failed to create flow: {e}")

            if col_btn2.button("Run Simulation", use_container_width=True):
                if len(sim.scheduler) == 0 and len(sim.active_flows) == 0:
                    st.warning("No traffic to simulate. Generate traffic first.")
                else:
                    processed = sim.run_until_empty()
                    st.success(f"Processed {len(processed)} packets. Time: {sim.time:.2f}s")
                    st.rerun()

            col_btn3, col_btn4 = st.columns(2)
            if col_btn3.button("Tick +1s", use_container_width=True):
                sim.tick(1.0)
                st.rerun()
            if col_btn4.button("Reset Simulation", use_container_width=True):
                sim.reset()
                st.session_state.selected_flow_id = None
                st.success("Simulation reset")
                st.rerun()

        st.markdown("### 💥 Fault Injection")
        with st.container(border=True):
            links = sim.topology.links()
            link_options = [f"{u}-{v}" for u, v in links]
            node_options = sim.topology.nodes()

            selected_link = st.selectbox("Select Link", link_options, key="link_select")
            col_f1, col_f2 = st.columns(2)
            if col_f1.button("Fail Link", use_container_width=True):
                try:
                    u, v = selected_link.split("-")
                    sim.fail_link(u, v)
                    sim.tick(sim.failure_detection_timeout + 0.5)
                    st.warning(
                        f"Link {selected_link} failed. Detection after {sim.failure_detection_timeout}s"
                    )
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if col_f2.button("Recover Link", use_container_width=True):
                try:
                    u, v = selected_link.split("-")
                    sim.recover_link(u, v)
                    st.success(f"Link {selected_link} recovered")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            selected_node = st.selectbox("Select Node", node_options, key="node_select")
            col_n1, col_n2 = st.columns(2)
            if col_n1.button("Fail Node", use_container_width=True):
                try:
                    if selected_node in ("H1", "H2", "H3", "H4"):
                        st.warning(
                            "Failing a host will isolate it. Prefer router failures for rerouting demo."
                        )
                    sim.fail_node(selected_node)
                    sim.tick(sim.failure_detection_timeout + 0.5)
                    st.warning(f"Node {selected_node} failed")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if col_n2.button("Recover Node", use_container_width=True):
                try:
                    sim.recover_node(selected_node)
                    st.success(f"Node {selected_node} recovered")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            st.divider()
            st.markdown("**Condition Injection**")
            col_c1, col_c2 = st.columns(2)
            congestion_val = col_c1.slider("Congestion", 0.0, 1.0, 0.0, 0.1, key="cong_slider")
            loss_val = col_c2.slider("Packet Loss", 0.0, 1.0, 0.0, 0.05, key="loss_slider")
            bw_val = st.slider("Bandwidth (Mbps)", 1.0, 100.0, 100.0, 5.0, key="bw_slider")

            col_cc1, col_cc2, col_cc3 = st.columns(3)
            if col_cc1.button("Set Congestion", use_container_width=True):
                try:
                    u, v = selected_link.split("-")
                    sim.set_congestion(u, v, congestion_val)
                    st.info(f"Congestion on {selected_link} set to {congestion_val:.0%}")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if col_cc2.button("Set Loss", use_container_width=True):
                try:
                    u, v = selected_link.split("-")
                    sim.set_packet_loss(u, v, loss_val)
                    st.info(f"Loss on {selected_link} set to {loss_val:.0%}")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if col_cc3.button("Set BW", use_container_width=True):
                try:
                    u, v = selected_link.split("-")
                    sim.set_bandwidth(u, v, bw_val)
                    st.info(f"Bandwidth on {selected_link} set to {bw_val} Mbps")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.button("Reset Conditions", use_container_width=True):
                for u, v in sim.topology.links():
                    sim.topology.update_link(u, v, congestion=0.0, packet_loss=0.0)
                    orig_bw = sim.topology.get_original_bandwidth(u, v)
                    sim.topology.update_link(u, v, bandwidth=orig_bw)
                st.success("Conditions reset")
                st.rerun()

        st.markdown("### 🛣️ Active Route")
        with st.container(border=True):
            try:
                route, cost, status = sim.get_current_route(
                    st.session_state.selected_source, st.session_state.selected_dest
                )
                st.markdown(f"**Source:** {st.session_state.selected_source}")
                st.markdown(f"**Destination:** {st.session_state.selected_dest}")
                st.markdown(f"**Route:** {format_route(route)}")
                st.markdown(f"**Cost:** {cost:.2f}")
                if "FAILED" in status:
                    st.error(f"Status: {status}")
                elif status == "REROUTED":
                    st.warning(f"Status: {status}")
                else:
                    st.success(f"Status: {status}")

                if (
                    st.session_state.selected_flow_id
                    and st.session_state.selected_flow_id in sim.active_flows
                ):
                    flow = sim.active_flows[st.session_state.selected_flow_id]
                    st.divider()
                    st.markdown(f"**Selected Flow:** {flow.flow_id}")
                    st.markdown(f"Type: {flow.traffic_type} | Size: {flow.packet_size}B")
                    st.markdown(
                        f"Sent: {flow.packets_sent} | Delivered: {flow.packets_delivered} | "
                        f"Dropped: {flow.packets_dropped}"
                    )
                    st.markdown(f"Route Status: {flow.route_status}")
                    st.markdown(f"Current Route: {format_route(flow.current_route)}")
                    if flow.previous_route:
                        st.markdown(f"Previous Route: {format_route(flow.previous_route)}")

            except Exception as e:
                st.error(f"Route error: {e}")

        st.markdown("### 📋 Active Flows")
        with st.container(border=True):
            if sim.active_flows:
                flows_data = []
                for f in sim.active_flows.values():
                    flows_data.append(
                        {
                            "Flow ID": f.flow_id,
                            "Source": f.source,
                            "Dest": f.destination,
                            "Type": f.traffic_type,
                            "Status": f.status,
                            "Route Status": f.route_status,
                            "Sent": f.packets_sent,
                            "Delivered": f.packets_delivered,
                            "Dropped": f.packets_dropped,
                            "Route": format_route(f.current_route),
                        }
                    )
                st.dataframe(pd.DataFrame(flows_data), use_container_width=True, hide_index=True)

                flow_ids = list(sim.active_flows.keys())
                selected = st.selectbox("Highlight Flow Route", ["None"] + flow_ids, key="flow_highlight")
                if selected != "None":
                    st.session_state.selected_flow_id = selected
            else:
                st.info("No active flows. Generate traffic to create flows.")

        st.markdown("### 📦 Live Queue Statistics")
        with st.container(border=True):
            live_queue = sim.scheduler_statistics()
            q1, q2, q3 = st.columns(3)
            q1.metric("Scheduler", live_queue["scheduler"])
            q1.metric("Packets Served", int(live_queue["packets_served"]))
            q2.metric("Current Queue Length", int(live_queue["current_queue_length"]))
            q2.metric("Max Queue Length", int(live_queue["max_queue_length"]))
            q3.metric("Avg Waiting Time", f"{live_queue['average_waiting_time']*1000:.2f} ms")
            q3.metric("Max Waiting Time", f"{live_queue['max_waiting_time']*1000:.2f} ms")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Traffic Class": cls, **values}
                        for cls, values in sim.scheduler_class_statistics().items()
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    with right:
        highlight_route = None
        if (
            st.session_state.selected_flow_id
            and st.session_state.selected_flow_id in sim.active_flows
        ):
            highlight_route = sim.active_flows[st.session_state.selected_flow_id].current_route
        else:
            try:
                r, _, _ = sim.get_current_route(
                    st.session_state.selected_source, st.session_state.selected_dest
                )
                highlight_route = r
            except Exception:
                highlight_route = None

        st.markdown("### 🗺️ Network Topology")
        st.plotly_chart(get_topology_figure(sim, highlight_route), use_container_width=True)

        with st.expander("Link Conditions Details", expanded=False):
            link_data = []
            for u, v, data in sim.topology.graph.edges(data=True):
                link_data.append(
                    {
                        "Link": f"{u}↔{v}",
                        "Status": data.get("status", "UP"),
                        "Latency (ms)": data.get("latency", 0),
                        "Bandwidth (Mbps)": data.get("bandwidth", 0),
                        "Loss": f"{data.get('packet_loss', 0):.2%}",
                        "Congestion": f"{data.get('congestion', 0):.2%}",
                        "Failed": "Yes" if tuple(sorted((u, v))) in sim._failed_links else "No",
                    }
                )
            st.dataframe(pd.DataFrame(link_data), use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### 📈 Performance Graphs")

        if sim.metrics.history:
            df_hist = sim.metrics.dataframe()

            fig_lat = go.Figure()
            fig_lat.add_trace(
                go.Scatter(
                    x=df_hist["time"],
                    y=df_hist["average_latency"] * 1000,
                    mode="lines+markers",
                    name="Latency (ms)",
                    line=dict(color="#636EFA"),
                )
            )
            fig_lat.update_layout(
                title="Latency Over Time",
                xaxis_title="Simulation Time (s)",
                yaxis_title="Latency (ms)",
                height=300,
                margin=dict(l=20, r=20, t=40, b=20),
            )
            st.plotly_chart(fig_lat, use_container_width=True)

            col_g1, col_g2 = st.columns(2)
            with col_g1:
                fig_thr = go.Figure()
                fig_thr.add_trace(
                    go.Scatter(
                        x=df_hist["time"],
                        y=df_hist["throughput"],
                        mode="lines+markers",
                        name="Throughput",
                        line=dict(color="#00CC96"),
                    )
                )
                fig_thr.update_layout(
                    title="Throughput Over Time",
                    xaxis_title="Time (s)",
                    yaxis_title="Throughput (B/s)",
                    height=300,
                    margin=dict(l=20, r=20, t=40, b=20),
                )
                st.plotly_chart(fig_thr, use_container_width=True)

                fig_loss = go.Figure()
                fig_loss.add_trace(
                    go.Scatter(
                        x=df_hist["time"],
                        y=df_hist["packet_loss"],
                        mode="lines+markers",
                        name="Packet Loss %",
                        line=dict(color="#EF553B"),
                    )
                )
                fig_loss.update_layout(
                    title="Packet Loss Over Time",
                    xaxis_title="Simulation Time (s)",
                    yaxis_title="Loss (%)",
                    height=300,
                    margin=dict(l=20, r=20, t=40, b=20),
                )
                st.plotly_chart(fig_loss, use_container_width=True)

            with col_g2:
                fig_pdr = go.Figure()
                fig_pdr.add_trace(
                    go.Scatter(
                        x=df_hist["time"],
                        y=df_hist["packet_delivery_ratio"],
                        mode="lines+markers",
                        name="PDR",
                        line=dict(color="#AB63FA"),
                    )
                )
                fig_pdr.update_layout(
                    title="Packet Delivery Ratio Over Time",
                    xaxis_title="Simulation Time (s)",
                    yaxis_title="PDR (%)",
                    height=300,
                    margin=dict(l=20, r=20, t=40, b=20),
                )
                st.plotly_chart(fig_pdr, use_container_width=True)

                fig_cong = go.Figure()
                fig_cong.add_trace(
                    go.Scatter(
                        x=df_hist["time"],
                        y=df_hist["congestion"] * 100,
                        mode="lines+markers",
                        name="Congestion %",
                        line=dict(color="#FFA15A"),
                    )
                )
                fig_cong.update_layout(
                    title="Congestion Over Time",
                    xaxis_title="Simulation Time (s)",
                    yaxis_title="Congestion (%)",
                    height=300,
                    margin=dict(l=20, r=20, t=40, b=20),
                )
                st.plotly_chart(fig_cong, use_container_width=True)

            st.markdown("#### Per-Class QoS (live simulation)")
            st.dataframe(sim.class_dataframe().round(3), use_container_width=True, hide_index=True)

            st.markdown("#### Before / During / After Comparison")
            comparison = sim.metrics.get_comparison()
            if comparison.get("before") or comparison.get("during") or comparison.get("after"):
                comp_data = []
                for phase in ["before", "during", "after"]:
                    if comparison.get(phase):
                        row = {"Phase": phase.capitalize()}
                        row.update(comparison[phase])
                        comp_data.append(row)
                if comp_data:
                    st.dataframe(
                        pd.DataFrame(comp_data), use_container_width=True, hide_index=True
                    )
                    if len(comp_data) >= 2:
                        fig_comp = go.Figure()
                        for metric in [
                            "average_latency",
                            "throughput",
                            "packet_loss",
                            "packet_delivery_ratio",
                        ]:
                            fig_comp.add_trace(
                                go.Bar(
                                    name=metric,
                                    x=[c["Phase"] for c in comp_data],
                                    y=[c.get(metric, 0) for c in comp_data],
                                )
                            )
                        fig_comp.update_layout(
                            title="Performance Comparison", barmode="group", height=350
                        )
                        st.plotly_chart(fig_comp, use_container_width=True)
            else:
                st.info(
                    "Run experiments with failures to see before/during/after comparison. "
                    "Metrics history will be used."
                )
        else:
            st.info("No metrics history yet. Generate traffic and run simulation to see graphs.")

        st.divider()
        st.markdown("### 📜 Event Log")
        events = sim.get_event_log(limit=100)
        if events:
            df_events = pd.DataFrame(events)
            if "time" in df_events.columns:
                df_events = df_events.sort_values("time", ascending=False)
            st.dataframe(df_events, use_container_width=True, hide_index=True)

            with st.expander("Detailed Structured Events", expanded=False):
                structured = sim.get_structured_events(limit=50)
                for evt in reversed(structured[-20:]):
                    st.markdown(
                        f"`{evt.timestamp:.2f}s` **{evt.event_type}** — {evt.message} "
                        f"{f'[{evt.component}]' if evt.component else ''} "
                        f"{f'Flow:{evt.flow_id}' if evt.flow_id else ''}"
                    )
        else:
            st.info("No events yet.")

# ==================================================================
# TAB 2 — QoS Lab (Stage 4)
# ==================================================================
with tab_qos:
    st.markdown("## 🧪 QoS Laboratory")
    st.markdown(
        "Compare **FIFO**, **Priority Queue** and **WFQ** on the *identical* workload and "
        "network conditions. Every configuration is simulated separately by the same engine, "
        "then the actual results are compared."
    )

    qos_left, qos_right = st.columns([1, 2])

    with qos_left:
        st.markdown("### ⚙️ Experiment configuration")
        with st.container(border=True):
            qos_spec = workload_controls(
                "qos",
                WorkloadSpec(
                    packets_per_class=20, packet_size=1200, pps=50.0, seed=42
                ),
            )
            qos_conditions = scenario_selector(
                "qos", "Congested core (60% congestion, 10 Mbps link)"
            )
            st.caption(
                "Traffic classes: " + ", ".join(TRAFFIC_CLASSES) + " (all compete at once)"
            )
            selected_schedulers = st.multiselect(
                "Schedulers to compare",
                options=list(QOS_SCHEDULERS),
                default=list(QOS_SCHEDULERS),
                format_func=lambda key: SCHEDULER_LABELS.get(key, key),
                key="qos_schedulers",
            )
            st.caption("Routing algorithm used by all QoS runs (kept constant):")
            qos_algorithm = st.selectbox(
                "Routing algorithm",
                options=list(ALGORITHM_LABELS.keys()),
                format_func=lambda key: ALGORITHM_LABELS[key],
                key="qos_algorithm",
            )

            run_qos = st.button(
                "▶️ Run QoS comparison (same workload)",
                type="primary",
                use_container_width=True,
            )
            run_stress = st.button(
                "🔥 Run congestion stress test", use_container_width=True
            )

        st.markdown("### 🎛️ Traffic-class configuration")
        with st.container(border=True):
            st.markdown("*Priority levels (used by Priority Queue)*")
            priority_cols = st.columns(len(TRAFFIC_CLASSES))
            configured_priorities: Dict[str, float] = {}
            for column, traffic_class in zip(priority_cols, TRAFFIC_CLASSES):
                configured_priorities[traffic_class] = column.number_input(
                    traffic_class,
                    min_value=0.0,
                    max_value=10.0,
                    value=float(PRIORITIES[traffic_class]),
                    step=1.0,
                    key=f"prio_{traffic_class}",
                )
            if st.button("Apply priorities", use_container_width=True):
                resolved = sim.set_priority_config(configured_priorities)
                st.success(
                    "Priorities applied (live simulator + experiments): "
                    + ", ".join(f"{k}={v:g}" for k, v in resolved.items())
                )

            st.divider()
            st.markdown("*WFQ service weights*")
            weight_cols = st.columns(len(TRAFFIC_CLASSES))
            configured_weights: Dict[str, float] = {}
            for column, traffic_class in zip(weight_cols, TRAFFIC_CLASSES):
                configured_weights[traffic_class] = column.number_input(
                    f"{traffic_class} ",
                    min_value=0.1,
                    max_value=20.0,
                    value=float(DEFAULT_WFQ_WEIGHTS[traffic_class]),
                    step=0.5,
                    key=f"wfq_{traffic_class}",
                )
            if st.button("Apply WFQ weights", use_container_width=True):
                resolved = sim.set_wfq_weights(configured_weights)
                st.success(
                    "WFQ weights applied: "
                    + ", ".join(f"{k}={v:g}" for k, v in resolved.items())
                )

            st.divider()
            st.markdown("*Live scheduler for the simulation above*")
            live_scheduler_choice = st.selectbox(
                "Live QoS scheduler",
                options=list(QOS_SCHEDULERS),
                format_func=lambda key: SCHEDULER_LABELS.get(key, key),
                index=list(QOS_SCHEDULERS).index(sim.scheduler_name),
                key="live_scheduler_choice",
            )
            if st.button("Apply scheduler to live simulator", use_container_width=True):
                sim.set_scheduler(live_scheduler_choice)
                st.success(
                    "Live scheduler set to "
                    f"{SCHEDULER_LABELS.get(sim.scheduler_name, sim.scheduler_name)}. "
                    "It now schedules the packets generated in Network & Traffic."
                )

    if run_qos:
        if not selected_schedulers:
            st.error("Select at least one scheduler.")
        else:
            with st.spinner("Simulating identical workload for each scheduler..."):
                st.session_state.qos_result = run_qos_experiment(
                    spec=qos_spec,
                    conditions=qos_conditions,
                    schedulers=selected_schedulers,
                    algorithm=qos_algorithm,
                    wfq_weights=current_wfq_weights(),
                    priorities=current_priority_config(),
                )

    if run_stress:
        with st.spinner("Running congestion stress test..."):
            st.session_state.qos_stress_result = run_qos_stress_test(
                spec=WorkloadSpec(
                    packets_per_class=max(qos_spec.packets_per_class, 20),
                    packet_size=qos_spec.packet_size,
                    pps=max(qos_spec.pps, 100.0),
                    seed=qos_spec.seed,
                ),
                schedulers=selected_schedulers or list(QOS_SCHEDULERS),
                algorithm=qos_algorithm,
                wfq_weights=current_wfq_weights(),
                priorities=current_priority_config(),
            )

    with qos_right:
        for session_key, heading in (
            ("qos_result", "### 📊 QoS comparison — FIFO vs Priority vs WFQ"),
            ("qos_stress_result", "### 🔥 Congestion stress test results"),
        ):
            result = st.session_state.get(session_key)
            if result is None:
                continue

            st.markdown(heading)
            st.caption(
                f"{result.kind} · workload: {result.spec.total_packets} packets "
                f"({result.spec.packets_per_class} per class, {result.spec.packet_size} B, "
                f"{result.spec.pps:.0f} pps) · seed {result.spec.seed} · "
                f"conditions: {result.conditions.label}"
            )

            summary_frame = result.summary
            st.dataframe(summary_frame.round(3), use_container_width=True, hide_index=True)

            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.plotly_chart(
                    bar_chart(
                        list(summary_frame["Scheduler"]),
                        list(summary_frame["Avg Latency (ms)"]),
                        "Overall average latency",
                        "ms",
                        color="#636EFA",
                    ),
                    use_container_width=True,
                )
                st.plotly_chart(
                    bar_chart(
                        list(summary_frame["Scheduler"]),
                        list(summary_frame["Throughput (B/s)"]),
                        "Overall throughput",
                        "B/s",
                        color="#00CC96",
                        text_format="%.0f",
                    ),
                    use_container_width=True,
                )
            with chart_col2:
                st.plotly_chart(
                    bar_chart(
                        list(summary_frame["Scheduler"]),
                        list(summary_frame["Avg Queue Wait (ms)"]),
                        "Average queue waiting time",
                        "ms",
                        color="#FFA15A",
                    ),
                    use_container_width=True,
                )
                st.plotly_chart(
                    bar_chart(
                        list(summary_frame["Scheduler"]),
                        list(summary_frame["Jitter (ms)"]),
                        "Overall jitter (packet delay variation)",
                        "ms",
                        color="#AB63FA",
                    ),
                    use_container_width=True,
                )

            st.markdown("#### 📦 Queue statistics per scheduler")
            st.dataframe(
                result.queue_statistics.round(3), use_container_width=True, hide_index=True
            )

            st.markdown("#### 🧮 Class-wise comparison")
            class_rows = []
            for label, frame in result.class_metrics.items():
                for _, row in frame.iterrows():
                    class_rows.append({"Scheduler": label, **row.to_dict()})
            combined_class_frame = pd.DataFrame(class_rows)

            class_metric_options = [
                "Avg Latency (ms)",
                "Throughput (B/s)",
                "Packet Loss (%)",
                "PDR (%)",
                "Jitter (ms)",
                "Avg Queue Wait (ms)",
                "Max Queue Wait (ms)",
            ]
            metric_choice = st.selectbox(
                "Class-wise metric",
                class_metric_options,
                index=0,
                key=f"qos_class_metric_{session_key}",
            )
            st.plotly_chart(
                grouped_chart(
                    combined_class_frame,
                    category_column="Traffic Class",
                    value_column=metric_choice,
                    group_column="Scheduler",
                    title=f"{metric_choice} per traffic class and scheduler",
                    y_title=metric_choice,
                ),
                use_container_width=True,
            )

            with st.expander("All per-class metric tables", expanded=False):
                st.dataframe(
                    combined_class_frame.round(3), use_container_width=True, hide_index=True
                )

            show_class_tables(result.class_metrics)

            st.markdown("#### 🗂️ Per-class queue occupancy")
            for label, frame in result.queue_class_statistics.items():
                with st.expander(f"Queue per class — {label}"):
                    st.dataframe(frame, use_container_width=True, hide_index=True)

            st.divider()

        if st.session_state.get("qos_result") is None and st.session_state.get(
            "qos_stress_result"
        ) is None:
            st.info(
                "Configure the workload and press **Run QoS comparison** to simulate the same "
                "traffic through FIFO, Priority Queue and WFQ."
            )

# ==================================================================
# TAB 3 — Routing Lab (Stage 5)
# ==================================================================
with tab_routing:
    st.markdown("## 🧭 Routing Laboratory")
    st.markdown(
        "Compare **Dijkstra** and **Bellman-Ford**, and change the route-cost weights "
        "(latency, packet loss, congestion, bandwidth, hop count) to see the *actual* route "
        "selection change."
    )

    route_left, route_right = st.columns([1, 2])

    with route_left:
        st.markdown("### ⚙️ Routing configuration")
        with st.container(border=True):
            routing_spec = workload_controls(
                "routing", WorkloadSpec(packets_per_class=5, packet_size=1200, pps=50.0)
            )
            routing_conditions = scenario_selector(
                "routing", "Degraded core (60% congestion, 35% loss)"
            )

            st.markdown("**Route-cost weights**")
            weight_inputs: Dict[str, float] = {}
            weight_cols = st.columns(len(ROUTING_WEIGHT_NAMES))
            preset_name = st.selectbox(
                "Preset", ["Custom"] + list(ROUTING_WEIGHT_PRESETS.keys()), key="weight_preset"
            )
            preset_weights = (
                ROUTING_WEIGHT_PRESETS[preset_name]
                if preset_name != "Custom"
                else DEFAULT_ROUTING_WEIGHTS
            )
            for column, weight_name in zip(weight_cols, ROUTING_WEIGHT_NAMES):
                weight_inputs[weight_name] = column.number_input(
                    weight_name,
                    min_value=0.0,
                    max_value=1000.0,
                    value=float(preset_weights[weight_name]),
                    step=1.0,
                    key=f"weight_{weight_name}",
                )

            if st.button("Apply weights to live router", use_container_width=True):
                resolved = sim.set_routing_weights(**weight_inputs)
                st.success(
                    "Weights applied: " + ", ".join(f"{k}={v:g}" for k, v in resolved.items())
                )

            st.divider()
            live_algorithm_choice = st.selectbox(
                "Live routing algorithm",
                options=list(ALGORITHM_LABELS.keys()),
                format_func=lambda key: ALGORITHM_LABELS[key],
                index=list(ALGORITHM_LABELS.keys()).index(sim.get_router_algorithm()),
                key="live_algorithm_choice",
            )
            if st.button("Apply algorithm to live simulator", use_container_width=True):
                sim.set_router_algorithm(live_algorithm_choice)
                st.success(
                    f"Live router now uses {ALGORITHM_LABELS[sim.get_router_algorithm()]} "
                    "for route recalculation, failure recovery and rerouting."
                )

            st.divider()
            run_routing = st.button(
                "▶️ Run Dijkstra vs Bellman-Ford", type="primary", use_container_width=True
            )
            run_weights = st.button(
                "🧮 Run routing weight experiment", use_container_width=True
            )

    if run_routing:
        with st.spinner("Simulating the same workload with Dijkstra and Bellman-Ford..."):
            st.session_state.routing_result = run_routing_comparison(
                spec=routing_spec,
                conditions=routing_conditions,
                routing_weights=weight_inputs,
            )

    if run_weights:
        with st.spinner("Evaluating routing weight configurations..."):
            st.session_state.weight_result = run_routing_weight_experiment(
                spec=routing_spec,
                conditions=routing_conditions,
                algorithm="dijkstra",
                presets=None if preset_name == "Custom" else ROUTING_WEIGHT_PRESETS,
            )

    with route_right:
        routing_result = st.session_state.get("routing_result")
        if routing_result is not None:
            st.markdown("### 📊 Dijkstra vs Bellman-Ford")
            st.caption(
                f"workload: {routing_result.spec.total_packets} packets · seed "
                f"{routing_result.spec.seed} · conditions: {routing_result.conditions.label} · "
                f"weights: "
                + ", ".join(f"{k}={v:g}" for k, v in routing_result.weights.items())
            )
            st.dataframe(
                routing_result.summary.round(3), use_container_width=True, hide_index=True
            )

            if routing_result.routes_agree:
                st.success(
                    "Both algorithms selected the same optimal route and identical route cost — "
                    "an independent cross-check that both implementations are correct."
                )
            else:
                st.warning(
                    "The algorithms selected different routes (equal-cost alternatives). "
                    "Route costs are reported above for comparison."
                )

            comparison_cols = st.columns(2)
            with comparison_cols[0]:
                st.plotly_chart(
                    bar_chart(
                        list(routing_result.summary["Algorithm"]),
                        list(routing_result.summary["Route Cost"]),
                        "Route cost",
                        "cost",
                        color="#636EFA",
                    ),
                    use_container_width=True,
                )
            with comparison_cols[1]:
                st.plotly_chart(
                    bar_chart(
                        list(routing_result.summary["Algorithm"]),
                        list(routing_result.summary["Hops"]),
                        "Hop count",
                        "hops",
                        color="#00CC96",
                        text_format="%.0f",
                    ),
                    use_container_width=True,
                )

            st.markdown("#### 🗺️ Selected route on the topology")
            first_run = next(iter(routing_result.runs.values()), None)
            if first_run is not None:
                route_path = list(routing_result.summary["Route"].iloc[0].split(" → "))
                st.plotly_chart(
                    get_topology_figure(first_run, route_path), use_container_width=True
                )

            with st.expander("Per-class metrics of the routing comparison"):
                for label, frame in routing_result.runs.items():
                    st.markdown(f"**{label}**")
                    st.dataframe(
                        frame.class_dataframe().round(3),
                        use_container_width=True,
                        hide_index=True,
                    )
            st.divider()

        weight_result = st.session_state.get("weight_result")
        if weight_result is not None:
            st.markdown("### 🧮 Routing weight sensitivity")
            st.caption(
                f"Identical topology and conditions for every weight configuration "
                f"({weight_result.conditions.label}). "
                f"{len(weight_result.distinct_routes)} distinct route(s) selected."
            )
            st.dataframe(
                weight_result.summary.round(3), use_container_width=True, hide_index=True
            )

            st.plotly_chart(
                bar_chart(
                    list(weight_result.summary["Weight Config"]),
                    list(weight_result.summary["Route Cost"]),
                    "Route cost per weight configuration",
                    "cost",
                    color="#AB63FA",
                ),
                use_container_width=True,
            )
            st.plotly_chart(
                bar_chart(
                    list(weight_result.summary["Weight Config"]),
                    list(weight_result.summary["Hops"]),
                    "Hop count per weight configuration",
                    "hops",
                    color="#FFA15A",
                    text_format="%.0f",
                ),
                use_container_width=True,
            )
            st.dataframe(
                weight_result.summary[["Weight Config", "Route", "Route Cost", "Hops"]],
                use_container_width=True,
                hide_index=True,
            )
            st.divider()

        if routing_result is None and weight_result is None:
            st.info(
                "Press **Run Dijkstra vs Bellman-Ford** or **Run routing weight experiment** "
                "to produce routing results from the simulation engine."
            )

# ==================================================================
# TAB 4 — Combined Lab (Stage 4 + Stage 5)
# ==================================================================
with tab_combined:
    st.markdown("## 🔬 Combined Laboratory — Routing × QoS")
    st.markdown(
        "One controlled experiment over all six combinations of routing algorithm and QoS "
        "scheduler, using the **same topology, same workload, same conditions and same seed**."
    )

    combined_left, combined_right = st.columns([1, 2])

    with combined_left:
        st.markdown("### ⚙️ Experiment configuration")
        with st.container(border=True):
            combined_spec = workload_controls(
                "combined",
                WorkloadSpec(packets_per_class=10, packet_size=1200, pps=50.0, seed=42),
            )
            combined_conditions = scenario_selector(
                "combined", "Congested core (60% congestion, 10 Mbps link)"
            )
            st.markdown(
                """
                **Configurations (always all six):**
                1. Dijkstra + FIFO
                2. Dijkstra + Priority Queue
                3. Dijkstra + WFQ
                4. Bellman-Ford + FIFO
                5. Bellman-Ford + Priority Queue
                6. Bellman-Ford + WFQ
                """
            )
            run_combined = st.button(
                "▶️ Run six-way comparison", type="primary", use_container_width=True
            )

    if run_combined:
        with st.spinner("Running all six routing × QoS configurations..."):
            st.session_state.combined_result = run_combined_experiment(
                spec=combined_spec,
                conditions=combined_conditions,
                wfq_weights=current_wfq_weights(),
                priorities=current_priority_config(),
            )

    with combined_right:
        combined_result = st.session_state.get("combined_result")
        if combined_result is None:
            st.info(
                "Configure the workload and press **Run six-way comparison** to simulate all six "
                "routing × QoS combinations."
            )
        else:
            st.markdown("### 📊 Six-way comparison")
            st.caption(
                f"workload: {combined_result.spec.total_packets} packets · "
                f"seed {combined_result.spec.seed} · conditions: "
                f"{combined_result.conditions.label}"
            )

            summary_frame = combined_result.summary
            st.dataframe(summary_frame.round(3), use_container_width=True, hide_index=True)

            metric_groups = [
                ("Avg Latency (ms)", "Latency (ms)", "#636EFA", "%.2f"),
                ("Throughput (B/s)", "Throughput (B/s)", "#00CC96", "%.0f"),
                ("Packet Loss (%)", "Packet loss (%)", "#EF553B", "%.2f"),
                ("PDR (%)", "Packet delivery ratio (%)", "#AB63FA", "%.2f"),
                ("Jitter (ms)", "Jitter (ms)", "#FFA15A", "%.2f"),
                ("Route Cost", "Route cost", "#19D3F3", "%.2f"),
            ]

            for row_start in range(0, len(metric_groups), 2):
                chart_cols = st.columns(2)
                for column, (column_name, title, color, text_format) in zip(
                    chart_cols, metric_groups[row_start : row_start + 2]
                ):
                    with column:
                        st.plotly_chart(
                            bar_chart(
                                list(summary_frame["Configuration"]),
                                list(summary_frame[column_name]),
                                title,
                                title,
                                color=color,
                                text_format=text_format,
                            ),
                            use_container_width=True,
                        )

            st.markdown("#### 🗺️ Route, cost and hop count")
            st.dataframe(
                summary_frame[["Configuration", "Route", "Route Cost", "Hops"]],
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("#### 🧮 Class-wise comparison across the six configurations")
            combined_class_rows = []
            for label, frame in combined_result.class_metrics.items():
                for _, row in frame.iterrows():
                    combined_class_rows.append({"Configuration": label, **row.to_dict()})
            combined_class_frame = pd.DataFrame(combined_class_rows)

            combined_metric = st.selectbox(
                "Class-wise metric",
                [
                    "Avg Latency (ms)",
                    "Throughput (B/s)",
                    "Packet Loss (%)",
                    "PDR (%)",
                    "Jitter (ms)",
                    "Avg Queue Wait (ms)",
                ],
                key="combined_class_metric",
            )
            class_choice = st.selectbox(
                "Traffic class",
                options=["All classes"] + TRAFFIC_CLASSES,
                key="combined_class_choice",
            )
            filtered = (
                combined_class_frame
                if class_choice == "All classes"
                else combined_class_frame[
                    combined_class_frame["Traffic Class"] == class_choice
                ]
            )
            st.plotly_chart(
                grouped_chart(
                    filtered,
                    category_column="Configuration",
                    value_column=combined_metric,
                    group_column="Traffic Class",
                    title=f"{combined_metric} per traffic class",
                    y_title=combined_metric,
                    height=440,
                ),
                use_container_width=True,
            )

            st.markdown("#### 📦 Queue statistics per configuration")
            st.dataframe(
                combined_result.queue_statistics.round(3),
                use_container_width=True,
                hide_index=True,
            )

            with st.expander("All per-class metric tables", expanded=False):
                st.dataframe(
                    combined_class_frame.round(3), use_container_width=True, hide_index=True
                )

# ------------------------------------------------------------------
# Footer
# ------------------------------------------------------------------
st.divider()
st.markdown(
    """
    **Demonstration scenarios**

    *Network & Traffic* — generate traffic, inject congestion/loss/failures, watch detection,
    rerouting and recovery metrics.

    *QoS Lab* — run the same workload through FIFO, Priority Queue and WFQ and compare per-class
    latency, throughput, loss, PDR, jitter and queue waiting time.

    *Routing Lab* — compare Dijkstra with Bellman-Ford and change the route-cost weights
    (latency / loss / congestion / bandwidth / hop) to change the selected route.

    *Combined Lab* — six routing × QoS configurations on identical topology, workload, conditions
    and seed.
    """
)

st.caption(
    "NetAdapt — Simulation engine is the source of truth. All metrics come from real packet "
    "simulation. No fake or hardcoded data."
)
