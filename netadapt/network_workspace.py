"""Interactive network workspace for NetAdapt.

This module is a frontend layer only. Packet movement is driven by real
``NetworkSimulator.process_next_packet()`` calls, and every route, timing,
delivery, drop, topology, queue and event value in the workspace is read back
from the simulation engine after each step.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from events import EventType
from qos import SCHEDULER_LABELS, TRAFFIC_CLASSES
from routing import ALGORITHM_LABELS, DEFAULT_ROUTING_WEIGHTS, ROUTING_WEIGHT_NAMES
from simulator import NetworkSimulator


TRAFFIC_COLORS: Dict[str, str] = {
    "Emergency": "#ff4d6d",
    "VoIP": "#22d3ee",
    "Video": "#a78bfa",
    "HTTP": "#38bdf8",
    "FTP": "#f59e0b",
}

EVENT_LABELS: Dict[str, str] = {
    EventType.PACKET_GENERATED: "Packet generated",
    EventType.PACKET_DELIVERED: "Packet delivered",
    EventType.PACKET_DROPPED: "Packet dropped",
    EventType.LINK_FAILED: "Link failed",
    EventType.NODE_FAILED: "Node failed",
    EventType.FAILURE_DETECTED: "Failure detected",
    EventType.ROUTE_RECALCULATED: "Route recalculated",
    EventType.TRAFFIC_REROUTED: "Traffic rerouted",
    EventType.LINK_RECOVERED: "Link recovered",
    EventType.NODE_RECOVERED: "Node recovered",
    EventType.CONGESTION_CHANGED: "Congestion changed",
    EventType.PACKET_LOSS_CHANGED: "Packet loss changed",
    EventType.BANDWIDTH_CHANGED: "Bandwidth changed",
    EventType.SCHEDULER_CHANGED: "Scheduler changed",
    EventType.ROUTING_ALGORITHM_CHANGED: "Routing algorithm changed",
    EventType.ROUTING_WEIGHTS_CHANGED: "Routing weights changed",
}


def node_label(node: str) -> str:
    """Human-friendly endpoint label for the Streamlit controls."""
    return f"PC{node[1:]}" if node.startswith("H") else node


def link_label(u: str, v: str) -> str:
    return f"{node_label(u)} ↔ {node_label(v)}"


def _flow_for_packet(sim: NetworkSimulator, packet_id: int):
    for flow in sim.active_flows.values():
        if packet_id in flow.packet_ids:
            return flow
    return None


def _packet_route(sim: NetworkSimulator, packet: Any) -> List[str]:
    if packet.route:
        return list(packet.route)
    flow = _flow_for_packet(sim, packet.packet_id)
    return list(flow.current_route) if flow else []


def _packet_view(
    sim: NetworkSimulator,
    packet: Any,
    *,
    animation_seconds: float = 1.6,
) -> Dict[str, Any]:
    """Serialize one real Packet into the frontend's stable view model."""
    flow = _flow_for_packet(sim, packet.packet_id)
    route = _packet_route(sim, packet)
    status = str(packet.delivery_status or "PENDING")
    if status == "PENDING" and len(sim.scheduler):
        status = "QUEUED"

    if status == "DELIVERED":
        current_node = packet.destination
    elif status == "DROPPED" and route:
        current_node = "Route loss"
    else:
        current_node = packet.source

    return {
        "id": packet.packet_id,
        "source": packet.source,
        "destination": packet.destination,
        "sourceLabel": node_label(packet.source),
        "destinationLabel": node_label(packet.destination),
        "trafficType": packet.traffic_type,
        "color": TRAFFIC_COLORS.get(packet.traffic_type, "#e2e8f0"),
        "size": int(packet.size),
        "priority": float(packet.priority),
        "route": route,
        "routeLabel": " → ".join(route) if route else "Waiting for route",
        "currentNode": current_node,
        "status": status,
        "createdAt": float(packet.creation_time),
        "finishedAt": (
            float(packet.delivery_time)
            if packet.delivery_time is not None
            else float(sim.time)
        ),
        "latencyMs": (
            float(packet.latency) * 1000.0
            if packet.latency is not None
            else None
        ),
        "queueWaitMs": (
            float(packet.queue_wait_time) * 1000.0
            if packet.queue_wait_time is not None
            else None
        ),
        "flowId": flow.flow_id if flow else None,
        "animationSeconds": max(0.18, float(animation_seconds)),
    }


def advance_packet(
    sim: NetworkSimulator,
    traces: List[Dict[str, Any]],
    *,
    speed: int = 1,
    sequence: int = 1,
) -> Tuple[bool, int]:
    """Process one real scheduler packet and append its frontend animation trace.

    If traffic is empty but a failure is awaiting heartbeat detection, the
    simulation clock advances instead. No route or packet state is synthesized.
    """
    before_events = len(sim.event_logger.events)
    before_time = sim.time
    packet = sim.process_next_packet()

    if packet is None:
        pending_failures = list(sim._failed_links.values()) + list(
            sim._failed_nodes.values()
        )
        if pending_failures:
            sim.tick(max(0.1, 0.25 * max(1, speed)))
            new_events = [
                event.to_dict()
                for event in sim.event_logger.events[before_events:]
            ]
            traces.append(
                {
                    "sequence": sequence,
                    "kind": "heartbeat",
                    "time": sim.time,
                    "events": new_events,
                }
            )
            del traces[:-24]
            return True, sequence + 1
        return False, sequence

    span = max(sim.time - before_time, packet.latency or 0.05, 0.05)
    animation_seconds = min(2.4, max(0.75, span * 2.2)) / max(1, speed)
    packet_data = _packet_view(sim, packet, animation_seconds=animation_seconds)
    packet_data["sequence"] = sequence
    packet_data["kind"] = "packet"
    new_events = [event.to_dict() for event in sim.event_logger.events[before_events:]]
    packet_data["events"] = new_events
    traces.append(packet_data)
    del traces[:-24]
    return True, sequence + 1


