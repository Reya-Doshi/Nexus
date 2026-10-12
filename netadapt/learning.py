"""Stage 11 - Learning Mode, protocol step mode, and failure explanation.

Every demonstration in this module runs against a real
:class:`~simulator.NetworkSimulator`.  Nothing here animates pre-written
text: the "steps" a student walks through are the real events emitted by the
existing ``EventLogger`` while the simulator processes real packets over the
real adaptive router and QoS scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from protocols import SwitchFrame
from simulator import NetworkSimulator


# --------------------------------------------------------------------------
# Topic catalog
# --------------------------------------------------------------------------

CATEGORY_ORDER = (
    "NETWORK BASICS",
    "DATA LINK",
    "NETWORK LAYER",
    "TRANSPORT",
    "APPLICATION",
    "SECURITY",
    "QoS",
)


@dataclass
class Topic:
    """One learning topic with a demonstration that drives the simulator."""

    topic_id: str
    category: str
    title: str
    concept: str
    why: str
    how: str
    demonstration: str
    observe: List[str]
    run: Callable[["LearningMode"], Dict[str, Any]]
    diagram: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.topic_id,
            "category": self.category,
            "title": self.title,
            "concept": self.concept,
            "why": self.why,
            "how": self.how,
            "demonstration": self.demonstration,
            "observe": list(self.observe),
            "diagram": self.diagram,
        }


TOPICS: List[Topic] = []


def topic(topic_id, category, title, concept, why, how, demonstration, observe, run, diagram=""):
    TOPICS.append(
        Topic(
            topic_id=topic_id,
            category=category,
            title=title,
            concept=concept,
            why=why,
            how=how,
            demonstration=demonstration,
            observe=observe,
            run=run,
            diagram=diagram,
        )
    )
    return topic_id


TCP_DIAGRAM = """CLIENT                     SERVER
  |                          |
  |------- SYN ------------>|   step 1
  |<------ SYN-ACK ---------|   step 2
  |------- ACK ------------>|   step 3
  |                          |
  |==== ESTABLISHED ========|   data transfer"""


# --------------------------------------------------------------------------
# Learning mode
# --------------------------------------------------------------------------


class LearningMode:
    """Runs topic demonstrations on a real simulator and steps through them."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.simulator = NetworkSimulator(seed=seed)
        self.result: Optional[Dict[str, Any]] = None
        self.steps: List[Dict[str, Any]] = []
        self.cursor = 0
        self.playing = False

    # -- catalog ------------------------------------------------------
    @staticmethod
    def categories() -> List[Dict[str, Any]]:
        return [
            {
                "category": category,
                "topics": [item.to_dict() for item in TOPICS if item.category == category],
            }
            for category in CATEGORY_ORDER
            if any(item.category == category for item in TOPICS)
        ]

    @staticmethod
    def get(topic_id: str) -> Topic:
        for item in TOPICS:
            if item.topic_id == topic_id:
                return item
        raise ValueError(f"Unknown learning topic: {topic_id}")

    # -- demonstration ------------------------------------------------
    def reset(self) -> None:
        """Restore the deterministic starting point used by every topic."""

        self.simulator = NetworkSimulator(seed=self.seed)
        # QoS class priorities are a module-level setting; reset them so a
        # previous topic can never leak configuration into the next one.
        self.simulator.reset_traffic_priorities()
        self.steps = []
        self.cursor = 0
        self.playing = False
        self.result = None

    def run(self, topic_id: str) -> Dict[str, Any]:
        item = self.get(topic_id)
        self.reset()
        summary = item.run(self) or {}
        self.result = {
            "topic": item.to_dict(),
            "summary": summary,
            "steps": self.steps,
            "events": [
                event.to_dict() for event in self.simulator.event_logger.get_events(limit=60)
            ],
            "packets": self._packets(),
            "metrics": self._metrics(),
            "position": 0,
            "total": len(self.steps),
        }
        return self.result

    def _packets(self) -> List[Dict[str, Any]]:
        packets = []
        for packet in self.simulator.packets[-40:]:
            transport = getattr(packet, "transport", None)
            service = getattr(packet, "service", None)
            security = getattr(packet, "security", None)
            row = {
                "id": packet.packet_id,
                "source": packet.source,
                "destination": packet.destination,
                "protocol": getattr(packet, "transport_protocol", "IP"),
                "traffic_class": packet.traffic_type,
                "size": packet.size,
                "status": packet.delivery_status,
                "route": list(packet.route),
                "latency": packet.latency,
                "kind": getattr(packet, "transport_kind", None),
                "flags": list(getattr(packet, "transport_flags", []) or []),
                "service": dict(service) if service else None,
                "security": dict(security) if security else None,
            }
            if transport is not None:
                row.update(
                    {
                        "source_port": transport.source_port,
                        "destination_port": transport.destination_port,
                        "drop_reason": transport.drop_reason,
                    }
                )
            packets.append(row)
        return packets

    def _metrics(self) -> Dict[str, Any]:
        simulator = self.simulator
        metrics = simulator.metrics.calculate(
            simulator.time, simulator.average_congestion(), len(simulator.active_flows)
        )
        return {
            "packets_sent": metrics["packets_sent"],
            "packets_delivered": metrics["packets_delivered"],
            "packets_dropped": metrics["packets_dropped"],
            "packet_delivery_ratio": metrics["packet_delivery_ratio"],
            "average_latency": metrics["average_latency"],
            "throughput": metrics["throughput"],
            "queue": simulator.scheduler_statistics(),
        }

    def _note(self, title: str, detail: Dict[str, Any]) -> None:
        self.steps.append(
            {
                "index": len(self.steps) + 1,
                "title": title,
                "time": round(self.simulator.time, 4),
                **detail,
            }
        )

    def _absorb_events(self, since: int) -> None:
        """Turn real simulator events into walk-through steps."""

        for event in self.simulator.event_logger.events[since:]:
            if event.event_type in {
                "PACKET_GENERATED",
                "PACKET_DELIVERED",
                "PACKET_DROPPED",
                "PACKET_BLOCKED",
                "SERVICE_REQUEST",
            }:
                continue
            self._note(
                str(getattr(event.event_type, "value", event.event_type)),
                {
                    "event": event.event_type,
                    "component": event.component,
                    "message": event.message,
                    "details": {
                        key: value
                        for key, value in event.details.items()
                        if key != "security"
                    },
                    "packet_id": event.details.get("packet_id"),
                    "source": event.details.get("source"),
                    "destination": event.details.get("destination"),
                    "route": event.details.get("route"),
                },
            )

    def advance(self, steps: int = 1) -> Dict[str, Any]:
        self.cursor = max(0, min(len(self.steps), self.cursor + steps))
        return self.state()

    def back(self, steps: int = 1) -> Dict[str, Any]:
        self.cursor = max(0, self.cursor - steps)
        return self.state()

    def reset_steps(self) -> Dict[str, Any]:
        self.cursor = 0
        return self.state()

    def state(self) -> Dict[str, Any]:
        current = self.steps[self.cursor] if self.steps and self.cursor < len(self.steps) else None
        return {
            "result": self.result,
            "steps": self.steps,
            "position": self.cursor,
            "total": len(self.steps),
            "current": current,
            "playing": self.playing,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "categories": self.categories(),
            "step_mode": self.state(),
            "result": self.result,
        }


