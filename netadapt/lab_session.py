"""Stateful backend session for the interactive NetAdapt network laboratory.

The browser owns presentation and pointer interaction. This module owns the
actual editable topology and delegates routing, QoS, self-healing, protocol,
and metric behavior to the existing :class:`NetworkSimulator`.
"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
import json
import re

from challenges import ChallengeEngine
from demos import DemoEngine
from devices import NetworkInterface
from learning import (
    LearningMode,
    TOPICS as LEARNING_TOPICS,
    build_packet_journey,
    explain_failure,
)
from quiz import QuizEngine
from report import (
    network_health,
    network_overview,
    network_report,
    report_csv,
    report_rows,
    report_to_markdown,
)
from services_security import (
    DROP_ACL,
    DROP_ARP,
    DROP_FIREWALL,
    DROP_FLOOD,
    DROP_PORT,
    SERVICE_NAMES,
    SERVICE_PORTS,
    normalise_service,
)
from simulator import NetworkSimulator
from topology import NetworkTopology


PALETTE_TYPES = (
    "host", "pc", "laptop", "server", "printer", "router", "switch", "hub",
    "access_point", "cloud", "internet",
)
TRAFFIC_TYPES = ("Emergency", "VoIP", "Video", "HTTP", "FTP")

#: Static learning catalog (built once, reused by every session).
LEARNING_CATEGORIES = LearningMode.categories()

CLI_HELP: Dict[str, str] = {
    "ping": "ping <ip> - ICMP echo request to an IP address; reports RTT and the path taken.",
    "tracert": "tracert <ip> - hop-by-hop discovery using a limited TTL.",
    "arp": "arp -a - show the simulated ARP cache of every device.",
    "ipconfig": "ipconfig [/all|/renew|/release] - interface configuration, DHCP lease control.",
    "route": "route print - show every router table plus host default gateways.",
    "netstat": "netstat - show the simulated TCP connections and UDP flows.",
    "nslookup": "nslookup <hostname> - resolve a name through the simulated DNS service.",
    "show": "show interfaces|ip route|services|service <name>|firewall|access-lists|arp|"
    "connections|security|running-config|mac address-table|vlan brief.",
    "routing": "Routing: the adaptive router (Dijkstra or Bellman-Ford) recomputes paths when a "
    "link or node fails. 'show ip route' shows the live table.",
    "tcp": "TCP: SYN, SYN-ACK, ACK handshake, then ACKed segments with a sliding window, "
    "timeouts, retransmissions, and congestion control. 'netstat' shows the connections.",
    "dns": "DNS: A records on a simulated DNS server, queries over UDP/53, and a per-client "
    "cache with TTL. 'nslookup <name>' resolves a hostname.",
    "services": "Services: DHCP, DNS, HTTP, FTP, and SMTP are hosted by real devices. "
    "'show services' and 'show service <name>' report their state.",
    "security": "Security: the simulated firewall, interface ACLs, ARP spoofing detection, and "
    "flood detection. 'show firewall', 'show access-lists', 'show security'.",
}


class LabError(ValueError):
    """An invalid editor operation that can be returned to the frontend."""


class LabSession:
    """One user's editable lab and its live Python simulation."""

    def __init__(self, seed: int = 42) -> None:
        self.lock = RLock()
        self.seed = seed
        self.document: Dict[str, Any] = {}
        self.history: List[Dict[str, Any]] = []
        self.future: List[Dict[str, Any]] = []
        self.running = False
        self.speed = 1.0
        self.last_packet: Optional[Dict[str, Any]] = None
        self.diagnostic_result: Optional[Dict[str, Any]] = None
        self.transport_result: Optional[Dict[str, Any]] = None
        self.service_result: Optional[Dict[str, Any]] = None
        self.security_result: Optional[Dict[str, Any]] = None
        # Stage 11 learning / challenge / quiz / demo state
        self.challenge_answers: Dict[str, Dict[str, str]] = {}
        self.challenge_marks: Dict[str, float] = {}
        self.learning_result: Optional[Dict[str, Any]] = None
        self.quiz_answers: Dict[str, Any] = {}
        self.demo_result: Optional[Dict[str, Any]] = None
        self.step_mode: Optional[Dict[str, Any]] = None
        self.learning_catalog = LEARNING_CATEGORIES
        self.learning = LearningMode(seed=seed)
        self.challenges = ChallengeEngine(self)
        self.quiz = QuizEngine(self)
        self.demos = DemoEngine(self)
        self.challenge_state: Optional[Dict[str, Any]] = None
        self.quiz_state: Optional[Dict[str, Any]] = None
        self.last_journey: Optional[Dict[str, Any]] = None
        self.last_explanation: Optional[Dict[str, Any]] = None
        self._counter = 1
        self.simulator: NetworkSimulator
        self.load_preset("self_healing", record=False)

    # ------------------------------------------------------------------
    # Topology construction and serialization
    # ------------------------------------------------------------------
    @staticmethod
    def _new_id(prefix: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_]", "", prefix) or "Device"
        return f"{value}{LabSession._next_suffix(value, prefix)}"

    @staticmethod
    def _next_suffix(prefix: str, fallback: str) -> int:
        # The caller owns the counter; this helper is replaced by _unique_name.
        return 1

    def _unique_name(self, requested: Optional[str], device_type: str) -> str:
        base = requested or f"{device_type}_{self._counter}"
        base = re.sub(r"[^A-Za-z0-9_-]", "", str(base)).strip() or device_type
        if base not in self.simulator.topology.devices:
            return base
        index = 2
        while f"{base}_{index}" in self.simulator.topology.devices:
            index += 1
        return f"{base}_{index}"

    def _record(self) -> None:
        self.history.append(deepcopy(self.document))
        del self.history[:-50]
        self.future.clear()

    def _rebuild(self, document: Dict[str, Any]) -> None:
        topology = NetworkTopology()
        topology.clear()
        self.document = deepcopy(document)
        self.document.setdefault("version", 1)
        self.document.setdefault("name", "Untitled Network")
        self.document.setdefault("devices", [])
        self.document.setdefault("links", [])
        self.document.setdefault("settings", {})
        self.document.setdefault("positions", {})
        for item in self.document["devices"]:
            self.document["positions"].setdefault(str(item["id"]), {"x": float(item.get("x", 200)), "y": float(item.get("y", 200))})
        settings = self.document["settings"]
        seed = int(settings.get("seed", self.seed))
        self.seed = seed

        for item in self.document["devices"]:
            name = str(item["id"])
            device_type = str(item.get("type", "host"))
            topology.add_node(name, device_type)
            device = topology.get_device(name)
            if device is None:
                continue
            device.default_gateway = item.get("default_gateway")
            device.set_status("UP")
            # Replace the deterministic default interface only when the
            # document contains serialized interface data. Presets with no
            # interface payload retain the topology's deterministic default.
            serialized_interfaces = item.get("interfaces", [])
            if serialized_interfaces:
                device.interfaces.clear()
                for interface_data in serialized_interfaces:
                    interface = NetworkInterface(
                        interface_id=interface_data.get("interface_id", "eth0"),
                        mac_address=interface_data.get("mac_address"),
                        ip_address=interface_data.get("ip_address"),
                        prefix=int(interface_data.get("prefix", 24)),
                        status=interface_data.get("status", "UP"),
                    )
                    device.add_interface(interface)
            if not device.interfaces:
                device.add_interface(NetworkInterface("eth0"))

        for link in self.document["links"]:
            u, v = str(link["source"]), str(link["target"])
            if u not in topology.devices or v not in topology.devices:
                continue
            topology.add_link(
                u,
                v,
                latency=float(link.get("latency", 10.0)),
                bandwidth=float(link.get("bandwidth", 100.0)),
                packet_loss=float(link.get("packet_loss", 0.0)),
                congestion=float(link.get("congestion", 0.0)),
            )
            edge = topology.graph[u][v]
            edge["interface_a"] = link.get("interface_a", "eth0")
            edge["interface_b"] = link.get("interface_b", "eth0")
            edge["duplex"] = link.get("duplex", "Full")
            if link.get("status", "UP") == "DOWN":
                topology.fail_link(u, v)

        self.diagnostic_result = None
        self.transport_result = None
        self.service_result = None
        self.security_result = None
        self.learning_result = None
        self.quiz_answers = {}
        self.demo_result = None
        self.step_mode = None
        self.challenge_answers = {}
        self.challenge_marks = {}
        self.simulator = NetworkSimulator(
            topology=topology,
            seed=seed,
            scheduler=str(settings.get("scheduler", "priority")),
            algorithm=str(settings.get("routing_algorithm", "dijkstra")),
        )
        # QoS class priorities are process-wide; start every session from the
        # defaults and only then apply the document's own configuration.
        self.simulator.reset_traffic_priorities()
        if settings.get("routing_weights"):
            self.simulator.set_routing_weights(**settings["routing_weights"])
        if settings.get("qos_priorities"):
            self.simulator.set_priority_config(settings["qos_priorities"])
        if settings.get("wfq_weights"):
            self.simulator.set_wfq_weights(settings["wfq_weights"])
        self._counter = max(
            [self._counter, len(self.simulator.topology.devices) + 1]
        )

    def export_document(self) -> Dict[str, Any]:
        return deepcopy(self.document)

    def export_json(self) -> str:
        return json.dumps(self.export_document(), indent=2, sort_keys=True)

    def import_document(self, document: Dict[str, Any], record: bool = True) -> Dict[str, Any]:
        if not isinstance(document, dict) or not isinstance(document.get("devices"), list):
            raise LabError("Topology JSON must contain a devices list")
        if record:
            self._record()
        self._rebuild(document)
        self.running = False
        self.last_packet = None
        return self.state()

    def load_preset(self, key: str, record: bool = True) -> Dict[str, Any]:
        if key not in PRESETS:
            raise LabError(f"Unknown topology preset: {key}")
        if record:
            self._record()
        self._rebuild(deepcopy(PRESETS[key]))
        self.running = False
        self.last_packet = None
        return self.state()

    def new_network(self, record: bool = True) -> Dict[str, Any]:
        if record:
            self._record()
        self._rebuild({"version": 1, "name": "New Network", "devices": [], "links": [], "settings": {"seed": self.seed}})
        self.running = False
        self.last_packet = None
        return self.state()

    def _update_document(self) -> None:
        topology = self.simulator.topology
        devices = []
        for name, device in topology.devices.items():
            position = self.document.setdefault("positions", {}).get(name, {"x": 120, "y": 120})
            devices.append({
                "id": name,
                "type": device.device_type,
                "x": float(position.get("x", 120)),
                "y": float(position.get("y", 120)),
                "default_gateway": device.default_gateway,
                "interfaces": [interface.to_dict() for interface in device.interfaces],
            })
        links = []
        for u, v, edge in topology.graph.edges(data=True):
            links.append({
                "id": f"{u}--{v}",
                "source": u,
                "target": v,
                "status": edge.get("status", "UP"),
                "latency": float(edge.get("latency", 10.0)),
                "bandwidth": float(edge.get("bandwidth", 100.0)),
                "packet_loss": float(edge.get("packet_loss", 0.0)),
                "congestion": float(edge.get("congestion", 0.0)),
                "duplex": edge.get("duplex", "Full"),
                "interface_a": edge.get("interface_a", "eth0"),
                "interface_b": edge.get("interface_b", "eth0"),
            })
        self.document["devices"] = devices
        self.document["links"] = links
        self.document.setdefault("settings", {})["seed"] = self.simulator.seed
        self.document["settings"]["routing_algorithm"] = self.simulator.get_router_algorithm()
        self.document["settings"]["routing_weights"] = self.simulator.get_routing_weights()
        self.document["settings"]["scheduler"] = self.simulator.scheduler_name
        self.document["settings"]["qos_priorities"] = self.simulator.get_priority_config()
        self.document["settings"]["wfq_weights"] = self.simulator.get_wfq_weights()

    # ------------------------------------------------------------------
    # Edit operations
    # ------------------------------------------------------------------
    def add_device(self, device_type: str, x: float, y: float, name: Optional[str] = None) -> Dict[str, Any]:
        device_type = str(device_type).lower()
        if device_type not in PALETTE_TYPES:
            raise LabError(f"Unsupported device type: {device_type}")
        with self.lock:
            self._record()
            self._counter += 1
            device_name = self._unique_name(name, device_type)
            self.simulator.topology.add_node(device_name, device_type)
            self.document.setdefault("positions", {})[device_name] = {"x": float(x), "y": float(y)}
            self._update_document()
            return self.state()

    def move_device(self, name: str, x: float, y: float) -> Dict[str, Any]:
        with self.lock:
            if name not in self.simulator.topology.devices:
                raise LabError(f"Unknown device: {name}")
            self._record()
            self.document.setdefault("positions", {})[name] = {"x": float(x), "y": float(y)}
            return self.state()

    def rename_device(self, old_name: str, new_name: str) -> Dict[str, Any]:
        with self.lock:
            if old_name not in self.simulator.topology.devices:
                raise LabError(f"Unknown device: {old_name}")
            new_name = self._unique_name(new_name, "Device")
            if new_name == old_name:
                return self.state()
            self._record()
            topology = self.simulator.topology
            device = topology.devices[old_name]
            position = self.document.setdefault("positions", {}).pop(old_name, {"x": 200, "y": 200})
            old_edges = []
            for source, target, edge in topology.graph.edges(data=True):
                if old_name in {source, target}:
                    old_edges.append((source, target, dict(edge)))
            topology.graph.remove_node(old_name)
            topology.devices.pop(old_name, None)
            device.name = new_name
            topology.devices[new_name] = device
            topology.graph.add_node(new_name, type=device.device_type, status=device.status)
            for source, target, edge in old_edges:
                new_source = new_name if source == old_name else source
                new_target = new_name if target == old_name else target
                topology.graph.add_edge(new_source, new_target, **edge)
                for endpoint, neighbour in ((new_source, new_target), (new_target, new_source)):
                    endpoint_device = topology.devices[endpoint]
                    for interface in endpoint_device.interfaces:
                        interface.associate_link(f"{endpoint}-{neighbour}")
            self.document.setdefault("positions", {})[new_name] = position
            for link in list(self.document.get("links", [])):
                if link["source"] == old_name:
                    link["source"] = new_name
                if link["target"] == old_name:
                    link["target"] = new_name
            for flow in self.simulator.active_flows.values():
                if flow.source == old_name:
                    flow.source = new_name
                if flow.destination == old_name:
                    flow.destination = new_name
                flow.current_route = [new_name if hop == old_name else hop for hop in flow.current_route]
                flow.previous_route = [new_name if hop == old_name else hop for hop in flow.previous_route]
                try:
                    flow.current_route, flow.route_cost = self.simulator.router.shortest_path(self.simulator.topology, flow.source, flow.destination)
                except ValueError:
                    flow.current_route, flow.route_status, flow.status = [], "FAILED", "FAILED"
            self._update_document()
            return self.state()

    def delete_device(self, name: str) -> Dict[str, Any]:
        with self.lock:
            if name not in self.simulator.topology.devices:
                raise LabError(f"Unknown device: {name}")
            self._record()
            self.simulator.topology.remove_node(name)
            self.document.setdefault("positions", {}).pop(name, None)
            for flow in self.simulator.active_flows.values():
                if name in {flow.source, flow.destination} or name in flow.current_route:
                    flow.status = "FAILED"
                    flow.route_status = "FAILED"
                    flow.current_route = []
            self._update_document()
            return self.state()

    def duplicate_device(self, name: str) -> Dict[str, Any]:
        with self.lock:
            if name not in self.simulator.topology.devices:
                raise LabError(f"Unknown device: {name}")
            original = self.simulator.topology.devices[name]
            x = self.document.get("positions", {}).get(name, {}).get("x", 200) + 40
            y = self.document.get("positions", {}).get(name, {}).get("y", 200) + 40
            return self.add_device(original.device_type, x, y, f"{name}_copy")

    def add_link(self, source: str, target: str, latency: float = 10.0, bandwidth: float = 100.0) -> Dict[str, Any]:
        with self.lock:
            topology = self.simulator.topology
            if source == target or source not in topology.devices or target not in topology.devices:
                raise LabError("Choose two different existing devices")
            if topology.graph.has_edge(source, target):
                raise LabError("Those devices are already connected")
            self._record()
            topology.add_link(source, target, latency=latency, bandwidth=bandwidth)
            self._update_document()
            return self.state()

    def delete_link(self, source: str, target: str) -> Dict[str, Any]:
        with self.lock:
            if not self.simulator.topology.graph.has_edge(source, target):
                raise LabError("Link does not exist")
            self._record()
            self.simulator.topology.remove_link(source, target)
            for flow in self.simulator.active_flows.values():
                if self.simulator._route_uses_link(flow.current_route, source, target):
                    try:
                        route, cost = self.simulator.router.shortest_path(self.simulator.topology, flow.source, flow.destination)
                        flow.current_route, flow.route_cost, flow.route_status = route, cost, "REROUTED"
                    except ValueError:
                        flow.current_route, flow.route_status, flow.status = [], "FAILED", "FAILED"
            self._update_document()
            return self.state()

    def configure_link(self, source: str, target: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if not self.simulator.topology.graph.has_edge(source, target):
                raise LabError("Link does not exist")
            self._record()
            self.simulator.set_link_conditions(
                source,
                target,
                latency=float(values["latency"]) if values.get("latency") is not None else None,
                bandwidth=float(values["bandwidth"]) if values.get("bandwidth") is not None else None,
                packet_loss=float(values["packet_loss"]) if values.get("packet_loss") is not None else None,
                congestion=float(values["congestion"]) if values.get("congestion") is not None else None,
            )
            if values.get("status") == "DOWN":
                self.simulator.fail_link(source, target)
            elif values.get("status") == "UP":
                self.simulator.recover_link(source, target)
            edge = self.simulator.topology.graph[source][target]
            for key in ("duplex", "interface_a", "interface_b"):
                if key in values:
                    edge[key] = values[key]
            self._update_document()
            return self.state()

    def configure_interface(self, name: str, interface_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            device = self.simulator.topology.get_device(name)
            if device is None:
                raise LabError(f"Unknown device: {name}")
            self._record()
            try:
                interface = device.get_interface(interface_id)
            except KeyError as exc:
                raise LabError(
                    f"Interface does not exist: {name} has no interface {interface_id}"
                ) from exc
            if values.get("ip_address"):
                prefix = values.get("prefix")
                interface.assign_ip(str(values["ip_address"]), int(prefix) if prefix is not None else None)
            if values.get("mac_address"):
                interface.mac_address = str(values["mac_address"]).upper()
            if values.get("status") == "DOWN":
                self.simulator.fail_interface(name, interface_id)
            elif values.get("status") == "UP":
                self.simulator.recover_interface(name, interface_id)
            self._update_document()
            return self.state()

    def shutdown_device(self, name: str) -> Dict[str, Any]:
        with self.lock:
            self._record()
            self.simulator.fail_node(name)
            self._update_document()
            return self.state()

    def restart_device(self, name: str) -> Dict[str, Any]:
        with self.lock:
            self._record()
            self.simulator.recover_node(name)
            self._update_document()
            return self.state()

    # ------------------------------------------------------------------
    # Simulation and diagnostics
    # ------------------------------------------------------------------
    def start_traffic(self, source: str, destination: str, packet_count: int = 10, pps: float = 5.0, traffic_type: str = "Video", packet_size: int = 1500) -> Dict[str, Any]:
        with self.lock:
            if source not in self.simulator.topology.devices or destination not in self.simulator.topology.devices:
                raise LabError("Traffic source and destination must be existing devices")
            if source == destination:
                raise LabError("Traffic source and destination must be different")
            if traffic_type not in TRAFFIC_TYPES:
                raise LabError(f"Unsupported traffic type: {traffic_type}")
            if int(packet_count) < 1 or int(packet_size) < 1 or float(pps) <= 0:
                raise LabError("Packet count, packet size, and rate must be positive")
            self.simulator.create_flow(source, destination, traffic_type, int(packet_count), int(packet_size), float(pps), float(packet_count) / max(pps, 0.1))
            self.running = True
            return self.state()

    def step(self) -> Dict[str, Any]:
        with self.lock:
            before = len(self.simulator.event_logger.events)
            packet = self.simulator.process_next_packet()
            if packet is None:
                if self.simulator._failed_links or self.simulator._failed_nodes:
                    self.simulator.tick(max(0.1, self.simulator.heartbeat_interval))
                else:
                    self.running = False
            else:
                packet_events = [
                    event.to_dict() for event in self.simulator.event_logger.events[before:]
                    if event.details.get("packet_id") == packet.packet_id
                ]
                flow = next((item for item in self.simulator.active_flows.values() if packet.packet_id in item.packet_ids), None)
                drop_reason = next((event["details"].get("reason") for event in reversed(packet_events) if event["details"].get("reason")), None)
                if packet.delivery_status == "DROPPED" and drop_reason == "NO_ROUTE":
                    drop_reason = "DESTINATION_UNREACHABLE"
                transport_packet = getattr(packet, "transport", None)
                self.last_packet = {
                    "id": packet.packet_id,
                    "source": packet.source,
                    "destination": packet.destination,
                    "protocol": "IP",
                    "traffic_class": packet.traffic_type,
                    "size": packet.size,
                    "ttl": packet.ttl,
                    "flow_id": flow.flow_id if flow else None,
                    "route": list(packet.route),
                    "status": packet.delivery_status,
                    "latency": packet.latency,
                    "drop_reason": drop_reason,
                    "next_hop": packet.route[1] if packet.delivery_status == "PENDING" and len(packet.route) > 1 else None,
                    "current_device": packet.destination if packet.delivery_status == "DELIVERED" else packet.source,
                    "journey": packet_events,
                    "events": packet_events,
                }
                if transport_packet is not None:
                    self.last_packet.update(transport_packet.to_dict())
                    self.last_packet["size"] = transport_packet.payload_size
                    self.last_packet["journey"] = packet_events
            self._update_document()
            return self.state()

    def reset_simulation(self) -> Dict[str, Any]:
        with self.lock:
            self._record()
            self.simulator.reset()
            self.running = False
            self.last_packet = None
            self.diagnostic_result = None
            self.transport_result = None
            self.service_result = None
            self.security_result = None
            return self.state()

    def set_running(self, running: bool) -> Dict[str, Any]:
        with self.lock:
            self.running = bool(running)
            return self.state()

    def set_speed(self, speed: float) -> Dict[str, Any]:
        with self.lock:
            self.speed = max(0.25, min(10.0, float(speed)))
            return self.state()

    def set_scheduler(self, scheduler: str) -> Dict[str, Any]:
        """Select the live queue scheduler used by subsequent packets."""
        with self.lock:
            self._record()
            self.simulator.set_scheduler(scheduler)
            self._update_document()
            return self.state()

    def set_qos_config(
        self,
        priorities: Optional[Dict[str, float]] = None,
        weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Apply class priorities and/or WFQ weights to the live scheduler."""
        with self.lock:
            self._record()
            if priorities is not None:
                self.simulator.set_priority_config(
                    {str(name): float(value) for name, value in priorities.items()}
                )
            if weights is not None:
                self.simulator.set_wfq_weights(
                    {str(name): float(value) for name, value in weights.items()}
                )
            self._update_document()
            return self.state()

    def set_routing_algorithm(self, algorithm: str) -> Dict[str, Any]:
        with self.lock:
            self._record()
            self.simulator.set_router_algorithm(algorithm)
            self._update_document()
            return self.state()

    def set_routing_weights(self, weights: Dict[str, float]) -> Dict[str, Any]:
        with self.lock:
            self._record()
            self.simulator.set_routing_weights(**{str(k): float(v) for k, v in weights.items()})
            self._update_document()
            return self.state()

    def _set_diagnostic_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.diagnostic_result = deepcopy(result)
        packet_results = result.get("packet_results") or result.get("packets") or []
        self.record_diagnostic_packets(result)
        if packet_results:
            self.last_packet = deepcopy(packet_results[-1])
        self.running = False
        return self.state()

    def record_diagnostic_packets(self, result: Dict[str, Any]) -> None:
        """Record measured diagnostic packets in the same metrics as flow traffic."""
        if result.get("type") != "ping":
            return
        rtt_ms = result.get("rtt_ms", {}).get("average")
        for packet in result.get("packet_results") or []:
            delivered = packet.get("status") == "DELIVERED" or packet.get("delivered") is True
            latency = (float(rtt_ms) / 1000.0) if delivered and rtt_ms is not None else None
            self.simulator.metrics.record(
                packet.get("id", len(self.simulator.metrics.records)),
                float(packet.get("created_at", self.simulator.time)),
                float(packet.get("created_at", self.simulator.time)) + latency if latency is not None else None,
                latency,
                int(packet.get("size", 84)),
                "DELIVERED" if delivered else "DROPPED",
                flow_id=packet.get("flow_id"),
                route=packet.get("route"),
                traffic_type="ICMP",
            )

    def run_ping(self, source: str, destination: str, count: int = 4) -> Dict[str, Any]:
        with self.lock:
            return self._set_diagnostic_result(
                self.simulator.diagnostic_ping(source, destination, count=count)
            )

    def run_traceroute(self, source: str, destination: str) -> Dict[str, Any]:
        with self.lock:
            return self._set_diagnostic_result(
                self.simulator.traceroute(source, destination)
            )

    def inspect_arp(self, device_name: str) -> Dict[str, Any]:
        with self.lock:
            entries = self.simulator.arp_table(device_name)
            return self._set_diagnostic_result({
                "type": "arp",
                "title": f"ARP TABLE · {device_name}",
                "device": device_name,
                "entries": entries,
            })

    def clear_arp(self, device_name: str) -> Dict[str, Any]:
        with self.lock:
            removed = self.simulator.clear_arp(device_name)
            return self._set_diagnostic_result({
                "type": "arp",
                "title": f"ARP TABLE · {device_name}",
                "device": device_name,
                "entries": [],
                "cleared": removed,
                "message": f"Cleared {removed} ARP entr{'y' if removed == 1 else 'ies'}.",
            })

    def inspect_mac(self, switch_name: str) -> Dict[str, Any]:
        with self.lock:
            entries = self.simulator.mac_table(switch_name)
            return self._set_diagnostic_result({
                "type": "mac",
                "title": f"MAC TABLE · {switch_name}",
                "device": switch_name,
                "entries": entries,
            })

    def clear_mac(self, switch_name: str) -> Dict[str, Any]:
        with self.lock:
            removed = self.simulator.clear_mac_table(switch_name)
            return self._set_diagnostic_result({
                "type": "mac",
                "title": f"MAC TABLE · {switch_name}",
                "device": switch_name,
                "entries": [],
                "cleared": removed,
                "message": f"Cleared {removed} MAC entr{'y' if removed == 1 else 'ies'}.",
            })

    def create_transport(self, protocol: str, source: str, destination: str, source_port: int = 5000, destination_port: int = 8080, payload_size: int = 1000, initial_cwnd: int = 1, receiver_window: int = 8, ssthresh: float = 16.0, timeout: float = 0.1, traffic_class: str = "HTTP") -> Dict[str, Any]:
        with self.lock:
            if protocol.upper() == "TCP":
                self.simulator.create_tcp_connection(source, destination, source_port, destination_port, initial_cwnd, receiver_window, ssthresh, timeout, traffic_class)
                # The live lab advances the same queued handshake packets that
                # the simulator-level API exposes; the UI never fabricates a
                # connection state independently.
                self.simulator.run_until_empty()
            elif protocol.upper() == "UDP":
                self.simulator.create_udp_flow(source, destination, source_port, destination_port, payload_size, traffic_class)
            else:
                raise LabError("Transport protocol must be TCP or UDP")
            self.running = True
            return self.state()

    def send_transport_data(self, protocol: str, flow_id: str, packet_count: int = 1, payload_size: int = 1000) -> Dict[str, Any]:
        with self.lock:
            if protocol.upper() == "TCP":
                self.simulator.send_tcp_data(flow_id, packet_count, payload_size)
            elif protocol.upper() == "UDP":
                self.simulator.send_udp_data(flow_id, packet_count, payload_size)
            else:
                raise LabError("Transport protocol must be TCP or UDP")
            self.simulator.run_until_empty()
            return self.state()

    def close_transport(self, flow_id: str) -> Dict[str, Any]:
        with self.lock:
            self.simulator.close_tcp_connection(flow_id)
            self.simulator.run_until_empty()
            return self.state()

    def process_transport_tick(self, delta: float = 0.1) -> Dict[str, Any]:
        with self.lock:
            self.simulator.tick(float(delta))
            # A timeout tick may enqueue a retransmission. Process that packet
            # through the same routing/QoS path immediately so the live panel
            # reflects actual delivery and ACK recovery.
            self.simulator.run_until_empty()
            return self.state()

    def reset_transport(self) -> Dict[str, Any]:
        with self.lock:
            self.simulator.transport.reset()
            self.transport_result = None
            return self.state()

    def compare_transport(self, source: str, destination: str, packet_count: int = 6, payload_size: int = 1000) -> Dict[str, Any]:
        with self.lock:
            from transport import compare_transport
            self.transport_result = compare_transport(self.simulator, source, destination, packet_count, payload_size)
            return self.state()

    # ------------------------------------------------------------------
    # Stage 10 network services
    # ------------------------------------------------------------------
    def install_service(self, name: str, device: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self.lock:
            try:
                self.simulator.install_service(name, device, **(config or {}))
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self._update_document()
            return self.state()

    def remove_service(self, name: str, device: str) -> Dict[str, Any]:
        with self.lock:
            self.simulator.remove_service(name, device)
            return self.state()

    def start_service(self, name: str, device: str) -> Dict[str, Any]:
        return self.service_control("start", name, device)

    def stop_service(self, name: str, device: str) -> Dict[str, Any]:
        return self.service_control("stop", name, device)

    def restart_service(self, name: str, device: str) -> Dict[str, Any]:
        return self.service_control("restart", name, device)

    def service_control(self, action: str, name: str, device: str) -> Dict[str, Any]:
        with self.lock:
            try:
                if action == "start":
                    self.simulator.start_service(name, device)
                elif action == "stop":
                    self.simulator.stop_service(name, device)
                elif action == "restart":
                    self.simulator.restart_service(name, device)
                else:
                    raise LabError(f"Unknown service action: {action}")
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self.state()

    def configure_service(self, name: str, device: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            try:
                self.simulator.configure_service(name, device, values)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self.state()

    def _service_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.service_result = result
        return self.state()

    def dhcp_acquire(self, client: str, server: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            result = self.simulator.dhcp_acquire(client, server)
            self.running = True
            return self._service_result(result)

    def dhcp_renew(self, client: str, server: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            return self._service_result(self.simulator.dhcp_renew(client, server))

    def dhcp_release(self, client: str) -> Dict[str, Any]:
        with self.lock:
            return self._service_result(self.simulator.dhcp_release(client))

    def dns_query(self, client: str, hostname: str, server: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            return self._service_result(self.simulator.dns_query(client, hostname, server))

    def dns_add_record(self, server: str, hostname: str, address: str, ttl: float = 300.0) -> Dict[str, Any]:
        with self.lock:
            try:
                self.simulator.dns_add_record(server, hostname, address, ttl)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self.state()

    def http_request(
        self,
        client: str,
        server: Optional[str] = None,
        method: str = "GET",
        path: str = "/",
        body: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            return self._service_result(
                self.simulator.http_request(client, server, method=method, path=path, body=body)
            )

    def ftp_command(
        self,
        client: str,
        server: Optional[str] = None,
        command: str = "LIST",
        filename: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            if str(command).upper() == "CONNECT":
                return self._service_result(self.simulator.ftp_connect(client, server))
            return self._service_result(
                self.simulator.ftp_command(
                    client, server, command=command, filename=filename, content=content
                )
            )

    def smtp_send(
        self,
        client: str,
        server: Optional[str] = None,
        sender: str = "student@netadapt.local",
        recipient: str = "server@netadapt.local",
        subject: str = "Stage 10 lab message",
        body: str = "Hello from the NetAdapt simulated SMTP service.",
    ) -> Dict[str, Any]:
        with self.lock:
            return self._service_result(
                self.simulator.smtp_send(
                    client, server, sender=sender, recipient=recipient, subject=subject, body=body
                )
            )

    # ------------------------------------------------------------------
    # Stage 10 security
    # ------------------------------------------------------------------
    def _security_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.security_result = result
        return self.state()

    # Convenience aliases matching the simulator-level API names.
    def add_firewall_rule(self, **values: Any) -> Dict[str, Any]:
        return self.firewall_add_rule(values)

    def add_acl_entry(self, name: str, **values: Any) -> Dict[str, Any]:
        return self.acl_add_entry(name, values)

    def create_acl(self, name: str, acl_type: str = "STANDARD") -> Dict[str, Any]:
        return self.acl_create(name, acl_type)

    def attach_acl(self, name: str, device: str, interface_id: str = "eth0") -> Dict[str, Any]:
        return self.acl_attach(name, device, interface_id)

    def firewall_add_rule(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            try:
                if values.get("port_filter"):
                    rule = self.simulator.add_port_filter(
                        str(values.get("protocol") or "TCP"),
                        int(values.get("port", 0)),
                        str(values.get("action", "DENY")).upper(),
                        values.get("device"),
                    )
                else:
                    rule = self.simulator.add_firewall_rule(**values)
            except (TypeError, ValueError) as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(rule.to_dict())

    def firewall_remove_rule(self, rule_id: int) -> Dict[str, Any]:
        with self.lock:
            if not self.simulator.services.firewall.remove_rule(int(rule_id)):
                raise LabError(f"Unknown firewall rule: {rule_id}")
            return self._security_result({"removed": int(rule_id)})

    def firewall_clear(self) -> Dict[str, Any]:
        with self.lock:
            self.simulator.services.firewall.clear()
            return self._security_result({"cleared": True})

    def acl_create(self, name: str, acl_type: str = "STANDARD") -> Dict[str, Any]:
        with self.lock:
            try:
                acl = self.simulator.create_acl(name, acl_type)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(acl.to_dict())

    def acl_add_entry(self, name: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            try:
                entry = self.simulator.add_acl_entry(name, **values)
            except (TypeError, ValueError) as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(entry.to_dict())

    def acl_attach(self, name: str, device: str, interface_id: str = "eth0") -> Dict[str, Any]:
        with self.lock:
            if device not in self.simulator.topology.devices:
                raise LabError(f"Unknown device: {device}")
            try:
                acl = self.simulator.attach_acl(name, device, interface_id)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(acl.to_dict())

    def acl_detach(self, name: str, device: str, interface_id: str = "eth0") -> Dict[str, Any]:
        with self.lock:
            try:
                acl = self.simulator.services.acls.detach(name, device, interface_id)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(acl.to_dict())

    def acl_remove(self, name: str) -> Dict[str, Any]:
        with self.lock:
            if not self.simulator.services.acls.remove(name):
                raise LabError(f"Unknown access list: {name}")
            return self._security_result({"removed": name})

    def configure_security(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            layer = self.simulator.services
            if "arp_detection" in values:
                layer.arp_detection = bool(values["arp_detection"])
            if "arp_protection" in values:
                layer.arp_protection = bool(values["arp_protection"])
            if "flood_protection" in values:
                layer.flood_protection = bool(values["flood_protection"])
            if "flood_threshold" in values:
                layer.flood_threshold = max(1.0, float(values["flood_threshold"]))
            return self._security_result(self.simulator.security_state())

    def arp_spoof(
        self,
        attacker: str,
        victim: str,
        target_ip: Optional[str] = None,
        detect: bool = True,
        protect: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            try:
                result = self.simulator.arp_spoof(
                    attacker, victim, target_ip, detect=detect, protect=protect
                )
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self._security_result(result)

    def start_flood(
        self,
        attacker: str,
        target: str,
        protocol: str = "UDP",
        rate: float = 1000.0,
        duration: float = 1.0,
        threshold: Optional[float] = None,
        protect: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            try:
                result = self.simulator.start_flood(
                    attacker,
                    target,
                    protocol=protocol,
                    rate=rate,
                    duration=duration,
                    threshold=threshold,
                    protect=protect,
                )
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self.running = True
            return self._security_result(result)

    def console(self, device_name: str, command: str) -> Dict[str, Any]:
        with self.lock:
            device = self.simulator.topology.get_device(device_name)
            if device is None:
                raise LabError(f"Unknown device: {device_name}")
            normalized = " ".join(str(command).strip().split()).lower()
            if normalized in {"help", "?"}:
                lines = ["Available command groups:"]
                for key in sorted(CLI_HELP):
                    lines.append(f"  help {key:<10} {CLI_HELP[key]}")
                return {"output": "\n".join(lines), "command": command}
            if normalized.startswith("help "):
                key = normalized.split(" ", 1)[1].strip()
                text = CLI_HELP.get(key)
                if text is None:
                    return {
                        "output": f"% No help topic for '{key}'. Try: "
                        + ", ".join(sorted(CLI_HELP)),
                        "command": command,
                    }
                return {"output": f"{key}: {text}", "command": command}
            stage10 = self._console_stage10(device_name, normalized)
            if stage10 is not None:
                return {**stage10, "command": command}
            if normalized in {"netstat", "netstat -a", "show transport connections", "show transport"}:
                stats = self.simulator.get_transport_statistics()
                flows = stats["connections"] + stats["udp_flows"]
                relevant = [
                    flow for flow in flows
                    if device_name in {flow["source"], flow["destination"]}
                ]
                if not relevant:
                    return {
                        "output": f"No active transport connections on {device_name}.",
                        "command": command,
                        "flows": [],
                    }
                lines = [
                    "Proto  Flow ID       Local endpoint          Remote endpoint         State             Retrans  In flight",
                ]
                for flow in relevant:
                    retrans = flow.get("retransmission_count", flow.get("retransmissions", 0))
                    in_flight = flow.get("packets_in_flight", 0)
                    lines.append(
                        f"{flow['protocol']:<5}  {flow['flow_id']:<12}  "
                        f"{flow['source']}:{flow['source_port']:<15} "
                        f"{flow['destination']}:{flow['destination_port']:<12} "
                        f"{flow['state']:<17} {retrans:>7}  {in_flight:>9}"
                    )
                return {
                    "output": "\n".join(lines),
                    "command": command,
                    "flows": relevant,
                }
            if normalized in {"show interfaces", "show interface status"}:
                lines = [f"{device_name} {device.device_type.upper()}"]
                lines.extend(f"{i.interface_id}: {i.mac_address} {i.ip_address or 'unassigned'}/{i.prefix} [{i.status}]" for i in device.interfaces)
                return {"output": "\n".join(lines), "command": command}
            if normalized in {"ipconfig", "ifconfig"}:
                lines = [f"{device_name} {device.device_type}"]
                lines.extend(f"  {i.interface_id}: MAC {i.mac_address} IP {i.ip_address or 'unassigned'} mask {i.subnet_mask or 'n/a'} gateway {device.default_gateway or 'n/a'}" for i in device.interfaces)
                return {"output": "\n".join(lines), "command": command}
            if normalized in {"show arp", "arp -a"}:
                return {"output": json.dumps(self.simulator.arp.to_dict(), indent=2), "command": command}
            if normalized == "show ip route":
                if not device.is_router:
                    return {
                        "output": "% Command not supported on this device: show ip route is a router command",
                        "command": command,
                        "success": False,
                    }
                table = self.simulator.routing_table(device_name)
                return {"output": "\n".join([f"{e.destination_network}/{e.prefix} via {e.next_hop or 'DIRECT'} {e.outgoing_interface} metric {e.metric:g}" for e in table.entries]) or "No routes", "command": command}
            if normalized.startswith("ping "):
                target_ip = normalized.split(" ", 1)[1]
                if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", target_ip):
                    return {"output": f"% Invalid IP address: {target_ip}", "command": command, "success": False}
                target = next((d for d in self.simulator.topology.devices.values() if any(i.ip_address == target_ip for i in d.interfaces)), None)
                if target is None:
                    return {"output": f"ping: cannot resolve {target_ip}", "command": command, "success": False}
                result = self.simulator.ping(device_name, target.name)
                return {"output": f"{target_ip} reachable: {result.rtt * 1000:.2f} ms, {result.hops} hops" if result.success else f"ping failed: {result.reason}", "command": command, "success": result.success}
            if normalized.startswith("tracert ") or normalized.startswith("traceroute "):
                target_ip = normalized.split(" ", 1)[1]
                if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", target_ip):
                    return {"output": f"% Invalid IP address: {target_ip}", "command": command, "success": False}
                target = next((d for d in self.simulator.topology.devices.values() if any(i.ip_address == target_ip for i in d.interfaces)), None)
                if target is None:
                    return {"output": f"target not found: {target_ip}", "command": command, "success": False}
                result = self.simulator.ping(device_name, target.name)
                return {"output": "\n".join(f"{index + 1}: {hop}" for index, hop in enumerate(result.route)) or "No route", "command": command, "success": result.success}
            return {
                "output": f"% Command not supported: {command.strip()} "
                "(type 'help' for the available commands)",
                "command": command,
                "success": False,
            }

    # ------------------------------------------------------------------
    # Stage 10 services and security controls
    # ------------------------------------------------------------------
    def service_request(self, name: str, client: str, **values) -> Dict[str, Any]:
        with self.lock:
            name = name.upper()
            if name == "DHCP":
                result = self.simulator.dhcp_acquire(client, values.get("server"))
            elif name == "DNS":
                result = self.simulator.dns_query(client, values["hostname"], values.get("server"))
            elif name == "HTTP":
                result = self.simulator.http_request(client, values.get("server"), values.get("method", "GET"), values.get("path", "/"), values.get("body"))
            elif name == "FTP":
                result = self.simulator.ftp_command(client, values.get("server"), values.get("command", "LIST"), values.get("filename"), values.get("content"))
            elif name == "SMTP":
                result = self.simulator.smtp_send(client, values.get("server"), values.get("sender", "student@netadapt.local"), values.get("recipient", "server@netadapt.local"), values.get("subject", "NetAdapt lab message"), values.get("body", "Simulated message"))
            else:
                raise LabError(f"Unknown service: {name}")
            self.service_result = result
            return self.state()

    def security_control(self, action: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if action == "firewall":
                rule = self.simulator.add_firewall_rule(**values)
            elif action == "port_filter":
                rule = self.simulator.add_port_filter(**values)
            elif action == "acl":
                acl = self.simulator.create_acl(values["name"], values.get("type", "STANDARD"))
                if values.get("source_ip") or values.get("action"):
                    self.simulator.add_acl_entry(values["name"], **{k: v for k, v in values.items() if k not in {"name", "type"}})
                rule = acl
            elif action == "attach_acl":
                rule = self.simulator.attach_acl(values["name"], values["device"], values.get("interface_id", "eth0"))
            elif action == "arp_spoof":
                rule = self.simulator.arp_spoof(**values)
            elif action == "flood":
                rule = self.simulator.start_flood(**values)
            else:
                raise LabError(f"Unknown security action: {action}")
            self.security_result = rule.to_dict() if hasattr(rule, "to_dict") else rule
            return self.state()

    # ------------------------------------------------------------------
    # Stage 10 CLI helpers (device commands backed by live simulation state)
    # ------------------------------------------------------------------
    def _console_stage10(self, device_name: str, normalized: str) -> Optional[Dict[str, Any]]:
        services = self.simulator.services

        if normalized in {"ipconfig", "ipconfig /all", "ifconfig", "ifconfig -a"}:
            device = self.simulator.topology.get_device(device_name)
            detail = normalized.endswith("/all") or normalized.endswith("-a")
            lines = [f"{device_name} {device.device_type}"]
            for interface in device.interfaces:
                lease = services.leases.get(device_name)
                lines.append(
                    f"  {interface.interface_id}: MAC {interface.mac_address} "
                    f"IP {interface.ip_address or 'unassigned'}/{interface.prefix} "
                    f"mask {interface.subnet_mask or 'n/a'} gateway {device.default_gateway or 'n/a'}"
                )
                if detail:
                    client = services.dhcp_clients.get(device_name, {})
                    lines.append(
                        f"    DHCP server : {client.get('server', 'none')}"
                    )
                    lines.append(
                        f"    Lease time  : {client.get('lease_time', 'n/a')}s"
                    )
                    lines.append(
                        f"    DNS servers : {', '.join(client.get('dns_servers', [])) or 'n/a'}"
                    )
                    lines.append(f"    Status      : {interface.status}")
            return {"output": "\n".join(lines), "device": device_name}

        if normalized in {"ipconfig /renew", "ipconfig /release"}:
            renew = normalized.endswith("/renew")
            result = (
                self.simulator.dhcp_renew(device_name)
                if renew
                else self.simulator.dhcp_release(device_name)
            )
            self.service_result = result
            verb = "renewed" if renew else "released"
            if result.get("success"):
                address = result.get("address", "none")
                if renew:
                    output = f"{verb} DHCP lease for {device_name}: {address}"
                else:
                    output = f"released DHCP lease for {device_name}"
            else:
                output = f"ipconfig: {verb} failed ({result.get('reason', 'unknown')})"
            return {"output": output, "result": result, "success": bool(result.get("success"))}

        if normalized.startswith("nslookup "):
            hostname = normalized.split(" ", 1)[1]
            result = self.simulator.dns_query(device_name, hostname)
            self.service_result = result
            if result.get("success"):
                output = (
                    f"Server:    {result['server']}\n"
                    f"Address:   {result['server']}\n\n"
                    f"Name:      {result['hostname']}\n"
                    f"Address:   {result['address']}   (from {result.get('source', 'SERVER')})"
                )
            else:
                output = (
                    f"** server can't find {hostname}: {result.get('reason', 'NXDOMAIN')}"
                )
            return {"output": output, "result": result, "success": bool(result.get("success"))}

        if normalized == "route print":
            table = []
            for device in self.simulator.topology.devices.values():
                if not device.is_router:
                    continue
                for entry in self.simulator.routing_table(device.name).entries:
                    table.append(
                        f"{device.name:<6} {entry.destination_network}/{entry.prefix}"
                        f"  via {entry.next_hop or 'DIRECT'}  {entry.outgoing_interface}"
                        f"  metric {entry.metric:g}"
                    )
            for device in self.simulator.topology.devices.values():
                if device.is_router or not device.default_gateway:
                    continue
                for interface in device.interfaces:
                    table.append(
                        f"{device.name:<6} 0.0.0.0/0  via {device.default_gateway}"
                        f"  {interface.interface_id}"
                    )
            return {
                "output": "\n".join(["IPv4 Route Table", *table]) if table else "No routes",
                "routes": table,
            }

        if normalized in {"show services", "show service"}:
            rows = services.service_status(device_name)
            if not rows:
                return {"output": f"No services installed on {device_name}.", "services": []}
            lines = [
                f"{device_name} ({self.simulator.services.device_ip(device_name) or 'no ip'})",
                f"{'SERVICE':<8}{'PORT':<7}{'PROTO':<7}{'QOS CLASS':<12}{'STATE':<9}REQUESTS",
            ]
            for row in rows:
                lines.append(
                    f"{row['name']:<8}{row['port']:<7}{row['protocol']:<7}"
                    f"{row['traffic_class']:<12}{row['state']:<9}{row['requests']}"
                    + (f"  ({row['unavailable_reason']})" if row["unavailable_reason"] else "")
                )
            return {"output": "\n".join(lines), "services": rows}

        if normalized.startswith("show service "):
            name = normalized.split(" ", 2)[2]
            try:
                instance = services.registry.require(name, device_name)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            ready, reason = services.service_ready(instance)
            rows = services.service_status(device_name)
            return {
                "output": "\n".join(
                    [
                        f"Service : {instance.name}",
                        f"Device  : {instance.device}",
                        f"IP      : {services.device_ip(instance.device)}",
                        f"Port    : {instance.port}/{instance.protocol}",
                        f"QoS     : {instance.traffic_class}",
                        f"State   : {instance.state}"
                        + ("" if ready else f" (unavailable: {reason})"),
                        f"Requests: {instance.requests}  Failures: {instance.failures}",
                        f"Config  : {json.dumps(instance.config, sort_keys=True)}",
                    ]
                ),
                "service": instance.to_dict(),
                "status": next((row for row in rows if row["name"] == instance.name), None),
            }

        if normalized == "show firewall":
            firewall = services.firewall.to_dict()
            lines = [
                f"Firewall on {device_name}: {'ENABLED' if firewall['active'] else 'NO RULES'} "
                f"(default action {firewall['default_action']})",
                f"Inspected: {firewall['packets_inspected']}  "
                f"Allowed: {firewall['packets_allowed']}  Blocked: {firewall['packets_blocked']}",
            ]
            for rule in firewall["rules"]:
                lines.append(
                    f"  rule {rule['rule_id']}: {rule['action']} {rule['protocol'] or 'any'} "
                    f"{rule['source_ip']} -> {rule['destination_ip']} "
                    f"dport {rule['destination_port'] or 'any'} [{rule['reason']}] "
                    f"matched {rule['packets_matched']}"
                )
            return {"output": "\n".join(lines), "firewall": firewall}

        if normalized in {"show access-lists", "show acl"}:
            acls = services.acls.to_dict()
            if not acls:
                return {"output": "No access lists configured.", "access_lists": []}
            lines = []
            for acl in acls:
                lines.append(f"{acl['name']} ({acl['type']})")
                for entry in acl["entries"]:
                    lines.append(
                        f"  {entry['sequence']} {entry['action']} {entry['source_ip']} "
                        f"{entry['protocol'] or 'any'} {entry['destination_ip']} "
                        f"eq {entry['destination_port'] or 'any'}"
                    )
                for attachment in acl["attachments"]:
                    lines.append(
                        f"  applied to {attachment['device']} {attachment['interface_id']}"
                    )
            return {"output": "\n".join(lines), "access_lists": acls}

        if normalized == "show connections":
            stats = self.simulator.get_transport_statistics()
            flows = [
                flow
                for flow in stats["connections"] + stats["udp_flows"]
                if device_name in {flow["source"], flow["destination"]}
            ]
            if not flows:
                return {"output": f"No active connections on {device_name}.", "flows": []}
            lines = ["Proto  Local endpoint            Remote endpoint            State"]
            for flow in flows:
                lines.append(
                    f"{flow['protocol']:<6}{flow['source']}:{flow['source_port']:<22}"
                    f"{flow['destination']}:{flow['destination_port']:<23}{flow['state']}"
                )
            return {"output": "\n".join(lines), "flows": flows}

        if normalized in {"show ip interface brief", "show ip interface"}:
            device = self.simulator.topology.get_device(device_name)
            if not device.is_router:
                raise LabError("show ip interface brief is only available on routers")
            lines = [f"{'Interface':<12}{'IP-Address':<16}{'Status':<9}{'Protocol':<10}Services"]
            for interface in device.interfaces:
                services_here = [
                    f"{row['name']}/{row['port']}"
                    for row in services.service_status(device_name)
                    if row["available"]
                ]
                lines.append(
                    f"{interface.interface_id:<12}{interface.ip_address or 'unassigned':<16}"
                    f"{interface.status:<9}{'up' if interface.status == 'UP' else 'down':<10}"
                    f"{', '.join(services_here) or '-'}"
                )
            return {"output": "\n".join(lines), "device": device_name}

        if normalized == "show running-config":
            device = self.simulator.topology.get_device(device_name)
            lines = [
                f"! NetAdapt simulated running configuration for {device_name}",
                f"hostname {device_name}",
                f"device-type {device.device_type}",
                f"default-gateway {device.default_gateway or 'none'}",
            ]
            for interface in device.interfaces:
                lines.append(
                    f"interface {interface.interface_id}"
                )
                lines.append(f"  ip address {interface.ip_address or 'dhcp'}"
                             f" {interface.prefix}")
                lines.append(f"  mac address {interface.mac_address}")
                lines.append(f"  {interface.status.lower()}")
            for acl in services.acls.to_dict():
                for attachment in acl["attachments"]:
                    if attachment["device"] != device_name:
                        continue
                    lines.append(f"! access-list {acl['name']} applied on {attachment['interface_id']}")
                    for entry in acl["entries"]:
                        lines.append(
                            f"access-list {entry['sequence']} {entry['action'].lower()} "
                            f"{entry['protocol'] or 'ip'} {entry['source_ip']} "
                            f"{entry['destination_ip']} {entry['destination_port'] or ''}".rstrip()
                        )
            for rule in services.firewall.rules:
                if rule.device in (None, device_name):
                    lines.append(
                        f"firewall rule {rule.rule_id} {rule.action.lower()} "
                        f"{rule.protocol or 'any'} {rule.source_ip} -> {rule.destination_ip} "
                        f"dport {rule.destination_port or 'any'}"
                    )
            return {"output": "\n".join(lines), "device": device_name}

        if normalized in {"show mac address-table", "show mac-address-table"}:
            device = self.simulator.topology.get_device(device_name)
            if not device.is_switch:
                raise LabError("show mac address-table is only available on switches")
            rows = self.simulator.mac_table(device_name)
            if not rows:
                return {"output": "Mac Address Table is empty.", "entries": []}
            lines = [f"{'MAC Address':<20}{'Interface':<12}Type"]
            lines.extend(
                f"{row['mac_address']:<20}{row['interface_id']:<12}{row['type']}" for row in rows
            )
            return {"output": "\n".join(lines), "entries": rows}

        if normalized == "show vlan brief":
            device = self.simulator.topology.get_device(device_name)
            rows = [
                {
                    "vlan": "1",
                    "name": "default",
                    "status": "active",
                    "ports": [interface.interface_id for interface in device.interfaces],
                }
            ]
            lines = [f"{'VLAN':<6}{'Name':<12}{'Status':<10}Ports"]
            lines.append(f"{'1':<6}{'default':<12}{'active':<10}{', '.join(rows[0]['ports'])}")
            return {"output": "\n".join(lines), "vlans": rows}

        if normalized in {"show security", "show arp security"}:
            state = services.security_state()
            lines = [
                f"ARP detection: {'enabled' if state['arp']['detection_enabled'] else 'disabled'}",
                f"ARP protection: {'enabled' if state['arp']['protection_enabled'] else 'disabled'}",
                f"Spoof attempts: {state['arp']['spoof_attempts']}  "
                f"conflicts: {len(state['arp']['conflicts'])}",
                f"Flood threshold: {state['flood']['threshold']:g} pps  "
                f"protection: {'enabled' if state['flood']['protection_enabled'] else 'disabled'}",
                f"Detected floods: {state['flood']['detected_floods']}",
            ]
            for conflict in state["arp"]["conflicts"]:
                lines.append(
                    f"  CONFLICT {conflict['ip_address']} {conflict['known_mac']} -> "
                    f"{conflict['new_mac']} on {conflict['device']} "
                    f"({conflict.get('attacker')}) severity={conflict['severity']}"
                )
            return {"output": "\n".join(lines), "security": state}

        return None

    # ------------------------------------------------------------------
    # Stage 11 learning, challenges, quiz, demos, and reporting
    # ------------------------------------------------------------------
    def learning_mode(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "categories": LEARNING_CATEGORIES,
                "step_mode": self.learning.state(),
                "result": deepcopy(self.learning_result),
            }

    def run_learning_topic(self, topic_id: str) -> Dict[str, Any]:
        with self.lock:
            try:
                result = self.learning.run(topic_id)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self.learning_result = result
            self.step_mode = self.learning.state()
            self.running = True
            return self.state()

    def learning_step(self, action: str) -> Dict[str, Any]:
        with self.lock:
            action = str(action).lower()
            if action == "next":
                self.learning.advance(1)
            elif action == "previous":
                self.learning.back(1)
            elif action == "play":
                self.learning.playing = True
            elif action == "pause":
                self.learning.playing = False
            elif action == "reset":
                self.learning.reset_steps()
            else:
                raise LabError(f"Unknown step action: {action}")
            self.step_mode = self.learning.state()
            return self.state()

    def challenge_mode(self, action: str = "state", challenge_id: Optional[str] = None,
                       key: Optional[str] = None, value: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            try:
                if action == "start":
                    result = self.challenges.start(str(challenge_id))
                elif action == "reset":
                    result = self.challenges.reset()
                elif action == "hint":
                    result = self.challenges.hint()
                elif action == "answer":
                    result = self.challenges.submit_answer(str(key), str(value))
                elif action == "evaluate":
                    result = self.challenges.evaluate()
                else:
                    result = self.challenges.state()
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self.challenge_state = result
            return self.state()

    def quiz_mode(self, action: str = "state", answer: Any = None) -> Dict[str, Any]:
        with self.lock:
            action = str(action).lower()
            try:
                if action == "start":
                    result = self.quiz.start()
                elif action == "submit":
                    result = self.quiz.submit(answer)
                elif action == "next":
                    result = self.quiz.next_question()
                else:
                    result = self.quiz.state()
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self.quiz_state = result
            return self.state()

    def demo_mode(self, action: str = "state", demo_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            action = str(action).lower()
            try:
                if action == "run":
                    result = self.demos.run(str(demo_id))
                elif action == "reset":
                    result = self.demos.reset(demo_id)
                elif action == "catalog":
                    result = {"demos": DemoEngine.catalog()}
                else:
                    result = {"demos": DemoEngine.catalog(), "result": deepcopy(self.demo_result)}
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            return self.state()

    def packet_journey(self, packet_id: int) -> Dict[str, Any]:
        with self.lock:
            try:
                journey = build_packet_journey(self.simulator, packet_id)
            except ValueError as exc:
                raise LabError(str(exc)) from exc
            self.last_journey = journey
            return self.state()

    def explain(self, packet_id: Optional[int] = None,
                result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Explain why a packet or request failed, from real event data."""

        with self.lock:
            packet = None
            if packet_id is not None:
                packet = next(
                    (item for item in self.simulator.packets if item.packet_id == int(packet_id)),
                    None,
                )
                if packet is None:
                    raise LabError(f"Unknown packet: {packet_id}")
            if result is None:
                result = self.service_result if isinstance(self.service_result, dict) else None
            if packet is None and result is None:
                candidate = next(
                    (item for item in reversed(self.simulator.packets)
                     if item.delivery_status == "DROPPED"),
                    None,
                )
                packet = candidate
            explanation = explain_failure(self.simulator, packet, result)
            self.last_explanation = explanation
            return self.state()

    def health_report(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "overview": network_overview(self),
                "health": network_health(self),
            }

    def network_report(self, fmt: str = "json") -> Dict[str, Any]:
        with self.lock:
            report = network_report(self)
            fmt = str(fmt).lower()
            if fmt == "markdown":
                return {"format": "markdown", "text": report_to_markdown(report)}
            if fmt == "csv":
                return {"format": "csv", "text": report_csv(report),
                        "rows": report_rows(report)}
            if fmt == "rows":
                return {"format": "rows", "rows": report_rows(report)}
            return {"format": "json", "report": report}

    def undo(self) -> Dict[str, Any]:
        with self.lock:
            if not self.history:
                return self.state()
            self.future.append(self.export_document())
            return self.import_document(self.history.pop(), record=False)

    def redo(self) -> Dict[str, Any]:
        with self.lock:
            if not self.future:
                return self.state()
            self.history.append(self.export_document())
            return self.import_document(self.future.pop(), record=False)

    def _packet_states(self) -> List[Dict[str, Any]]:
        packets = [
            self.simulator.protocols.packet_details(packet)
            for packet in self.simulator.protocols.protocol_packets.values()
        ]
        for packet in self.simulator.packets[-100:]:
            events = [
                event.to_dict()
                for event in self.simulator.event_logger.events
                if event.details.get("packet_id") == packet.packet_id
            ]
            flow = next((item for item in self.simulator.active_flows.values() if packet.packet_id in item.packet_ids), None)
            drop_reason = next((event["details"].get("reason") for event in reversed(events) if event["details"].get("reason")), None)
            if packet.delivery_status == "DROPPED" and drop_reason == "NO_ROUTE":
                drop_reason = "DESTINATION_UNREACHABLE"
            transport_packet = getattr(packet, "transport", None)
            packet_state = {
                "id": packet.packet_id,
                "packet_id": packet.packet_id,
                "source": packet.source,
                "destination": packet.destination,
                "protocol": "IP",
                "traffic_class": packet.traffic_type,
                "size": packet.size,
                "ttl": packet.ttl,
                "flow_id": flow.flow_id if flow else None,
                "route": list(packet.route),
                "status": packet.delivery_status,
                "latency": packet.latency,
                "current_device": packet.destination if packet.delivery_status == "DELIVERED" else packet.source,
                "next_hop": packet.route[1] if packet.delivery_status == "PENDING" and len(packet.route) > 1 else None,
                "queue_wait_time": packet.queue_wait_time,
                "queue_wait_ms": packet.queue_wait_time * 1000 if packet.queue_wait_time is not None else None,
                "drop_reason": drop_reason,
                "journey": events,
            }
            if transport_packet is not None:
                packet_state.update(transport_packet.to_dict())
                packet_state["size"] = transport_packet.payload_size
                packet_ids = {packet.packet_id, transport_packet.packet_id}
                transport_events = [
                    event.to_dict()
                    for event in self.simulator.event_logger.events
                    if event.details.get("packet_id") in packet_ids
                    or (event.flow_id == transport_packet.flow_id and event.details.get("sequence_number") == transport_packet.sequence_number)
                ]
                packet_state["journey"] = transport_events or events
                packet_state["drop_reason"] = transport_packet.drop_reason or drop_reason
            service_info = getattr(packet, "service", None)
            if service_info:
                packet_state["service"] = dict(service_info)
                packet_state["service_name"] = service_info.get("service")
            security_info = getattr(packet, "security", None)
            if security_info:
                packet_state["security"] = dict(security_info)
            packets.append(packet_state)
        return packets[-200:]

    def state(self) -> Dict[str, Any]:
        with self.lock:
            self._update_document()
            topology = self.simulator.topology
            metrics = self.simulator.metrics.calculate(self.simulator.time, self.simulator.average_congestion(), len(self.simulator.active_flows))
            queue = self.simulator.scheduler_statistics()
            devices = []
            for name, device in topology.devices.items():
                position = self.document.get("positions", {}).get(name, {"x": 200, "y": 200})
                devices.append({
                    "id": name,
                    "name": name,
                    "type": device.device_type,
                    "x": position["x"],
                    "y": position["y"],
                    "status": device.status,
                    "default_gateway": device.default_gateway,
                    "interfaces": [i.to_dict() for i in device.interfaces],
                    "mac_table": dict(device.mac_table) if device.is_switch else {},
                })
            links = []
            for u, v, edge in topology.graph.edges(data=True):
                links.append({
                    "id": f"{u}--{v}", "source": u, "target": v,
                    "status": edge.get("status", "UP"),
                    "latency": float(edge.get("latency", 0.0)),
                    "bandwidth": float(edge.get("bandwidth", 0.0)),
                    "packet_loss": float(edge.get("packet_loss", 0.0)),
                    "congestion": float(edge.get("congestion", 0.0)),
                    "duplex": edge.get("duplex", "Full"),
                    "interface_a": edge.get("interface_a", "eth0"),
                    "interface_b": edge.get("interface_b", "eth0"),
                })
            flows = []
            for flow in self.simulator.active_flows.values():
                flows.append({"id": flow.flow_id, "source": flow.source, "destination": flow.destination, "route": flow.current_route, "status": flow.status, "delivered": flow.packets_delivered, "dropped": flow.packets_dropped})
            routing_tables = {}
            for device in topology.devices.values():
                if device.is_router:
                    routing_tables[device.name] = [entry.to_dict() for entry in self.simulator.routing_table(device.name).entries]
            return {
                "name": self.document.get("name", "Untitled Network"),
                "time": self.simulator.time,
                "running": self.running,
                "speed": self.speed,
                "devices": devices,
                "links": links,
                "flows": flows,
                "last_packet": self.last_packet,
                "packets": self._packet_states(),
                "diagnostic_result": deepcopy(self.diagnostic_result),
                "transport": {
                    "flows": self.simulator.get_transport_flows(),
                    "connections": self.simulator.get_transport_statistics()["connections"],
                    "udp_flows": self.simulator.get_transport_statistics()["udp_flows"],
                    "statistics": self.simulator.get_transport_statistics(),
                    "result": deepcopy(self.transport_result),
                },
                "queue": {"length": queue.get("current_queue_length", 0), "max": queue.get("max_queue_length", 0), "average_wait": queue.get("average_waiting_time", 0.0)},
                "metrics": {"sent": metrics["packets_sent"], "delivered": metrics["packets_delivered"], "dropped": metrics["packets_dropped"], "pdr": metrics["packet_delivery_ratio"], "latency": metrics["average_latency"] * 1000, "throughput": metrics["throughput"], "route_changes": sum(1 for event in self.simulator.event_logger.events if event.event_type == "ROUTE_RECALCULATED")},
                "events": [event.to_dict() for event in self.simulator.event_logger.get_events(limit=40)],
                "routing": {"algorithm": self.simulator.get_router_algorithm(), "weights": self.simulator.get_routing_weights()},
                "qos": {
                    "scheduler": self.simulator.scheduler_name,
                    "priorities": self.simulator.get_priority_config(),
                    "weights": self.simulator.get_wfq_weights(),
                },
                "routing_tables": routing_tables,
                "services": {
                    **self.simulator.service_state(),
                    "result": deepcopy(self.service_result),
                },
                "security": {
                    **self.simulator.security_state(),
                    "result": deepcopy(self.security_result),
                },
                "overview": network_overview(self),
                "health": network_health(self),
                "learning": {
                    "categories": LEARNING_CATEGORIES,
                    "step_mode": self.learning.state(),
                    "result": deepcopy(self.learning_result),
                },
                "challenges": deepcopy(self.challenge_state) or self.challenges.state(),
                "quiz": deepcopy(self.quiz_state) or self.quiz.state(),
                "demos": {
                    "catalog": DemoEngine.catalog(),
                    "result": deepcopy(self.demo_result),
                },
                "journey": deepcopy(self.last_journey),
                "explanation": deepcopy(self.last_explanation),
                "can_undo": bool(self.history),
                "can_redo": bool(self.future),
            }


def _device(kind: str, name: str, x: float, y: float) -> Dict[str, Any]:
    return {"id": name, "type": kind, "x": x, "y": y, "interfaces": []}


def _link(source: str, target: str, latency: float = 5.0, bandwidth: float = 100.0) -> Dict[str, Any]:
    return {"id": f"{source}--{target}", "source": source, "target": target, "status": "UP", "latency": latency, "bandwidth": bandwidth, "packet_loss": 0.0, "congestion": 0.0, "duplex": "Full", "interface_a": "eth0", "interface_b": "eth0"}


PRESETS: Dict[str, Dict[str, Any]] = {
    "self_healing": {"version": 1, "name": "Self-Healing Demo", "settings": {"seed": 42, "scheduler": "priority", "routing_algorithm": "dijkstra"}, "devices": [_device("host", "PC1", 100, 220), _device("switch", "SW1", 270, 220), _device("router", "R1", 450, 160), _device("router", "R2", 650, 160), _device("router", "R3", 550, 350), _device("switch", "SW2", 820, 220), _device("host", "PC2", 980, 220), _device("server", "Server1", 650, 430)], "links": [_link("PC1", "SW1"), _link("SW1", "R1"), _link("R1", "R2", 12), _link("R1", "R3", 15), _link("R3", "R2", 15), _link("R2", "SW2"), _link("SW2", "PC2"), _link("R3", "Server1")]},
    "simple_lan": {"version": 1, "name": "Simple LAN", "settings": {"seed": 42}, "devices": [_device("host", "PC1", 150, 200), _device("switch", "SW1", 450, 200), _device("server", "Server1", 750, 200)], "links": [_link("PC1", "SW1"), _link("SW1", "Server1")]},
    "two_router": {"version": 1, "name": "Two Router Network", "settings": {"seed": 42}, "devices": [_device("host", "PC1", 100, 200), _device("router", "R1", 300, 200), _device("router", "R2", 600, 200), _device("host", "PC2", 850, 200)], "links": [_link("PC1", "R1"), _link("R1", "R2", 12), _link("R2", "PC2")]},
    "redundant": {"version": 1, "name": "Redundant Network", "settings": {"seed": 42}, "devices": [_device("host", "PC1", 100, 250), _device("router", "R1", 300, 150), _device("router", "R2", 600, 150), _device("router", "R3", 450, 380), _device("host", "PC2", 800, 250)], "links": [_link("PC1", "R1"), _link("R1", "R2"), _link("R1", "R3"), _link("R3", "R2"), _link("R2", "PC2")]},
    "qos_demo": {"version": 1, "name": "QoS Demo", "settings": {"seed": 42, "scheduler": "wfq"}, "devices": [_device("host", "PC1", 120, 200), _device("router", "R1", 360, 200), _device("switch", "SW1", 620, 200), _device("server", "Server1", 850, 200)], "links": [_link("PC1", "R1"), _link("R1", "SW1", 10, 25), _link("SW1", "Server1")]},
    "large_network": {"version": 1, "name": "Large Network", "settings": {"seed": 42}, "devices": [_device("host", f"PC{i}", 80 + (i % 4) * 180, 100 + (i // 4) * 150) for i in range(1, 9)] + [_device("router", f"R{i}", 350 + (i % 3) * 180, 120 + (i // 3) * 180) for i in range(1, 5)] + [_device("switch", "SW1", 550, 300)] + [_device("server", "Server1", 900, 300)], "links": [_link("PC1", "R1"), _link("PC2", "R1"), _link("PC3", "R2"), _link("PC4", "R2"), _link("PC5", "R3"), _link("PC6", "R3"), _link("PC7", "R4"), _link("PC8", "R4"), _link("R1", "SW1"), _link("R2", "SW1"), _link("R3", "SW1"), _link("R4", "SW1"), _link("SW1", "Server1")]},
    "existing_default": {"version": 1, "name": "Existing Default Topology", "settings": {"seed": 42}, "devices": [_device("host", n, 80 + (i % 4) * 210, 100 + (i // 4) * 180) for i, n in enumerate(["H1", "H2", "H3", "H4"])] + [_device("router", n, 260 + (i % 3) * 190, 110 + (i // 3) * 190) for i, n in enumerate(["R1", "R2", "R3", "R4", "R5", "R6"])], "links": [_link("H1", "R1"), _link("H2", "R2"), _link("H3", "R5"), _link("H4", "R6"), _link("R1", "R2", 10), _link("R1", "R3", 15), _link("R2", "R4", 12), _link("R3", "R4", 8), _link("R3", "R5", 15), _link("R4", "R5", 10), _link("R4", "R6", 18), _link("R5", "R6", 10)]},
}