def run_all_packets(
    sim: NetworkSimulator,
    traces: List[Dict[str, Any]],
    *,
    sequence: int = 1,
) -> int:
    """Drain the real scheduler while retaining actual packet event traces."""
    for _ in range(5000):
        advanced, sequence = advance_packet(
            sim, traces, speed=10, sequence=sequence
        )
        if not advanced:
            break
    return sequence


def _session_value(key: str, default: Any = None) -> Any:
    try:
        return st.session_state.get(key, default)
    except RuntimeError:
        return default


def _active_route(sim: NetworkSimulator) -> Tuple[List[str], float, str]:
    selected_flow_id = _session_value("ws_selected_flow_id")
    flow = sim.active_flows.get(selected_flow_id) if selected_flow_id else None
    if flow and flow.current_route:
        return list(flow.current_route), float(flow.route_cost), str(flow.route_status)

    source = str(_session_value("ws_source", "H1"))
    destination = str(_session_value("ws_destination", "H3"))
    route, cost, status = sim.get_current_route(source, destination)
    return route, cost, status


def _route_edge(route: Sequence[str], u: str, v: str) -> bool:
    return any(
        (a == u and b == v) or (a == v and b == u)
        for a, b in zip(route, route[1:])
    )


def build_workspace_snapshot(
    sim: NetworkSimulator,
    *,
    traces: Optional[List[Dict[str, Any]]] = None,
    selected_packet_id: Optional[int] = None,
    active_route: Optional[Sequence[str]] = None,
    route_cost: Optional[float] = None,
    route_status: str = "HEALTHY",
) -> Dict[str, Any]:
    """Build a JSON-safe snapshot entirely from the live simulation engine."""
    if active_route is None:
        active_route, resolved_cost, route_status = _active_route(sim)
        if route_cost is None:
            route_cost = resolved_cost
    active_route = list(active_route)
    if route_cost is None:
        try:
            route_cost = sim.router.route_cost(sim.topology, active_route)
        except ValueError:
            route_cost = float("inf")
    positions_raw = sim.topology.get_positions()
    min_x, max_x = -3.0, 3.0
    min_y, max_y = -2.0, 2.0

    nodes: List[Dict[str, Any]] = []
    for node, attributes in sim.topology.graph.nodes(data=True):
        x, y = positions_raw.get(node, (0.0, 0.0))
        packets_sent = sum(1 for packet in sim.packets if packet.source == node)
        packets_received = sum(
            1
            for packet in sim.packets
            if packet.destination == node and packet.delivery_status == "DELIVERED"
        )
        packets_processed = sum(
            1
            for record in sim.metrics.records
            if record.route and node in record.route
        )
        current_routes = [
            flow.flow_id
            for flow in sim.active_flows.values()
            if node in flow.current_route
        ]
        queued = sum(
            1 for packet in sim.packets if packet.source == node and packet.delivery_status == "PENDING"
        )
        nodes.append(
            {
                "id": node,
                "label": node_label(node),
                "type": attributes.get("type", "router"),
                "status": "ONLINE" if sim.topology.active_node(node) else "FAILED",
                "x": 90 + ((x - min_x) / (max_x - min_x)) * 1020,
                "y": 70 + ((max_y - y) / (max_y - min_y)) * 500,
                "packetsSent": packets_sent,
                "packetsReceived": packets_received,
                "packetsProcessed": packets_processed,
                "currentRoutes": current_routes,
                "queued": queued,
            }
        )

    links: List[Dict[str, Any]] = []
    for u, v, attributes in sim.topology.graph.edges(data=True):
        processed_traffic = sum(
            1
            for packet in sim.packets
            if packet.delivery_status != "PENDING" and _route_edge(packet.route, u, v)
        )
        current_flow_traffic = sum(
            1
            for flow in sim.active_flows.values()
            if _route_edge(flow.current_route, u, v)
        )
        links.append(
            {
                "id": f"{u}-{v}",
                "source": u,
                "target": v,
                "label": link_label(u, v),
                "status": "UP" if sim.topology.active_link(u, v) else "DOWN",
                "latency": float(attributes.get("latency", 0.0)),
                "bandwidth": float(attributes.get("bandwidth", 0.0)),
                "packetLoss": float(attributes.get("packet_loss", 0.0)),
                "congestion": float(attributes.get("congestion", 0.0)),
                "active": _route_edge(active_route, u, v),
                "processedTraffic": processed_traffic,
                "currentFlowTraffic": current_flow_traffic,
            }
        )

    packets = [_packet_view(sim, packet) for packet in sim.packets]
    recent_packets = list((traces or [])[-12:])
    selected_packet = next(
        (packet for packet in packets if packet["id"] == selected_packet_id),
        packets[-1] if packets else None,
    )

    events = [event.to_dict() for event in sim.event_logger.get_events(limit=40)]
    events.reverse()
    queue_stats = sim.scheduler_statistics()
    queue_classes = sim.scheduler_class_statistics()
    metrics = sim.metrics.calculate(
        sim.time, sim.average_congestion(), len(sim.active_flows)
    )

    return {
        "time": round(sim.time, 4),
        "algorithm": ALGORITHM_LABELS.get(sim.get_router_algorithm(), sim.get_router_algorithm()),
        "scheduler": SCHEDULER_LABELS.get(sim.scheduler_name, sim.scheduler_name),
        "route": active_route,
        "routeLabel": " → ".join(active_route) if active_route else "No active route",
        "routeCost": None if not math.isfinite(route_cost) else round(route_cost, 2),
        "routeHops": max(0, len(active_route) - 1),
        "routeStatus": route_status,
        "nodes": nodes,
        "links": links,
        "packets": packets,
        "queuedPackets": [packet for packet in packets if packet["status"] == "QUEUED"][-16:],
        "recentPackets": recent_packets,
        "selectedPacket": selected_packet,
        "events": events,
        "queue": {
            "scheduler": queue_stats.get("scheduler", sim.scheduler_name),
            "length": int(queue_stats.get("current_queue_length", 0)),
            "served": int(queue_stats.get("packets_served", 0)),
            "maxLength": int(queue_stats.get("max_queue_length", 0)),
            "classes": {
                name: {
                    "queued": int(values.get("current_length", 0)),
                    "served": int(values.get("served", 0)),
                }
                for name, values in queue_classes.items()
            },
        },
        "metrics": {
            "sent": int(metrics.get("packets_sent", 0)),
            "delivered": int(metrics.get("packets_delivered", 0)),
            "dropped": int(metrics.get("packets_dropped", 0)),
            "latencyMs": metrics.get("average_latency", 0.0) * 1000.0,
            "throughput": metrics.get("throughput", 0.0),
            "loss": metrics.get("packet_loss", 0.0),
            "pdr": metrics.get("packet_delivery_ratio", 0.0),
        },
    }