# --------------------------------------------------------------------------
# Packet journey and failure explanation
# --------------------------------------------------------------------------

_EXPLANATIONS: Dict[str, str] = {
    "FIREWALL_BLOCK": "A simulated firewall rule denied this packet.",
    "ACL_DENY": "An access control list attached to a device on the path denied this packet.",
    "PORT_BLOCKED": "A port filter denied traffic for this protocol and port.",
    "FLOOD_PROTECTION": "Flood protection dropped this attack packet after the traffic rate exceeded the threshold.",
    "ARP_CONFLICT": "One IP address was seen with more than one MAC address in the simulated ARP cache.",
    "NO_ROUTE": "The adaptive router has no available path between the source and the destination.",
    "DESTINATION_UNREACHABLE": "The destination could not be reached: no usable route exists right now.",
    "PACKET_LOSS": "The packet was lost because of the configured packet loss on its route.",
    "CONGESTION": "The packet was dropped because congestion on its route pushed loss probability above the loss roll.",
    "TTL_EXCEEDED": "The packet's TTL reached zero before it reached the destination.",
    "SERVICE_STOPPED": "The service is not running, so the request was refused.",
    "DEVICE_DOWN": "The server device is down, so the service could not answer.",
    "INTERFACE_DOWN": "The server interface is down, so the service could not answer.",
    "CONNECTION_FAILED": "The simulated TCP handshake never completed, so no service exchange could run.",
    "WINDOW_EXHAUSTED": "The receiver window was full, so the segment could not be sent yet.",
    "POOL_EXHAUSTED": "The DHCP pool has no free address left, so the server sent DHCP NAK.",
    "NXDOMAIN": "The DNS server has no A record for that hostname.",
    "RELAY_DENIED": "The SMTP server refused to relay to a domain it does not serve.",
    "FILE_NOT_FOUND": "The FTP server has no such file in its in-memory file store.",
    "SERVER_FAULT": "The HTTP service is configured to return 500 Internal Server Error.",
    "REQUEST_DROPPED": "The HTTP request never arrived, so no response could be produced.",
}


def explain_reason(reason: Optional[str]) -> str:
    if not reason:
        return "The packet was dropped."
    return _EXPLANATIONS.get(str(reason), f"The packet was dropped ({reason}).")


