"""Stage 11 - Network Overview, rule-based health, and the final report.

Every value here is read from the live simulator.  The health view uses
explicit, published rules instead of an invented score, and every rule states
the reason behind its verdict.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

HEALTHY = "HEALTHY"
WARNING = "WARNING"
CRITICAL = "CRITICAL"

#: Published thresholds used by the health rules.
THRESHOLDS = {
    "pdr_warning_percent": 95.0,
    "pdr_critical_percent": 50.0,
    "congestion_warning": 0.30,
    "congestion_critical": 0.70,
    "queue_warning": 50.0,
}


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


def network_overview(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    topology = simulator.topology
    metrics = simulator.metrics.calculate(
        simulator.time, simulator.average_congestion(), len(simulator.active_flows)
    )
    queue = simulator.scheduler_statistics()
    transport = simulator.get_transport_statistics()
    services = simulator.services.service_status_all()
    security = simulator.security_state()
    events = simulator.event_logger.events
    return {
        "devices": len(topology.devices),
        "links": topology.graph.number_of_edges(),
        "active_flows": len(simulator.active_flows),
        "packets_sent": metrics["packets_sent"],
        "packets_delivered": metrics["packets_delivered"],
        "packets_lost": metrics["packets_dropped"],
        "packet_delivery_ratio": metrics["packet_delivery_ratio"],
        "average_latency_ms": round(metrics["average_latency"] * 1000, 3),
        "throughput": round(metrics["throughput"], 2),
        "congestion": round(simulator.average_congestion(), 4),
        "queue_length": queue.get("current_queue_length", 0),
        "queue_max": queue.get("max_queue_length", 0),
        "active_failures": len(simulator.get_failed_links()) + len(simulator.get_failed_nodes()),
        "route_changes": sum(1 for event in events if event.event_type == "ROUTE_RECALCULATED"),
        "tcp_connections": len(transport["connections"]),
        "udp_flows": len(transport["udp_flows"]),
        "services_running": sum(1 for row in services if row["state"] == "RUNNING"),
        "services_total": len(services),
        "blocked_packets": security["metrics"]["packets_blocked"],
        "security_alerts": (
            len(security["arp"]["conflicts"])
            + security["metrics"]["floods_detected"]
            + security["arp"]["spoof_attempts"]
        ),
        "time": round(simulator.time, 3),
        "scheduler": simulator.scheduler_name,
    }


# --------------------------------------------------------------------------
# Health (transparent, rule-based)
# --------------------------------------------------------------------------


def _rule(state: str, reason: str) -> Dict[str, str]:
    return {"status": state, "reason": reason}


def _worst(states: List[str]) -> str:
    if CRITICAL in states:
        return CRITICAL
    if WARNING in states:
        return WARNING
    return HEALTHY


def _connectivity(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    topology = simulator.topology
    hosts = [
        (name, device)
        for name, device in topology.devices.items()
        if not device.is_router and not device.is_switch
    ]
    rules: List[Dict[str, str]] = []
    down_nodes = simulator.get_failed_nodes()
    down_links = simulator.get_failed_links()
    for name, device in hosts:
        if device.status != "UP":
            rules.append(_rule(CRITICAL, f"{name} is DOWN"))
            continue
        if not any(interface.ip_address for interface in device.interfaces):
            rules.append(_rule(CRITICAL, f"{name} has no IPv4 address"))
            continue
        if any(interface.status != "UP" for interface in device.interfaces):
            rules.append(
                _rule(WARNING, f"{name} has a downed interface")
            )
    for name in hosts:
        other = next((item for item in hosts if item[0] != name[0]), None)
        if other is None:
            continue
        try:
            path, _, _ = simulator.get_current_route(name[0], other[0])
        except Exception:
            path = []
        if not path:
            rules.append(_rule(CRITICAL, f"no route between {name[0]} and {other[0]}"))
    if down_nodes:
        rules.append(_rule(WARNING, f"failed nodes: {', '.join(down_nodes)}"))
    if down_links:
        rules.append(
            _rule(WARNING, f"failed links: {', '.join(f'{u}-{v}' for u, v in down_links)}")
        )
    if not rules:
        rules.append(_rule(HEALTHY, "every host is UP, addressed, and reachable"))
    return {"category": "Connectivity", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def _routing(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    topology = simulator.topology
    routers = [name for name, device in topology.devices.items() if device.is_router]
    hosts = [name for name, device in topology.devices.items() if not device.is_router]
    rules: List[Dict[str, str]] = []
    unreachable = 0
    for source in hosts:
        for destination in hosts:
            if source == destination:
                continue
            path, _, status = simulator.get_current_route(source, destination)
            if not path or status.startswith("FAILED"):
                unreachable += 1
    if unreachable:
        rules.append(
            _rule(CRITICAL, f"{unreachable} host pair(s) have no usable route while components are failed")
        )
    else:
        rules.append(_rule(HEALTHY, f"every host pair has a route across {len(routers)} router(s)"))
    changes = sum(
        1 for event in simulator.event_logger.events if event.event_type == "ROUTE_RECALCULATED"
    )
    if changes:
        rules.append(_rule(WARNING, f"{changes} route recalculation(s) happened in this run"))
    return {"category": "Routing", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def _performance(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    metrics = simulator.metrics.calculate(
        simulator.time, simulator.average_congestion(), len(simulator.active_flows)
    )
    queue = simulator.scheduler_statistics()
    rules: List[Dict[str, str]] = []
    pdr = metrics["packet_delivery_ratio"]
    if not metrics["packets_sent"]:
        rules.append(_rule(HEALTHY, "no traffic has been measured yet, so delivery ratio is not judged"))
    elif pdr < THRESHOLDS["pdr_critical_percent"]:
        rules.append(_rule(CRITICAL, f"packet delivery ratio {pdr:.1f}% is below {THRESHOLDS['pdr_critical_percent']:.0f}%"))
    elif pdr < THRESHOLDS["pdr_warning_percent"]:
        rules.append(_rule(WARNING, f"packet delivery ratio {pdr:.1f}% is below {THRESHOLDS['pdr_warning_percent']:.0f}%"))
    else:
        rules.append(_rule(HEALTHY, f"packet delivery ratio {pdr:.1f}%"))
    congestion = simulator.average_congestion()
    if congestion >= THRESHOLDS["congestion_critical"]:
        rules.append(_rule(CRITICAL, f"average congestion {congestion:.2f} is critical"))
    elif congestion >= THRESHOLDS["congestion_warning"]:
        rules.append(_rule(WARNING, f"average congestion {congestion:.2f} exceeds {THRESHOLDS['congestion_warning']:.2f}"))
    else:
        rules.append(_rule(HEALTHY, f"average congestion {congestion:.2f}"))
    if queue.get("max_queue_length", 0) > THRESHOLDS["queue_warning"]:
        rules.append(_rule(WARNING, f"peak queue length {queue['max_queue_length']:.0f} packets"))
    return {"category": "Performance", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def _qos(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    used = {record.traffic_type for record in simulator.metrics.records if record.traffic_type}
    rules: List[Dict[str, str]] = []
    if len(used) > 1 and simulator.scheduler_name == "fifo":
        rules.append(
            _rule(WARNING, f"FIFO serves {len(used)} traffic classes in arrival order, with no differentiation")
        )
    else:
        rules.append(
            _rule(HEALTHY, f"{simulator.scheduler_name} scheduler with {len(used)} active class(es)")
        )
    return {"category": "QoS", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def _services(session: Any) -> Dict[str, Any]:
    layer = session.simulator.services
    rules: List[Dict[str, str]] = []
    for instance in layer.registry.list():
        ready, reason = layer.service_ready(instance)
        if not ready:
            state = CRITICAL if reason == "DEVICE_DOWN" else WARNING
            rules.append(_rule(state, f"{instance.name} on {instance.device} is unavailable: {reason}"))
    if not rules:
        installed = len(layer.registry.list())
        rules.append(
            _rule(HEALTHY, f"all {installed} installed service(s) are running")
            if installed
            else _rule(HEALTHY, "no service is installed")
        )
    return {"category": "Services", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def _security(session: Any) -> Dict[str, Any]:
    state = session.simulator.security_state()
    metrics = state["metrics"]
    rules: List[Dict[str, str]] = []
    if metrics["arp_conflicts"]:
        rules.append(
            _rule(CRITICAL, f"{metrics['arp_conflicts']:.0f} unresolved ARP conflict(s) detected")
        )
    if state["flood"]["detected_floods"]:
        rules.append(
            _rule(CRITICAL, f"{state['flood']['detected_floods']} flood(s) detected above the threshold")
        )
    if metrics["spoof_attempts"]:
        rules.append(_rule(WARNING, f"{metrics['spoof_attempts']:.0f} ARP spoof attempt(s) recorded"))
    if metrics["packets_blocked"]:
        rules.append(
            _rule(WARNING, f"{metrics['packets_blocked']:.0f} packet(s) blocked by firewall/ACL/port policy")
        )
    if not rules:
        rules.append(_rule(HEALTHY, "no security alert is active"))
    return {"category": "Security", "status": _worst([rule["status"] for rule in rules]), "rules": rules}


def network_health(session: Any) -> Dict[str, Any]:
    categories = [
        _connectivity(session),
        _routing(session),
        _performance(session),
        _qos(session),
        _services(session),
        _security(session),
    ]
    overall = _worst([item["status"] for item in categories])
    return {
        "overall": overall,
        "thresholds": dict(THRESHOLDS),
        "categories": categories,
        "counts": {
            state: sum(1 for item in categories if item["status"] == state)
            for state in (HEALTHY, WARNING, CRITICAL)
        },
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _routing_summary(session: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for device in session.simulator.topology.devices.values():
        if not device.is_router:
            continue
        for entry in session.simulator.routing_table(device.name).entries:
            rows.append(
                {
                    "router": device.name,
                    "network": f"{entry.destination_network}/{entry.prefix}",
                    "next_hop": entry.next_hop,
                    "interface": entry.outgoing_interface,
                    "metric": entry.metric,
                }
            )
    return rows


def network_report(session: Any) -> Dict[str, Any]:
    """The complete final report, built from live simulator state."""

    simulator = session.simulator
    topology = simulator.topology
    events = simulator.event_logger.events
    metrics = simulator.metrics.calculate(
        simulator.time, simulator.average_congestion(), len(simulator.active_flows)
    )
    transport = simulator.get_transport_statistics()
    services = simulator.service_state()
    security = simulator.security_state()
    challenges = getattr(session, "challenge_state", None) or {}
    quiz = getattr(session, "quiz_state", None) or {}

    report = {
        "generated_at": round(simulator.time, 3),
        "network_name": session.document.get("name", "Untitled Network"),
        "topology": {
            "devices": len(topology.devices),
            "links": topology.graph.number_of_edges(),
            "device_types": sorted({device.device_type for device in topology.devices.values()}),
        },
        "devices": [
            {
                "name": device.name,
                "type": device.device_type,
                "status": device.status,
                "default_gateway": device.default_gateway,
                "interfaces": len(device.interfaces),
            }
            for device in topology.devices.values()
        ],
        "interfaces": [
            {
                "device": device.name,
                "interface": interface.interface_id,
                "mac": interface.mac_address,
                "ip": interface.ip_address,
                "prefix": interface.prefix,
                "status": interface.status,
            }
            for device in topology.devices.values()
            for interface in device.interfaces
        ],
        "routing": {
            "algorithm": simulator.get_router_algorithm(),
            "tables": _routing_summary(session),
            "changes": sum(1 for event in events if event.event_type == "ROUTE_RECALCULATED"),
        },
        "traffic": {
            "flows": [flow.to_dict() for flow in simulator.active_flows.values()],
            "packets_sent": metrics["packets_sent"],
            "packets_delivered": metrics["packets_delivered"],
            "packets_lost": metrics["packets_dropped"],
            "packet_delivery_ratio": metrics["packet_delivery_ratio"],
            "average_latency_ms": round(metrics["average_latency"] * 1000, 3),
            "throughput": round(metrics["throughput"], 2),
        },
        "qos": {
            "scheduler": simulator.scheduler_name,
            "priorities": simulator.get_priority_config(),
            "weights": simulator.get_wfq_weights(),
            "queue": simulator.scheduler_statistics(),
            "classes": simulator.scheduler_class_statistics(),
        },
        "transport": {
            "statistics": transport["TCP"],
            "udp": transport["UDP"],
            "connections": [flow["flow_id"] + ":" + flow["state"] for flow in transport["connections"]],
        },
        "services": {
            "registry": services["registry"],
            "metrics": services["metrics"],
            "dhcp_leases": services["dhcp"]["leases"],
            "dns_records": services["dns"]["records"],
        },
        "security": {
            "firewall": security["firewall"]["rules"],
            "access_lists": security["access_lists"],
            "arp": {
                "spoof_attempts": security["arp"]["spoof_attempts"],
                "conflicts": security["arp"]["conflicts"],
            },
            "flood": security["flood"],
            "metrics": security["metrics"],
        },
        "failures": {
            "failed_nodes": simulator.get_failed_nodes(),
            "failed_links": [f"{u}-{v}" for u, v in simulator.get_failed_links()],
            "events": [event.to_dict() for event in events if event.event_type in {"NODE_FAILED", "LINK_FAILED", "FAILURE_DETECTED", "NODE_RECOVERED", "LINK_RECOVERED"}],
        },
        "recovery": {
            "records": [record.__dict__ for record in simulator.metrics.recovery_records]
            if hasattr(simulator.metrics, "recovery_records")
            else [],
            "recoveries": sum(1 for event in events if event.event_type in {"NODE_RECOVERED", "LINK_RECOVERED"}),
        },
        "learning": {
            "topics": len(getattr(session, "learning_catalog", []) or []),
            "last_demonstration": (session.learning_result or {}).get("topic", {}).get("title")
            if isinstance(session.learning_result, dict)
            else None,
        },
        "challenges": challenges,
        "quiz": quiz,
        "overview": network_overview(session),
        "health": network_health(session),
    }
    return report


def report_to_markdown(report: Dict[str, Any]) -> str:
    """Plain-markdown rendering of the report (no new dependency needed)."""

    overview = report["overview"]
    lines = [
        f"# NetAdapt network report - {report['network_name']}",
        "",
        "> NetAdapt is an educational simulation. It never generates real network traffic.",
        "",
        "## Topology",
        f"- Devices: {report['topology']['devices']} ({', '.join(report['topology']['device_types'])})",
        f"- Links: {report['topology']['links']}",
        f"- Routing algorithm: {report['routing']['algorithm']} ({report['routing']['changes']} change(s))",
        "",
        "## Traffic",
        f"- Sent: {overview['packets_sent']}, delivered: {overview['packets_delivered']}, "
        f"lost: {overview['packets_lost']} (PDR {overview['packet_delivery_ratio']:.1f}%)",
        f"- Average latency: {overview['average_latency_ms']:.2f} ms, throughput {overview['throughput']:.1f} B/s",
        f"- Congestion: {overview['congestion']}, active failures: {overview['active_failures']}",
        "",
        "## QoS",
        f"- Scheduler: {report['qos']['scheduler']}",
        f"- Queue: {report['qos']['queue'].get('packets_enqueued', 0)} enqueued, "
        f"max length {report['qos']['queue'].get('max_queue_length', 0)}",
        "",
        "## Transport",
        f"- TCP connections: {overview['tcp_connections']}, UDP flows: {overview['udp_flows']}",
        "",
        "## Services",
        f"- Running: {overview['services_running']}/{overview['services_total']}",
        f"- Metrics: {report['services']['metrics']}",
        "",
        "## Security",
        f"- Blocked packets: {overview['blocked_packets']}, alerts: {overview['security_alerts']}",
        f"- Firewall rules: {len(report['security']['firewall'])}, ACLs: {len(report['security']['access_lists'])}",
        "",
        "## Failures and recovery",
        f"- Failed nodes: {report['failures']['failed_nodes'] or 'none'}",
        f"- Failed links: {report['failures']['failed_links'] or 'none'}",
        f"- Recovery events: {report['recovery']['recoveries']}",
        "",
        "## Network health",
    ]
    for category in report["health"]["categories"]:
        lines.append(f"### {category['category']} - {category['status']}")
        for rule in category["rules"]:
            lines.append(f"- {rule['reason']}")
    lines.extend(
        [
            "",
            "## Learning and evaluation",
            f"- Challenges: {report['challenges'].get('status', 'not attempted')}",
            f"- Quiz score: {report['quiz'].get('score', {}).get('score_percent', 'n/a')}%",
            "",
        ]
    )
    return "\n".join(lines)


def report_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten the report into rows for CSV/DataFrame export."""

    rows: List[Dict[str, Any]] = []
    for device in report["devices"]:
        rows.append(
            {
                "section": "device",
                "name": device["name"],
                "detail": f"{device['type']} status={device['status']} "
                f"gateway={device['default_gateway']} interfaces={device['interfaces']}",
            }
        )
    for interface in report["interfaces"]:
        rows.append(
            {
                "section": "interface",
                "name": f"{interface['device']}.{interface['interface']}",
                "detail": f"ip={interface['ip']}/{interface['prefix']} mac={interface['mac']} "
                f"status={interface['status']}",
            }
        )
    for entry in report["routing"]["tables"]:
        rows.append(
            {
                "section": "route",
                "name": entry["router"],
                "detail": f"{entry['network']} via {entry['next_hop'] or 'DIRECT'} "
                f"out {entry['interface']} metric {entry['metric']:g}",
            }
        )
    for name, values in report["services"]["metrics"].items():
        for key, value in values.items():
            rows.append({"section": f"service:{name}", "name": key, "detail": value})
    for key, value in report["security"]["metrics"].items():
        rows.append({"section": "security", "name": key, "detail": value})
    for category in report["health"]["categories"]:
        rows.append(
            {
                "section": "health",
                "name": category["category"],
                "detail": f"{category['status']}: " + "; ".join(rule["reason"] for rule in category["rules"]),
            }
        )
    return rows


def report_csv(report: Dict[str, Any]) -> str:
    """CSV text of the report using the standard library only."""

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["section", "name", "detail"])
    writer.writeheader()
    for row in report_rows(report):
        writer.writerow(row)
    return buffer.getvalue()


def report_dataframe(report: Dict[str, Any]):
    """Pandas DataFrame of the report (pandas is already a project dependency)."""

    import pandas as pd

    return pd.DataFrame(report_rows(report))