_WORKSPACE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #080d14; color: #e6edf3; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; overflow: hidden; }
  .lab { position: relative; height: 642px; background: radial-gradient(circle at 50% 45%, #111c2b 0, #0a111b 58%, #080d14 100%); border: 1px solid #263448; border-radius: 8px; overflow: hidden; }
  .grid { position: absolute; inset: 0; opacity: .22; background-image: linear-gradient(#334155 1px, transparent 1px), linear-gradient(90deg, #334155 1px, transparent 1px); background-size: 32px 32px; mask-image: linear-gradient(to bottom, transparent, #000 12%, #000 88%, transparent); }
  .topbar { position: absolute; z-index: 3; top: 0; left: 0; right: 0; height: 38px; display: flex; align-items: center; gap: 18px; padding: 0 16px; border-bottom: 1px solid #263448; background: rgba(8, 13, 20, .88); font-size: 12px; }
  .brand { color: #67e8f9; font-weight: 800; letter-spacing: 0; }
  .chip { padding: 3px 8px; border: 1px solid #334155; border-radius: 4px; color: #a8b6c8; background: #101a27; white-space: nowrap; }
  .chip strong { color: #f8fafc; }
  .route { margin-left: auto; color: #cbd5e1; max-width: 56%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  svg { position: absolute; inset: 38px 0 34px; width: 100%; height: calc(100% - 72px); }
  .cable { stroke-linecap: round; }
  .cable-shadow { stroke: #020617; stroke-width: 8; opacity: .72; }
  .cable-active { stroke: #22d3ee; stroke-width: 5; filter: drop-shadow(0 0 5px #22d3ee); }
  .cable-normal { stroke: #52657a; stroke-width: 3; }
  .cable-warning { stroke: #f59e0b; stroke-width: 5; }
  .cable-critical { stroke: #fb7185; stroke-width: 6; }
  .cable-down { stroke: #ef4444; stroke-width: 4; stroke-dasharray: 10 8; }
  .flow-dash { stroke: #67e8f9; stroke-width: 2; stroke-dasharray: 4 14; animation: flow 1.2s linear infinite; }
  @keyframes flow { to { stroke-dashoffset: -36; } }
  .hit { stroke: transparent; stroke-width: 18; cursor: pointer; }
  .link-hit:hover + .cable-shadow { stroke: #64748b; }
  .link-label rect { fill: #0b1420; stroke: #304157; stroke-width: 1; rx: 4; }
  .link-label text { fill: #91a4ba; font-size: 10px; font-family: ui-monospace, monospace; text-anchor: middle; dominant-baseline: middle; }
  .link-label.active rect { fill: #062c36; stroke: #22d3ee; }
  .link-label.active text { fill: #cffafe; }
  .node { cursor: pointer; }
  .node .device { fill: #111d2c; stroke: #64748b; stroke-width: 2; filter: drop-shadow(0 7px 10px rgba(0,0,0,.5)); }
  .node.router .device { fill: #142235; stroke: #7dd3fc; }
  .node.online .device { stroke: #67e8f9; }
  .node.failed .device { fill: #2b1118; stroke: #fb7185; }
  .node.selected .selection { opacity: 1; }
  .selection { opacity: 0; fill: none; stroke: #f8fafc; stroke-width: 2; stroke-dasharray: 4 3; }
  .port { fill: #22c55e; }
  .failed .port { fill: #ef4444; }
  .device-glyph { fill: none; stroke: #dbeafe; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .node-name { fill: #f8fafc; font-size: 13px; font-weight: 750; text-anchor: middle; }
  .node-status { fill: #6ee7b7; font-size: 9px; font-weight: 700; text-anchor: middle; letter-spacing: .8px; }
  .node.failed .node-status { fill: #fda4af; }
  .packet { cursor: pointer; filter: drop-shadow(0 0 7px currentColor); }
  .packet-ring { fill: #071019; stroke: currentColor; stroke-width: 3; }
  .packet-core { fill: currentColor; }
  .drop-x { stroke: #ef4444; stroke-width: 4; stroke-linecap: round; filter: drop-shadow(0 0 6px #ef4444); }
  .inspector { position: absolute; z-index: 6; right: 14px; top: 52px; width: 244px; max-height: 536px; overflow: auto; padding: 12px; border: 1px solid #38506c; border-radius: 6px; background: rgba(5, 10, 17, .96); box-shadow: 0 16px 40px rgba(0,0,0,.55); display: none; }
  .inspector.open { display: block; }
  .inspector-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; }
  .inspector-title { font-size: 13px; font-weight: 800; color: #f8fafc; }
  .close { border: 0; background: transparent; color: #94a3b8; font-size: 18px; cursor: pointer; }
  .kv { display: grid; grid-template-columns: 82px 1fr; gap: 5px 8px; font-size: 11px; line-height: 1.35; }
  .kv span:nth-child(odd) { color: #7f91a8; }
  .kv span:nth-child(even) { color: #dbeafe; overflow-wrap: anywhere; }
  .legend { position: absolute; z-index: 3; left: 0; right: 0; bottom: 0; height: 34px; display: flex; align-items: center; gap: 15px; padding: 0 15px; border-top: 1px solid #263448; background: rgba(8, 13, 20, .92); color: #8293a8; font-size: 10px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .swatch { width: 16px; height: 3px; background: #52657a; }
  .swatch.route { background: #22d3ee; }
  .swatch.warn { background: #f59e0b; }
  .swatch.down { background: #ef4444; }
  @media (prefers-reduced-motion: reduce) { .flow-dash { animation: none; } }
</style>
</head>
<body>
<div class="lab" id="lab">
  <div class="grid"></div>
  <div class="topbar">
    <span class="brand">NETADAPT / LIVE LAB</span>
    <span class="chip">TIME <strong id="sim-time">0.00 s</strong></span>
    <span class="chip">ROUTING <strong id="algorithm">Dijkstra</strong></span>
    <span class="chip">QOS <strong id="scheduler">Priority Queue</strong></span>
    <span class="route" id="route"></span>
  </div>
  <svg id="canvas" viewBox="0 0 1200 610" aria-label="Interactive network topology"></svg>
  <aside class="inspector" id="inspector">
    <div class="inspector-head"><span class="inspector-title" id="inspector-title"></span><button class="close" id="close">×</button></div>
    <div class="kv" id="inspector-body"></div>
  </aside>
  <div class="legend">
    <span><i class="swatch"></i>Available link</span>
    <span><i class="swatch route"></i>Active route</span>
    <span><i class="swatch warn"></i>Congestion ≥ 30%</span>
    <span><i class="swatch down"></i>Failed</span>
    <span>Click a device, cable or packet to inspect</span>
  </div>
</div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const canvas = document.getElementById("canvas");
const inspector = document.getElementById("inspector");
const title = document.getElementById("inspector-title");
const body = document.getElementById("inspector-body");
const byId = Object.fromEntries(DATA.nodes.map(node => [node.id, node]));
const packetsById = Object.fromEntries(DATA.packets.map(packet => [packet.id, packet]));
const trafficColors = {Emergency:"#ff4d6d", VoIP:"#22d3ee", Video:"#a78bfa", HTTP:"#38bdf8", FTP:"#f59e0b"};

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
}
function pct(value) { return `${Math.round(value * 100)}%`; }
function num(value, digits = 0) { return Number(value).toFixed(digits); }
function show(titleText, entries) {
  title.textContent = titleText;
  body.replaceChildren();
  entries.forEach(([key, value]) => {
    const k = document.createElement("span"); k.textContent = key;
    const v = document.createElement("span"); v.textContent = value;
    body.append(k, v);
  });
  inspector.classList.add("open");
}
document.getElementById("close").onclick = () => inspector.classList.remove("open");
document.getElementById("sim-time").textContent = `${num(DATA.time, 2)} s`;
document.getElementById("algorithm").textContent = DATA.algorithm;
document.getElementById("scheduler").textContent = DATA.scheduler;
document.getElementById("route").textContent = `${DATA.routeLabel}  ·  cost ${DATA.routeCost ?? "—"}  ·  ${DATA.routeHops} hops`;

const linkLayer = el("g", {id:"links"});
const routeLayer = el("g", {id:"routes"});
const nodeLayer = el("g", {id:"nodes"});
const packetLayer = el("g", {id:"packets"});
canvas.append(linkLayer, routeLayer, nodeLayer, packetLayer);

DATA.links.forEach(link => {
  const a = byId[link.source], b = byId[link.target];
  const group = el("g", {class:"network-link"});
  const hit = el("line", {x1:a.x, y1:a.y, x2:b.x, y2:b.y, class:"hit"});
  const shadow = el("line", {x1:a.x, y1:a.y, x2:b.x, y2:b.y, class:"cable cable-shadow"});
  let cableClass = "cable-normal";
  if (link.status === "DOWN") cableClass = "cable-down";
  else if (link.congestion >= .7) cableClass = "cable-critical";
  else if (link.congestion >= .3) cableClass = "cable-warning";
  const cable = el("line", {x1:a.x, y1:a.y, x2:b.x, y2:b.y, class:`cable ${cableClass}`});
  if (link.active && link.status === "UP") {
    const animated = el("line", {x1:a.x, y1:a.y, x2:b.x, y2:b.y, class:"flow-dash"});
    routeLayer.append(animated);
  }
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const label = el("g", {class:`link-label${link.active ? " active" : ""}`, transform:`translate(${mx} ${my})`});
  label.append(el("rect", {x:-57, y:-10, width:114, height:20}));
  label.append(el("text", {x:0, y:0}, `${num(link.latency)}ms · ${num(link.bandwidth)}Mb/s${link.congestion ? ` · ${pct(link.congestion)}` : ""}`));
  hit.addEventListener("click", () => show(link.label, [
    ["Status", link.status], ["Latency", `${num(link.latency, 1)} ms`],
    ["Bandwidth", `${num(link.bandwidth, 1)} Mbps`], ["Packet loss", pct(link.packetLoss)],
    ["Congestion", pct(link.congestion)], ["Route", link.active ? "ACTIVE" : "Alternative"],
    ["Processed", `${link.processedTraffic} packets`], ["Current flows", link.currentFlowTraffic]
  ]));
  group.append(hit, shadow, cable, label);
  linkLayer.append(group);
});

DATA.nodes.forEach(node => {
  const group = el("g", {class:`node ${node.type} ${node.status === "ONLINE" ? "online" : "failed"}`, transform:`translate(${node.x} ${node.y})`});
  group.append(el("rect", {class:"selection", x:-45, y:-39, width:90, height:78, rx:8}));
  if (node.type === "host") {
    group.append(el("rect", {class:"device", x:-28, y:-25, width:56, height:38, rx:5}));
    group.append(el("rect", {class:"device-glyph", x:-21, y:-18, width:42, height:23, rx:2}));
    group.append(el("path", {class:"device-glyph", d:"M-10 13 L-13 21 L13 21 L10 13 M0 21 L0 26 M-15 26 L15 26"}));
  } else {
    group.append(el("rect", {class:"device", x:-38, y:-25, width:76, height:50, rx:7}));
    group.append(el("path", {class:"device-glyph", d:"M-19 -7 L-8 0 L-19 7 M19 -7 L8 0 L19 7 M-4 0 L4 0"}));
    group.append(el("circle", {class:"port", cx:-30, cy:-20, r:2.5}));
    group.append(el("circle", {class:"port", cx:30, cy:-20, r:2.5}));
    group.append(el("circle", {class:"port", cx:-30, cy:20, r:2.5}));
    group.append(el("circle", {class:"port", cx:30, cy:20, r:2.5}));
  }
  group.append(el("text", {class:"node-name", x:0, y:39}, node.label));
  group.append(el("text", {class:"node-status", x:0, y:53}, node.status));
  group.addEventListener("click", () => {
    nodeLayer.querySelectorAll(".node").forEach(n => n.classList.remove("selected"));
    group.classList.add("selected");
    const type = node.type === "host" ? "Endpoint" : "Router";
    show(`${type} ${node.id}`, [
      ["Status", node.status], ["Packets sent", node.packetsSent],
      ["Packets received", node.packetsReceived], ["Packets processed", node.packetsProcessed],
      ["Queued at source", node.queued], ["Current routes", node.currentRoutes.join(", ") || "None"]
    ]);
  });
  nodeLayer.append(group);
});

function packetPath(packet) {
  return packet.route.map(id => byId[id]).filter(Boolean).map((node, index) => `${index ? "L" : "M"}${node.x} ${node.y}`).join(" ");
}
function inspectPacket(packet) {
  show(`Packet #${packet.id}`, [
    ["Source", packet.sourceLabel], ["Destination", packet.destinationLabel],
    ["Traffic", packet.trafficType], ["Size", `${packet.size} bytes`],
    ["Priority", packet.priority], ["Status", packet.status],
    ["Current", packet.currentNode], ["Route", packet.routeLabel],
    ["Latency", packet.latencyMs == null ? "Pending" : `${num(packet.latencyMs, 1)} ms`],
    ["Queue wait", packet.queueWaitMs == null ? "Pending" : `${num(packet.queueWaitMs, 1)} ms`],
    ["Flow", packet.flowId || "Unassigned"]
  ]);
}
function addPacketMarker(packet, point, faded = false) {
  const group = el("g", {class:"packet", style:`color:${packet.color}`, opacity:faded ? ".45" : "1", tabindex:"0"});
  group.append(el("circle", {class:"packet-ring", cx:point.x, cy:point.y, r:9}));
  group.append(el("circle", {class:"packet-core", cx:point.x, cy:point.y, r:4}));
  group.addEventListener("click", event => { event.stopPropagation(); inspectPacket(packet); });
  packetLayer.append(group);
  return group;
}
function pointAlong(path, ratio) {
  const length = path.getTotalLength();
  const point = path.getPointAtLength(length * ratio);
  return {x:point.x, y:point.y};
}

DATA.recentPackets.filter(item => item.kind === "packet").forEach((packet, index) => {
  const pathData = packetPath(packet);
  if (!pathData) return;
  const path = el("path", {d:pathData, fill:"none", stroke:"none"});
  packetLayer.append(path);
  const group = el("g", {class:"packet", style:`color:${packet.color}`, tabindex:"0"});
  group.append(el("circle", {class:"packet-ring", cx:0, cy:0, r:9}));
  group.append(el("circle", {class:"packet-core", cx:0, cy:0, r:4}));
  const motion = el("animateMotion", {path:pathData, dur:`${packet.animationSeconds}s`, begin:`${index * .08}s`, fill:"freeze", calcMode:"paced"});
  const fade = el("animate", {attributeName:"opacity", dur:`${packet.animationSeconds}s`, begin:`${index * .08}s`, values:"0;1;1;0", keyTimes:"0;.08;.78;1", fill:"freeze"});
  group.append(motion, fade);
  group.addEventListener("click", event => { event.stopPropagation(); inspectPacket(packet); });
  packetLayer.append(group);
  if (packet.status === "DROPPED") {
    const point = pointAlong(path, .78);
    const x = el("g", {class:"packet", style:`color:${packet.color}`});
    x.append(el("path", {class:"drop-x", d:`M${point.x-5} ${point.y-5}L${point.x+5} ${point.y+5}M${point.x+5} ${point.y-5}L${point.x-5} ${point.y+5}`}));
    x.addEventListener("click", event => { event.stopPropagation(); inspectPacket(packet); });
    packetLayer.append(x);
  } else {
    const destination = byId[packet.destination];
    if (destination) addPacketMarker(packet, destination, true);
  }
});

DATA.queuedPackets.forEach((packet, index) => {
  const source = byId[packet.source];
  if (!source) return;
  const angle = (Math.PI * 2 * index) / Math.max(6, DATA.queuedPackets.length);
  const point = {x:source.x + 30 + Math.cos(angle) * 17, y:source.y + Math.sin(angle) * 17};
  const group = addPacketMarker(packet, point);
  const pulse = el("animate", {attributeName:"opacity", values:".45;1;.45", dur:"1.2s", repeatCount:"indefinite"});
  group.append(pulse);
});

if (!DATA.packets.length) {
  show("Traffic generator", [["State", "Ready"], ["Action", "Send packets to begin"], ["Source of truth", "Live simulation engine"]]);
}
</script>
</body>
</html>
"""


def render_workspace_canvas(snapshot: Dict[str, Any]) -> None:
    """Render the state-driven SVG workspace component."""
    payload = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("</", "<\\/")
    components.html(
        _WORKSPACE_HTML.replace("__DATA__", payload),
        height=650,
        scrolling=False,
    )


def _ensure_workspace_state(sim: NetworkSimulator) -> None:
    defaults = {
        "ws_playing": False,
        "ws_traces": [],
        "ws_trace_sequence": 1,
        "ws_selected_packet_id": None,
        "ws_selected_flow_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _selected_link(sim: NetworkSimulator) -> Tuple[str, str]:
    selected = str(st.session_state.ws_link)
    for u, v in sim.topology.links():
        if selected in (f"{u}-{v}", f"{v}-{u}"):
            return u, v
    u, v = sim.topology.links()[0]
    return u, v


def render_workspace_controls(sim: NetworkSimulator) -> None:
    """Render compact traffic, fault, routing and QoS controls."""
    _ensure_workspace_state(sim)
    nodes = sim.topology.nodes()
    links = sim.topology.links()
    link_options = [f"{u}-{v}" for u, v in links]

    with st.sidebar:
        st.markdown("### NetAdapt Console")
        st.caption("LIVE NETWORK SIMULATOR")

        with st.expander("Traffic Generator", expanded=True):
            source = st.selectbox(
                "Source",
                nodes,
                index=nodes.index(_session_value("ws_source", "H1")),
                format_func=node_label,
                key="ws_source",
            )
            destination = st.selectbox(
                "Destination",
                nodes,
                index=nodes.index(_session_value("ws_destination", "H3")),
                format_func=node_label,
                key="ws_destination",
            )
            traffic_type = st.selectbox(
                "Traffic type",
                TRAFFIC_CLASSES,
                index=TRAFFIC_CLASSES.index(_session_value("ws_traffic_type", "Video")),
                key="ws_traffic_type",
            )
            size_col, count_col = st.columns(2)
            packet_size = size_col.number_input(
                "Size (B)",
                64,
                9000,
                int(_session_value("ws_packet_size", 1500)),
                100,
                key="ws_packet_size",
            )
            packet_count = count_col.number_input(
                "Count",
                1,
                500,
                int(_session_value("ws_packet_count", 20)),
                1,
                key="ws_packet_count",
            )
            pps = st.number_input(
                "Rate (packets/sec)",
                0.5,
                200.0,
                float(_session_value("ws_pps", 10.0)),
                0.5,
                key="ws_pps",
            )
            if st.button("SEND PACKETS", type="primary", use_container_width=True):
                try:
                    if source == destination:
                        raise ValueError("Source and destination must differ.")
                    flow = sim.create_flow(
                        source,
                        destination,
                        traffic_type,
                        int(packet_count),
                        int(packet_size),
                        float(pps),
                        float(packet_count) / max(float(pps), 0.1),
                    )
                    st.session_state.ws_selected_flow_id = flow.flow_id
                    st.session_state.ws_playing = True
                    st.toast(f"{link_label(source, destination)} traffic queued")
                except Exception as error:
                    st.error(str(error))
                st.rerun()

        with st.expander("Network Conditions", expanded=False):
            default_link = _session_value("ws_link", "R3-R5")
            default_link_index = link_options.index(default_link) if default_link in link_options else 0
            st.selectbox("Link", link_options, index=default_link_index, key="ws_link")
            u, v = _selected_link(sim)
            current = sim.topology.get_link(u, v) or {}
            st.caption(
                f"{link_label(u, v)} · {current.get('bandwidth', 0):g} Mbps · "
                f"{current.get('packet_loss', 0):.0%} loss"
            )
            congestion = st.slider(
                "Congestion", 0.0, 1.0, float(_session_value("ws_congestion", 0.0)), 0.05, key="ws_congestion"
            )
            packet_loss = st.slider(
                "Packet loss", 0.0, 1.0, float(_session_value("ws_packet_loss", 0.0)), 0.05, key="ws_packet_loss"
            )
            bandwidth = st.number_input(
                "Bandwidth (Mbps)", 0.1, 1000.0, float(_session_value("ws_bandwidth", 80.0)), 5.0, key="ws_bandwidth"
            )
            if st.button("APPLY CONDITIONS", use_container_width=True):
                sim.set_congestion(u, v, float(congestion))
                sim.set_packet_loss(u, v, float(packet_loss))
                sim.set_bandwidth(u, v, float(bandwidth))
                st.toast(f"Conditions applied to {link_label(u, v)}")
                st.rerun()

        with st.expander("Fault Injection", expanded=False):
            fail_col, recover_col = st.columns(2)
            if fail_col.button("FAIL LINK", use_container_width=True):
                try:
                    sim.fail_link(u, v)
                    sim.tick(sim.failure_detection_timeout + 0.01)
                    st.session_state.ws_playing = False
                    st.toast(f"{link_label(u, v)} failed; heartbeat timeout elapsed")
                except Exception as error:
                    st.error(str(error))
                st.rerun()
            if recover_col.button("RECOVER", use_container_width=True):
                sim.recover_link(u, v)
                st.toast(f"{link_label(u, v)} recovered")
                st.rerun()

            default_node = _session_value("ws_node", "R3")
            default_node_index = nodes.index(default_node) if default_node in nodes else 0
            selected_node = st.selectbox(
                "Device",
                nodes,
                index=default_node_index,
                format_func=node_label,
                key="ws_node",
            )
            node_fail_col, node_recover_col = st.columns(2)
            if node_fail_col.button("FAIL NODE", use_container_width=True):
                sim.fail_node(str(selected_node))
                sim.tick(sim.failure_detection_timeout + 0.01)
                st.session_state.ws_playing = False
                st.rerun()
            if node_recover_col.button("RECOVER", use_container_width=True, key="recover_node"):
                sim.recover_node(str(selected_node))
                st.rerun()

        with st.expander("Routing", expanded=False):
            algorithm = st.selectbox(
                "Algorithm",
                list(ALGORITHM_LABELS),
                index=list(ALGORITHM_LABELS).index(sim.get_router_algorithm()),
                format_func=lambda key: ALGORITHM_LABELS[key],
                key="ws_algorithm",
            )
            if st.button("APPLY ALGORITHM", use_container_width=True):
                sim.set_router_algorithm(algorithm)
                st.rerun()
            with st.popover("Route cost weights"):
                weight_values: Dict[str, float] = {}
                current_weights = sim.get_routing_weights()
                for name in ROUTING_WEIGHT_NAMES:
                    weight_values[name] = st.number_input(
                        name,
                        0.0,
                        1000.0,
                        float(current_weights.get(name, DEFAULT_ROUTING_WEIGHTS[name])),
                        1.0,
                        key=f"ws_weight_{name}",
                    )
                if st.button("APPLY WEIGHTS", use_container_width=True):
                    sim.set_routing_weights(**weight_values)
                    st.rerun()

        with st.expander("QoS Scheduler", expanded=False):
            scheduler = st.selectbox(
                "Scheduler",
                list(SCHEDULER_LABELS),
                index=list(SCHEDULER_LABELS).index(sim.scheduler_name),
                format_func=lambda key: SCHEDULER_LABELS[key],
                key="ws_scheduler",
            )
            if st.button("APPLY SCHEDULER", use_container_width=True):
                sim.set_scheduler(scheduler)
                st.rerun()


def _render_snapshot_details(sim: NetworkSimulator, snapshot: Dict[str, Any]) -> None:
    tabs = st.tabs(["PACKETS", "DEVICE", "LINK", "QUEUE", "EVENTS", "METRICS"])
    with tabs[0]:
        if sim.packets:
            packet_ids = [packet.packet_id for packet in reversed(sim.packets)]
            default_packet = snapshot["selectedPacket"]
            default_index = (
                packet_ids.index(default_packet["id"])
                if default_packet and default_packet["id"] in packet_ids
                else 0
            )
            selected_id = st.selectbox(
                "Recently transmitted packet",
                packet_ids,
                index=default_index,
                format_func=lambda packet_id: f"Packet #{packet_id}",
                key="ws_packet_inspector",
            )
            st.session_state.ws_selected_packet_id = selected_id

            packet = next(item for item in sim.packets if item.packet_id == selected_id)
            view = _packet_view(sim, packet)
            left, right = st.columns(2)
            left.markdown(
                f"**Packet #{view['id']}**  `{view['status']}`\n\n"
                f"- Source: **{view['sourceLabel']}**\n"
                f"- Destination: **{view['destinationLabel']}**\n"
                f"- Traffic: **{view['trafficType']}** / priority {view['priority']:g}\n"
                f"- Size: **{view['size']} bytes**\n"
                f"- Queue wait: **{('%.2f ms' % view['queueWaitMs']) if view['queueWaitMs'] is not None else 'pending'}**"
            )
            right.markdown(
                f"**Path**\n\n`{view['routeLabel']}`\n\n"
                f"- Current: **{view['currentNode']}**\n"
                f"- Latency: **{('%.2f ms' % view['latencyMs']) if view['latencyMs'] is not None else 'pending'}**\n"
                f"- Flow: **{view['flowId'] or 'unassigned'}**"
            )
        else:
            st.info("No packets yet. Use SEND PACKETS in the console.")

    with tabs[1]:
        node_map = {node["id"]: node for node in snapshot["nodes"]}
        selected_node = st.selectbox(
            "Device", list(node_map), format_func=node_label, key="ws_inspect_node"
        )
        node = node_map[selected_node]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status", node["status"])
        c2.metric("Sent", node["packetsSent"])
        c3.metric("Processed", node["packetsProcessed"])
        c4.metric("Received", node["packetsReceived"])
        st.caption(f"Current routes: {', '.join(node['currentRoutes']) or 'none'} · queued at source: {node['queued']}")

    with tabs[2]:
        link_map = {link["id"]: link for link in snapshot["links"]}
        selected_link = st.selectbox("Link", list(link_map), format_func=lambda value: link_map[value]["label"], key="ws_inspect_link")
        link = link_map[selected_link]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status", link["status"])
        c2.metric("Latency", f"{link['latency']:.1f} ms")
        c3.metric("Bandwidth", f"{link['bandwidth']:.0f} Mbps")
        c4.metric("Congestion", f"{link['congestion']:.0%}")
        st.caption(
            f"Loss {link['packetLoss']:.0%} · processed traffic {link['processedTraffic']} packets · "
            f"active flows {link['currentFlowTraffic']} · route {'ACTIVE' if link['active'] else 'alternative'}"
        )

    with tabs[3]:
        queue = snapshot["queue"]
        st.markdown(f"**{queue['scheduler']}** · depth **{queue['length']}** · served **{queue['served']}** · max **{queue['maxLength']}**")
        rows = []
        for traffic_class in TRAFFIC_CLASSES:
            values = queue["classes"][traffic_class]
            rows.append(
                {
                    "Traffic Class": traffic_class,
                    "Queued": values["queued"],
                    "Served": values["served"],
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        for row in rows:
            st.markdown(f"`{row['Traffic Class']:<9}` {'●' * min(20, row['Queued'])} {row['Queued']}")

    with tabs[4]:
        if snapshot["events"]:
            event_rows = []
            for event in snapshot["events"]:
                event_rows.append(
                    {
                        "Time": event["time"],
                        "Event": event["event"],
                        "Component": event.get("component") or "",
                        "Message": event.get("message") or "",
                    }
                )
            st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True, height=280)
        else:
            st.info("Simulation events will appear here.")

    with tabs[5]:
        metrics = snapshot["metrics"]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Latency", f"{metrics['latencyMs']:.1f} ms")
        c2.metric("Delivered", metrics["delivered"])
        c3.metric("Dropped", metrics["dropped"])
        c4.metric("PDR", f"{metrics['pdr']:.1f}%")
        c5.metric("Throughput", f"{metrics['throughput']:.0f} B/s")
        if sim.metrics.history:
            st.line_chart(
                pd.DataFrame(sim.metrics.history)[["time", "average_latency", "congestion"]]
                .rename(
                    columns={
                        "time": "Simulation time",
                        "average_latency": "Latency (s)",
                        "congestion": "Congestion",
                    }
                ),
                use_container_width=True,
            )


def _workspace_fragment() -> None:
    sim: NetworkSimulator = st.session_state.simulator
    if st.session_state.ws_playing:
        speed = int(st.session_state.ws_speed)
        advanced = False
        traces = list(st.session_state.ws_traces)
        sequence = int(st.session_state.ws_trace_sequence)
        for _ in range(max(1, speed)):
            did_advance, sequence = advance_packet(
                sim,
                traces,
                speed=speed,
                sequence=sequence,
            )
            advanced = advanced or did_advance
            if not did_advance:
                break
        st.session_state.ws_traces = traces
        st.session_state.ws_trace_sequence = sequence
        if not advanced:
            st.session_state.ws_playing = False

    snapshot = build_workspace_snapshot(
        sim,
        traces=st.session_state.ws_traces,
        selected_packet_id=st.session_state.ws_selected_packet_id,
    )
    render_workspace_canvas(snapshot)
    _render_snapshot_details(sim, snapshot)


if hasattr(st, "fragment"):
    _workspace_fragment = st.fragment(run_every=0.45)(_workspace_fragment)


def render_network_workspace(sim: NetworkSimulator) -> None:
    """Render the main simulator control bar, SVG workspace and detail panels."""
    _ensure_workspace_state(sim)

    reset_col, step_col, play_col, pause_col, run_col, speed_col, clock_col = st.columns(
        [1.05, 1.0, 1.0, 1.0, 1.0, 1.15, 1.25]
    )
    if reset_col.button("RESET", use_container_width=True):
        sim.reset()
        st.session_state.ws_traces = []
        st.session_state.ws_trace_sequence = 1
        st.session_state.ws_playing = False
        st.session_state.ws_selected_packet_id = None
        st.session_state.ws_selected_flow_id = None
        st.rerun()
    if step_col.button("STEP", use_container_width=True):
        st.session_state.ws_playing = False
        traces = list(st.session_state.ws_traces)
        _, sequence = advance_packet(
            sim,
            traces,
            speed=int(_session_value("ws_speed", 1)),
            sequence=st.session_state.ws_trace_sequence,
        )
        st.session_state.ws_traces = traces
        st.session_state.ws_trace_sequence = sequence
    if play_col.button("PLAY", use_container_width=True, type="primary"):
        st.session_state.ws_playing = True
    if pause_col.button("PAUSE", use_container_width=True):
        st.session_state.ws_playing = False
    if run_col.button("RUN ALL", use_container_width=True):
        st.session_state.ws_playing = False
        traces = list(st.session_state.ws_traces)
        st.session_state.ws_trace_sequence = run_all_packets(
            sim,
            traces,
            sequence=st.session_state.ws_trace_sequence,
        )
        st.session_state.ws_traces = traces
    speed_col.selectbox(
        "Speed",
        [1, 2, 5, 10],
        index=[1, 2, 5, 10].index(int(_session_value("ws_speed", 1))),
        format_func=lambda value: f"{value}x",
        key="ws_speed",
        label_visibility="collapsed",
    )
    state = "RUNNING" if st.session_state.ws_playing else "PAUSED"
    clock_col.metric("Simulation Time", f"{sim.time:.2f} s", delta=state)

    _workspace_fragment()