def explain_failure(simulator: Any, packet: Any = None, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Explain a drop or a failed service request from real event data."""

    reason = (result or {}).get("reason")
    details: Dict[str, Any] = {}
    source_event = None
    if result:
        details = {
            key: value
            for key, value in result.items()
            if key in {"service", "client", "server", "protocol", "method", "path",
                       "hostname", "address", "filename", "command", "packet_id"}
        }

    if packet is not None:
        reason = reason or getattr(packet, "security", {}).get("reason")
        for event in reversed(simulator.event_logger.events):
            if event.details.get("packet_id") != packet.packet_id:
                continue
            if event.event_type in {"PACKET_DROPPED", "PACKET_BLOCKED"}:
                reason = reason or event.details.get("reason")
                source_event = event.to_dict()
                details.setdefault("route", list(packet.route))
                break
        if not source_event:
            transport = getattr(packet, "transport", None)
            if transport is not None and transport.drop_reason:
                reason = reason or transport.drop_reason
        security = getattr(packet, "security", None) or {}
        if security:
            details.setdefault("firewall", security.get("firewall"))
            details.setdefault("firewall_rule", security.get("firewall_rule"))
            details.setdefault("acl", security.get("acl"))
            details.setdefault("acl_name", security.get("acl_name"))
        service = getattr(packet, "service", None)
        if service:
            details.setdefault("service_message", service.get("message"))
        details.setdefault("source", packet.source)
        details.setdefault("destination", packet.destination)
        details.setdefault("protocol", getattr(packet, "transport_protocol", "IP"))
        details.setdefault("source_port", getattr(packet, "transport_source_port", None))
        details.setdefault("destination_port", getattr(packet, "transport_destination_port", None))
        details.setdefault("traffic_class", packet.traffic_type)
        details.setdefault("packet_id", packet.packet_id)

    if not reason:
        return {
            "failed": False,
            "reason": None,
            "explanation": "No failure was recorded for this item.",
            "details": details,
        }
    return {
        "failed": True,
        "reason": reason,
        "explanation": explain_reason(reason),
        "details": details,
        "event": source_event,
    }


def _cursor_hop(packet: Any, route: List[str]) -> int:
    """Index of the hop the packet currently sits on."""

    if packet.delivery_status == "PENDING":
        return min(1, len(route) - 1)
    return len(route) - 1


def build_packet_journey(simulator: Any, packet_id: int) -> Dict[str, Any]:
    """Per-hop detail for one real packet, built from the event log."""

    packet = next(
        (item for item in simulator.packets if item.packet_id == int(packet_id)), None
    )
    if packet is None:
        raise ValueError(f"Unknown packet: {packet_id}")
    transport = getattr(packet, "transport", None)
    route = list(packet.route) or [packet.source, packet.destination]
    events = [
        event.to_dict()
        for event in simulator.event_logger.events
        if event.details.get("packet_id") == packet.packet_id
    ]
    drop_reason = next(
        (event["details"].get("reason") for event in reversed(events) if event["details"].get("reason")),
        None,
    )
    security = getattr(packet, "security", None) or {}
    hops: List[Dict[str, Any]] = []
    for index, device in enumerate(route):
        if index == 0:
            action = "SENT"
        elif index == len(route) - 1:
            action = "DELIVERED" if packet.delivery_status == "DELIVERED" else "DROPPED"
        else:
            action = "FORWARDED"
        interface = "—"
        for u, v in zip(route, route[1:]):
            if device not in (u, v):
                continue
            if not simulator.topology.graph.has_edge(u, v):
                continue
            edge = simulator.topology.graph[u][v]
            interface = edge.get("interface_b") if device == u else edge.get("interface_a")
            break
        hop_events = [event for event in events if event.get("component") in (device, None)]
        hops.append(
            {
                "hop": index,
                "device": device,
                "interface": interface or "—",
                "action": action,
                "timestamp": events[-1]["time"] if events else packet.creation_time,
                "queue_wait_ms": round((packet.queue_wait_time or 0.0) * 1000, 3)
                if index == 0
                else None,
                "queue_length": simulator.scheduler.stats.current_length if index == 0 else None,
                "latency_ms": round(packet.latency * 1000, 3)
                if (index == len(route) - 1 and packet.latency is not None)
                else None,
                "events": [event["event"] for event in hop_events],
                "result": "OK" if action != "DROPPED" else (drop_reason or "DROPPED"),
            }
        )
    return {
        "packet_id": packet.packet_id,
        "protocol": getattr(packet, "transport_protocol", "IP"),
        "source": packet.source,
        "destination": packet.destination,
        "source_port": getattr(packet, "transport_source_port", None)
        or (transport.source_port if transport else None),
        "destination_port": getattr(packet, "transport_destination_port", None)
        or (transport.destination_port if transport else None),
        "size": packet.size,
        "traffic_class": packet.traffic_type,
        "status": packet.delivery_status,
        "route": route,
        "hops": hops,
        "current_device": route[min(_cursor_hop(packet, route), len(route) - 1)],
        "drop_reason": drop_reason or (security.get("reason") if security else None),
        "security": dict(security) if security else None,
        "service": dict(getattr(packet, "service", {}) or {}) or None,
        "latency": packet.latency,
    }

    @staticmethod
    def _unused() -> None:
        return None


# --------------------------------------------------------------------------
# Topic definitions (each demonstration runs the real simulator)
# --------------------------------------------------------------------------


def _demo_basic_ping(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    result = simulator.diagnostic_ping("H1", "H3", count=3)
    mode._absorb_events(since)
    return {
        "success": result["success"],
        "route": result["path"],
        "rtt_ms": result["rtt_ms"],
        "sent": result["sent"],
        "received": result["received"],
        "loss_percent": result["loss_percent"],
    }


def _demo_arp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    entry = simulator.arp_lookup("H1", "192.168.1.3")
    again = simulator.arp_lookup("H1", "192.168.1.3")
    mode._absorb_events(since)
    return {
        "entry": entry.to_dict() if entry else None,
        "second_lookup_was_cache_hit": again is not None and again.mac_address == entry.mac_address,
        "table": simulator.arp_table("H1"),
    }


def _ensure_switch(simulator: Any) -> str:
    """Add a real switch to the default topology for the data-link demos."""

    if "SW1" not in simulator.topology.devices:
        simulator.topology.add_node("SW1", "switch")
        simulator.topology.add_link("H1", "SW1", latency=2.0, bandwidth=100.0)
        simulator.topology.add_link("H2", "SW1", latency=2.0, bandwidth=100.0)
    return "SW1"


def _demo_switching(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    switch = _ensure_switch(simulator)
    known = simulator.get_device("H2").interfaces[0].mac_address
    results = []
    for destination in (known, "00:00:00:00:00:99"):
        frame = SwitchFrame(
            source_mac=simulator.get_device("H1").interfaces[0].mac_address,
            destination_mac=destination,
            ingress_interface="eth0",
            payload="learning-demo",
        )
        results.append(simulator.switch_frame(switch, frame).to_dict())
    return {"switch": switch, "frames": results, "mac_table": simulator.mac_table(switch)}


def _demo_routing(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    path, cost, status = simulator.get_current_route("H1", "H4")
    since = len(simulator.event_logger.events)
    simulator.fail_link("R4", "R6")
    simulator.tick(1.0)
    mode._absorb_events(since)
    new_path, new_cost, _ = simulator.get_current_route("H1", "H4")
    return {
        "algorithm": simulator.get_router_algorithm(),
        "route": path,
        "cost": cost,
        "status": status,
        "after_failure": new_path,
        "after_cost": new_cost,
    }


def _demo_icmp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    result = simulator.diagnostic_ping("H1", "H4", count=2, ttl=64)
    mode._absorb_events(since)
    return {
        "success": result["success"],
        "rtt_ms": result["rtt_ms"],
        "path": result["path"],
        "sent": result["sent"],
        "received": result["received"],
    }


def _demo_ttl(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    result = simulator.traceroute("H1", "H4")
    mode._absorb_events(since)
    return {"hops": result["hops"], "path": result["path"]}


def _demo_tcp_handshake(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    connection = simulator.create_tcp_connection("H1", "H4", 5000, 80, traffic_class="HTTP")
    simulator.run_until_empty()
    mode._absorb_events(since)
    for packet in connection.packets:
        mode._note(
            f"{packet.protocol} {'+'.join(packet.flags) or packet.kind}",
            {
                "event": "TCP_PACKET",
                "source": packet.source,
                "destination": packet.destination,
                "route": list(packet.route),
                "packet_id": packet.packet_id,
                "message": f"{packet.source}:{packet.source_port} -> "
                f"{packet.destination}:{packet.destination_port} [{' '.join(packet.flags)}]",
            },
        )
    return {
        "state": connection.state.value,
        "packets": [
            {
                "flags": packet.flags,
                "source": packet.source,
                "destination": packet.destination,
                "status": packet.status,
                "latency": packet.latency,
            }
            for packet in connection.packets
        ],
    }


def _demo_tcp_data(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    connection = simulator.create_tcp_connection("H1", "H4", 5000, 80, receiver_window=8)
    simulator.run_until_empty()
    since = len(simulator.event_logger.events)
    simulator.send_tcp_data(connection.flow_id, 4, 1000)
    simulator.run_until_empty()
    mode._absorb_events(since)
    return {
        "state": connection.state.value,
        "cwnd": round(connection.cwnd, 2),
        "ack_count": connection.ack_count,
        "bytes": connection.bytes_transferred,
    }


def _demo_retransmission(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.set_link_conditions("R1", "R2", packet_loss=0.6)
    connection = simulator.create_tcp_connection("H1", "H4", 5000, 80, timeout=0.05)
    simulator.run_until_empty()
    since = len(simulator.event_logger.events)
    simulator.send_tcp_data(connection.flow_id, 2, 1000)
    simulator.run_until_empty()
    simulator.process_transport_tick(simulator.time + 0.2)
    simulator.run_until_empty()
    mode._absorb_events(since)
    return {
        "retransmissions": connection.retransmission_count,
        "timeouts": connection.timeout_count,
        "delivered": connection.data_packets_delivered,
        "lost": connection.data_packets_lost,
    }


def _demo_congestion(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.set_scheduler("priority")
    simulator.set_link_conditions("R1", "R3", congestion=0.8, bandwidth=5.0)
    since = len(simulator.event_logger.events)
    simulator.create_flow("H1", "H4", "VoIP", 6, 512, 20, 2.0)
    simulator.create_flow("H1", "H4", "FTP", 6, 1400, 20, 2.0)
    simulator.run_until_empty()
    mode._absorb_events(since)
    return {
        "queue": simulator.scheduler_statistics(),
        "class_metrics": simulator.class_metrics(),
        "congestion": simulator.average_congestion(),
    }


def _demo_dhcp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("DHCP", "H3")
    since = len(simulator.event_logger.events)
    result = simulator.dhcp_acquire("H1", "H3")
    mode._absorb_events(since)
    return {
        "steps": result["steps"],
        "address": result.get("address"),
        "gateway": result.get("gateway"),
        "interface": simulator.get_device("H1").interfaces[0].to_dict(),
    }


def _demo_dns(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("DNS", "H3")
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3", ttl=60)
    since = len(simulator.event_logger.events)
    first = simulator.dns_query("H1", "server.netadapt.local", "H3")
    second = simulator.dns_query("H1", "server.netadapt.local", "H3")
    mode._absorb_events(since)
    return {"server_answer": first, "cache_answer": second}


def _demo_http(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("HTTP", "H3")
    since = len(simulator.event_logger.events)
    ok = simulator.http_request("H1", "H3")
    missing = simulator.http_request("H1", "H3", path="/missing")
    mode._absorb_events(since)
    return {"ok": {k: ok.get(k) for k in ("status", "response", "latency")},
            "missing": {k: missing.get(k) for k in ("status", "response", "reason")}}


def _demo_ftp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("FTP", "H3")
    since = len(simulator.event_logger.events)
    listing = simulator.ftp_command("H1", "H3", "LIST")
    uploaded = simulator.ftp_command("H1", "H3", "PUT", "lab.txt", "hello netadapt")
    mode._absorb_events(since)
    return {
        "listing": listing.get("listing"),
        "put": {"success": uploaded.get("success"), "bytes": uploaded.get("bytes")},
    }


def _demo_smtp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("SMTP", "H3")
    since = len(simulator.event_logger.events)
    result = simulator.smtp_send("H1", "H3", recipient="server@h3.netadapt.local")
    mode._absorb_events(since)
    return {
        "delivered": result.get("delivered"),
        "transcript": [item["client"] for item in result.get("transcript", [])],
    }


def _demo_firewall(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("HTTP", "H3")
    allowed = simulator.http_request("H1", "H3")
    simulator.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    since = len(simulator.event_logger.events)
    blocked = simulator.http_request("H1", "H3")
    mode._absorb_events(since)
    return {
        "before": allowed.get("status"),
        "after": {"success": blocked.get("success"), "reason": blocked.get("reason")},
        "rules": simulator.security_state()["firewall"]["rules"],
    }


def _demo_acl(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("HTTP", "H3")
    simulator.create_acl("NO-WEB", "EXTENDED")
    simulator.add_acl_entry(
        "NO-WEB",
        action="DENY",
        source_ip="192.168.1.1",
        destination_ip="192.168.1.3",
        protocol="TCP",
        destination_port=80,
    )
    simulator.attach_acl("NO-WEB", "R5")
    since = len(simulator.event_logger.events)
    blocked = simulator.http_request("H1", "H3")
    allowed = simulator.http_request("H2", "H3")
    mode._absorb_events(since)
    return {
        "denied": {"success": blocked.get("success"), "reason": blocked.get("reason")},
        "other_client": allowed.get("status"),
    }


def _demo_arp_spoof(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    legitimate = simulator.get_device("H3").interfaces[0].mac_address
    simulator.services.arp_learn("H1", "192.168.1.3", legitimate)
    since = len(simulator.event_logger.events)
    result = simulator.services.arp_spoof("H2", "H1", "192.168.1.3", detect=True)
    mode._absorb_events(since)
    return {
        "spoof": result,
        "cache": [entry.to_dict() for entry in simulator.arp.lookup_all("H1")]
        if hasattr(simulator.arp, "lookup_all")
        else simulator.arp_table("H1"),
    }


def _demo_flood(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    since = len(simulator.event_logger.events)
    scenario = simulator.start_flood("H1", "H4", protocol="UDP", rate=200, duration=1.0, threshold=25)
    mode._absorb_events(since)
    return {"scenario": scenario, "security": simulator.security_state()["metrics"]}


def _demo_scheduler(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    results = {}
    for scheduler in ("fifo", "priority", "wfq"):
        simulator.reset()
        simulator.set_scheduler(scheduler)
        simulator.set_link_conditions("R1", "R3", bandwidth=2.0, congestion=0.4)
        simulator.create_flow("H1", "H4", "VoIP", 4, 512, 30, 2.0)
        simulator.create_flow("H1", "H4", "FTP", 4, 1400, 30, 2.0)
        simulator.run_until_empty()
        results[scheduler] = simulator.scheduler_class_statistics()
    simulator.set_scheduler("priority")
    simulator.reset()
    return {"comparison": results}


def _demo_self_healing(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    before = simulator.get_current_route("H1", "H4")[0]
    since = len(simulator.event_logger.events)
    simulator.fail_node("R2")
    simulator.tick(4.0)
    after = simulator.get_current_route("H1", "H4")[0]
    mode._absorb_events(since)
    return {"route_before": before, "route_after": after,
            "failures": simulator.get_failed_nodes() + [f"{u}-{v}" for u, v in simulator.get_failed_links()]}


def _demo_switch_failure(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.fail_node("R1")
    simulator.tick(4.0)
    result = simulator.diagnostic_ping("H2", "H4", count=1)
    simulator.recover_node("R1")
    simulator.tick(4.0)
    recovered = simulator.diagnostic_ping("H2", "H4", count=1)
    return {
        "during_failure": {"success": result["success"], "reason": result["reason"]},
        "after_recovery": {"success": recovered["success"], "reason": recovered["reason"]},
    }


def _demo_ports(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.install_service("HTTP", "H3")
    connection = simulator.create_tcp_connection("H1", "H3", 49152, 80, traffic_class="HTTP")
    simulator.run_until_empty()
    identification = []
    for packet in simulator.packets:
        if getattr(packet, "transport", None) is None:
            continue
        transport = packet.transport
        identification.append(
            {
                "protocol": transport.protocol,
                "source": f"{transport.source}:{transport.source_port}",
                "destination": f"{transport.destination}:{transport.destination_port}",
                "service": getattr(packet, "service_name", None)
                or {
                    ("TCP", 80): "HTTP",
                    ("UDP", 53): "DNS",
                    ("TCP", 21): "FTP",
                    ("TCP", 25): "SMTP",
                }.get((transport.protocol, transport.destination_port)),
            }
        )
    return {"connection": connection.flow_id, "packets": identification}


def _demo_subnetting(mode: LearningMode) -> Dict[str, Any]:
    from addressing import subnet_details

    rows = []
    for name in ("H1", "H2", "H3", "R1"):
        interface = mode.simulator.get_device(name).interfaces[0]
        rows.append({"device": name, **subnet_details(interface.ip_address, interface.prefix)})
    same = rows[0]["network"] == rows[1]["network"]
    return {
        "assignments": rows,
        "H1_H2_same_subnet": same,
        "H1_R1_same_subnet": rows[0]["network"] == rows[3]["network"],
    }


def _demo_congestion_alert(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    simulator.set_link_conditions("R1", "R3", congestion=0.9, packet_loss=0.3)
    since = len(simulator.event_logger.events)
    simulator.create_flow("H1", "H4", "Video", 8, 1000, 25, 1.0)
    simulator.run_until_empty()
    mode._absorb_events(since)
    return {
        "congestion": simulator.average_congestion(),
        "queue": simulator.scheduler_statistics(),
        "metrics": simulator.metrics.calculate(simulator.time, simulator.average_congestion(), 0),
    }


def _demo_mac_learning(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    switch = _ensure_switch(simulator)
    results = []
    for source in ("H1", "H2"):
        frame = SwitchFrame(
            source_mac=simulator.get_device(source).interfaces[0].mac_address,
            destination_mac="ff:ff:ff:ff:ff:ff",
            ingress_interface="eth0",
            payload="learning",
        )
        results.append(simulator.switch_frame(switch, frame).to_dict())
    return {"switch": switch, "frames": results, "mac_table": simulator.mac_table(switch)}


def _demo_udp(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    flow = simulator.create_udp_flow("H1", "H3", 5001, 53, payload_size=400, traffic_class="VoIP")
    since = len(simulator.event_logger.events)
    simulator.send_udp_data(flow.flow_id, 4, 400)
    simulator.run_until_empty()
    mode._absorb_events(since)
    return {
        "flow_id": flow.flow_id,
        "packets_sent": len(flow.packets),
        "delivered": flow.packets_delivered,
        "bytes": flow.bytes_delivered,
    }


def _demo_bellman_ford(mode: LearningMode) -> Dict[str, Any]:
    simulator = mode.simulator
    dijkstra = simulator.get_current_route("H1", "H4")[0]
    simulator.set_router_algorithm("bellman-ford")
    bellman = simulator.get_current_route("H1", "H4")[0]
    simulator.set_router_algorithm("dijkstra")
    return {"dijkstra": dijkstra, "bellman_ford": bellman, "match": dijkstra == bellman}


# -- catalog ------------------------------------------------------------

topic("osi", "NETWORK BASICS", "OSI and TCP/IP models",
      "The OSI model splits networking into seven layers; TCP/IP folds them into four.",
      "Layers let engineers reason about where a problem lives and which tools apply.",
      "Each NetAdapt layer maps to real behaviour: link conditions, routing, TCP/UDP, and the Stage 10 services.",
      "Run a ping (ICMP, network layer) and an HTTP request (application layer) and compare the packets produced.",
      ["Which layer produced each packet?", "What does the application layer add to the IP packet?"],
      lambda mode: {
          "icmp": _demo_basic_ping(mode),
          "application": _demo_http(mode),
      })

topic("devices", "NETWORK BASICS", "Network devices",
      "Hosts, switches, and routers each perform a different function.",
      "Confusing device roles is the root cause of many design mistakes.",
      "Switches forward by MAC, routers forward by IP, hosts originate traffic.",
      "Inspect the same topology: a host ping, a switch MAC table, and a router route table.",
      ["Which device learns MAC addresses?", "Which device selects a path?"],
      lambda mode: {
          "mac_table": _demo_mac_learning(mode),
          "route": _demo_routing(mode),
      })

topic("ip_addressing", "NETWORK BASICS", "IP addressing",
      "Every interface needs a unique IPv4 address to be reachable.",
      "Duplicate or missing addresses break forwarding immediately.",
      "NetAdapt stores the address on the real NetworkInterface of each device.",
      "Compare interface addresses and confirm reachability with a ping.",
      ["Which interfaces share a subnet?", "What changes when an address is removed?"],
      _demo_subnetting)

topic("subnetting", "NETWORK BASICS", "Subnetting",
      "A prefix length splits a network into smaller segments.",
      "Segmentation controls broadcast scope and which hosts can talk directly.",
      "NetAdapt uses the prefix for interface addressing and route lookups.",
      "Show the computed network, mask, and broadcast for each device interface.",
      ["Are H1 and H2 in the same subnet?", "Which router interface shares a subnet with H1?"],
      _demo_subnetting)

topic("mac_addresses", "NETWORK BASICS", "MAC addresses",
      "A MAC address identifies an interface on the local link.",
      "Switches and ARP both work with MAC addresses, not IP addresses.",
      "Every NetAdapt interface has a deterministic MAC used by ARP and switching.",
      "Run ARP resolution and read the MAC learned for the target address.",
      ["Which MAC belongs to the target?", "Does the MAC change when the IP changes?"],
      _demo_arp)

topic("ports", "NETWORK BASICS", "Ports",
      "A port number identifies the application endpoint on a host.",
      "Ports let one IP host run many services at once.",
      "Service ports are 53/UDP, 80/TCP, 21/TCP, 25/TCP, 67-68/UDP.",
      "Open a TCP connection to port 80 and read protocol, ports, and service from the packets.",
      ["How is the service identified from the port?", "What is the client's ephemeral port?"],
      _demo_ports)

topic("ethernet", "DATA LINK", "Ethernet frames",
      "Ethernet moves frames between interfaces on one link.",
      "It is the delivery mechanism below IP.",
      "NetAdapt models link status, bandwidth, and latency at this layer.",
      "Degrade one link and observe delivery ratio and latency changes.",
      ["Which link is the bottleneck?", "How does bandwidth affect latency?"],
      lambda mode: _demo_congestion_alert(mode))

topic("mac_learning", "DATA LINK", "MAC learning and switching",
      "A switch learns which port a MAC lives on from the source address.",
      "Without learning, a switch would flood frames to every port.",
      "NetAdapt's switch table is populated by real forwarded frames.",
      "Send frames through SW1 and read the learned MAC table.",
      ["Which MACs were learned?", "On which interface?"],
      _demo_mac_learning)

topic("arp", "DATA LINK", "ARP",
      "ARP maps an IPv4 address to a MAC address on the local link.",
      "A host cannot send an IP packet without a next-hop MAC.",
      "ARP request/reply and cache expiry are real, with per-device caches.",
      "Resolve 192.168.1.3 from H1 and inspect H1's ARP table.",
      ["Was it a cache hit or a miss?", "When does the entry expire?"],
      _demo_arp)

topic("switching", "DATA LINK", "Switching",
      "Switching forwards frames toward the destination port.",
      "It connects hosts without involving layer 3.",
      "Unknown destinations are flooded, known ones are forwarded.",
      "Send a frame with a known and an unknown destination MAC.",
      ["Which entry was learned?", "Was the frame flooded?"],
      _demo_switching)

topic("ipv4", "NETWORK LAYER", "IPv4 and forwarding",
      "IPv4 packets carry a source, destination, TTL, and route.",
      "Layer 3 is where end-to-end addressing and routing live.",
      "Every NetAdapt packet is routed by the adaptive router before delivery.",
      "Send traffic and read the route and per-hop forwarding.",
      ["What is the current route?", "Which device forwards the packet?"],
      _demo_ttl)

topic("routing", "NETWORK LAYER", "Routing tables",
      "A routing table maps destination networks to next hops.",
      "Routers need it to forward packets toward a destination.",
      "NetAdapt builds routing tables from the live topology.",
      "Read the routing table and confirm it matches the selected route.",
      ["What is the next hop?", "Which interface exits the router?"],
      _demo_routing)

topic("dijkstra", "NETWORK LAYER", "Dijkstra shortest path",
      "Dijkstra finds the cheapest path from a weighted graph.",
      "Cheap paths keep latency and cost low.",
      "NetAdapt implements it directly (no networkx helper) over weighted links.",
      "Compare the chosen route with the link costs.",
      ["Which links dominate the cost?", "What is the total cost?"],
      _demo_routing)

topic("bellman_ford", "NETWORK LAYER", "Bellman-Ford",
      "Bellman-Ford handles negative weights and counts hops.",
      "It is a robust alternative to Dijkstra.",
      "Switching the algorithm recomputes routes on the same topology.",
      "Run both algorithms and compare the resulting paths.",
      ["Do both algorithms agree?", "Where do they differ?"],
      _demo_bellman_ford)

topic("icmp", "NETWORK LAYER", "ICMP and ping",
      "ICMP echo request/reply measures reachability and RTT.",
      "It is the fastest way to test a path.",
      "Ping runs a real multi-packet exchange over the real route.",
      "Run a 3-packet ping and read the RTT statistics.",
      ["What is the average RTT?", "How many hops were used?"],
      _demo_icmp)

topic("ttl", "NETWORK LAYER", "TTL and traceroute",
      "TTL bounds how many routers a packet may cross.",
      "It prevents packets from looping forever.",
      "Reducing TTL makes each router reply with TTL exceeded.",
      "Run traceroute and read the discovered hop list.",
      ["Which hops answered?", "What was the final path?"],
      _demo_ttl)

topic("self_healing", "NETWORK LAYER", "Self-healing routing",
      "When a component fails, the router recomputes routes and traffic moves.",
      "Availability depends on having an alternative path.",
      "Heartbeats detect failure and the adaptive router recalculates.",
      "Fail R2 and watch the route and traffic change.",
      ["What was the route before?", "What replaced the failed component?"],
      _demo_self_healing)

topic("switch_failure", "NETWORK LAYER", "Node failure and recovery",
      "A failed node removes it from every route until it recovers.",
      "Node failures are common in real networks.",
      "Fail R1, observe impact, recover it, and observe restoration.",
      "Ping before, during, and after the failure.",
      ["Was the destination reachable during the failure?", "Did recovery restore it?"],
      _demo_switch_failure)

topic("tcp", "TRANSPORT", "TCP",
      "TCP provides a reliable, ordered byte stream.",
      "Applications need reliability that IP does not provide.",
      "Handshake, ACKs, window, and congestion control are all simulated.",
      "Open a connection, send data, and read the connection state.",
      ["What is the connection state?", "How many ACKs were generated?"],
      _demo_tcp_data)

topic("tcp_handshake", "TRANSPORT", "TCP three-way handshake",
      "TCP establishes a connection with SYN, SYN-ACK, ACK before data.",
      "Both sides must agree on starting sequence numbers.",
      "The simulator enqueues each segment as a real routed packet.",
      "Watch the three segments travel and the state become ESTABLISHED.",
      ["Which packet starts the exchange?", "When is the connection ESTABLISHED?"],
      _demo_tcp_handshake,
      diagram=TCP_DIAGRAM)

topic("udp", "TRANSPORT", "UDP",
      "UDP sends datagrams without connection setup or retransmission.",
      "Voice and DNS prefer low latency over reliability.",
      "A UDP flow is a real series of routed packets with its own metrics.",
      "Send a short UDP burst and read delivery and byte counters.",
      ["How many datagrams were delivered?", "What is the average latency?"],
      _demo_udp)

topic("sequence_ack", "TRANSPORT", "Sequence numbers and ACKs",
      "TCP numbers bytes and acknowledges the next expected byte.",
      "Numbers make loss and ordering detectable.",
      "Cumulative ACKs free the sliding window and grow cwnd.",
      "Send several segments and read sequence numbers and ACK values.",
      ["What was acknowledged?", "How did cwnd change?"],
      _demo_tcp_data)

topic("retransmission", "TRANSPORT", "Retransmission and timeouts",
      "A missing ACK triggers a timeout and a retransmission.",
      "Loss must be repaired or the transfer stalls.",
      "Timeouts and retransmissions use the measured RTO of the connection.",
      "Inject 60% loss, send data, and advance the clock.",
      ["How many retransmissions occurred?", "Was the data still delivered?"],
      _demo_retransmission)

topic("sliding_window", "TRANSPORT", "Sliding window and flow control",
      "The window limits how much unacknowledged data may be in flight.",
      "It protects the receiver and the network.",
      "The receiver window and cwnd both limit outstanding segments.",
      "Send data in a small window and count packets in flight.",
      ["How many segments were in flight?", "When did the window reopen?"],
      _demo_tcp_data)

topic("congestion_control", "TRANSPORT", "Congestion control",
      "cwnd grows slowly on loss and quickly otherwise.",
      "It adapts the send rate to the path.",
      "Slow start and congestion avoidance are logged as state changes.",
      "Read cwnd growth and the congestion phase over a transfer.",
      ["Which phase was active?", "How did cwnd change?"],
      _demo_tcp_data)

topic("dhcp", "APPLICATION", "DHCP",
      "DHCP hands an address, mask, gateway, and DNS to a client automatically.",
      "Manual addressing is error prone at scale.",
      "DISCOVER/OFFER/REQUEST/ACK run as real UDP traffic and configure the interface.",
      "Run a full DORA exchange and read the resulting interface configuration.",
      ["Which address was offered?", "What gateway was configured?"],
      _demo_dhcp)

topic("dns", "APPLICATION", "DNS",
      "DNS translates a hostname into an IPv4 address.",
      "Users and applications work with names, not addresses.",
      "Queries and responses are real UDP packets with a TTL cache.",
      "Resolve a hostname twice: once from the server, once from the cache.",
      ["What answered the first query?", "What answered the second?"],
      _demo_dns)

topic("http", "APPLICATION", "HTTP",
      "HTTP is a request/response protocol over TCP.",
      "It is the most common application protocol.",
      "GET/POST produce real TCP requests and responses with status codes.",
      "Request a valid path, then a missing path to see 200 and 404.",
      ["What status was returned?", "How long did the response take?"],
      _demo_http)

topic("ftp", "APPLICATION", "FTP",
      "FTP transfers files over a TCP control connection.",
      "Bulk transfer needs its own protocol semantics.",
      "LIST/GET/PUT use an in-memory file store; the real disk is untouched.",
      "List the store, then upload a file and list again.",
      ["Which files were listed?", "How many bytes were transferred?"],
      _demo_ftp)

topic("smtp", "APPLICATION", "SMTP",
      "SMTP relays a message from a sender to a recipient.",
      "Mail needs a store-and-forward conversation.",
      "HELO/MAIL FROM/RCPT TO/DATA/QUIT run over simulated TCP.",
      "Send one message and read the full transcript.",
      ["Which commands were exchanged?", "Was the message delivered?"],
      _demo_smtp)

topic("firewall", "SECURITY", "Firewall",
      "A firewall allows or denies traffic by rule.",
      "It is the first line of network defence.",
      "Rules match source, destination, protocol, and ports before delivery.",
      "Request HTTP, deny port 80, then request again.",
      ["Why was the second request blocked?", "Which rule matched?"],
      _demo_firewall)

topic("acl", "SECURITY", "Access control lists",
      "An ACL filters traffic on an interface.",
      "It provides per-interface, per-direction control.",
      "Standard ACLs match source; extended ACLs match destination, protocol, and port.",
      "Deny one client to port 80 on a router and show another client still works.",
      ["Which ACL entry matched?", "Did the other client succeed?"],
      _demo_acl)

topic("arp_spoofing", "SECURITY", "ARP spoofing simulation",
      "An attacker forges ARP replies to redirect traffic through itself.",
      "It is the classic man-in-the-middle attack.",
      "This is a controlled simulation inside NetAdapt only; it never touches a real network.",
      "Poison a victim's simulated cache and read the detected conflict.",
      ["Which IP was poisoned?", "Which MAC was claimed?"],
      _demo_arp_spoof)

topic("flood_detection", "SECURITY", "Flood detection",
      "A traffic flood is a sudden, abnormal arrival rate.",
      "Detection lets a network react before it collapses.",
      "Arrival rates are analysed in one-second windows against a threshold.",
      "Generate a controlled flood above the threshold and read the detection events.",
      ["What rate was observed?", "What was the threshold?"],
      _demo_flood)

topic("packet_filtering", "SECURITY", "Port filtering",
      "Port filtering allows or denies a protocol and port pair.",
      "It blocks unwanted services simply.",
      "Port rules report the PORT_BLOCKED drop reason.",
      "Filter a port and observe the drop reason on the request.",
      ["What was the drop reason?", "Which rule matched?"],
      lambda mode: _demo_firewall(mode))

topic("fifo", "QoS", "FIFO queue",
      "FIFO serves packets in arrival order regardless of class.",
      "It is the simplest baseline scheduler.",
      "All classes share one queue in arrival order.",
      "Run the same mixed workload under FIFO and read per-class waiting time.",
      ["Which class waited longest?", "What was the maximum queue length?"],
      _demo_scheduler)

topic("priority", "QoS", "Priority queue",
      "Priority serving delivers high-priority classes first.",
      "Voice and video degrade first under load without it.",
      "Class priorities are live-configurable in NetAdapt.",
      "Run the mixed workload under the priority scheduler and compare classes.",
      ["Which class was served first?", "How did its latency compare?"],
      _demo_scheduler)

topic("wfq", "QoS", "Weighted fair queueing",
      "WFQ shares bandwidth between classes by weight.",
      "It prevents one class from starving the others.",
      "Weights and service credits control the long-run share.",
      "Run the mixed workload under WFQ and read each class's service share.",
      ["Which class got the largest share?", "Did any class starve?"],
      _demo_scheduler)

topic("congestion", "QoS", "Congestion",
      "Congestion is demand exceeding available link capacity.",
      "It causes queue growth, latency, and loss.",
      "Link bandwidth and congestion directly affect transmission time and loss.",
      "Create a congested link and measure queue growth and loss.",
      ["How long did packets wait?", "What was the delivery ratio?"],
      _demo_congestion)

topic("traffic_prioritization", "QoS", "Traffic prioritization",
      "Different traffic types deserve different service.",
      "A VoIP call and a file download should not share the same queue.",
      "DNS/DHCP use Emergency, HTTP/SMTP use HTTP, FTP uses the bulk class.",
      "Send DNS, HTTP, and FTP traffic and read each class's queue statistics.",
      ["Which class had the shortest wait?", "Which class transferred the most bytes?"],
      _demo_congestion)
