from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple
import networkx as nx

from devices import Device, NetworkInterface, default_address, deterministic_mac


@dataclass
class Link:
    latency: float = 10.0
    bandwidth: float = 100.0
    packet_loss: float = 0.0
    congestion: float = 0.0
    status: str = "UP"


# Fixed layout for visualization consistency
DEFAULT_POSITIONS: Dict[str, Tuple[float, float]] = {
    "H1": (-2.5, 1.5),
    "H2": (-2.5, -1.5),
    "H3": (2.5, 1.5),
    "H4": (2.5, -1.5),
    "R1": (-1.5, 0.8),
    "R2": (-1.5, -0.8),
    "R3": (-0.3, 0.8),
    "R4": (-0.3, -0.8),
    "R5": (1.2, 0.8),
    "R6": (1.2, -0.8),
}


class NetworkTopology:
    def __init__(self) -> None:
        self.graph = nx.Graph()
        # Stage 7 device/interface model.  The graph remains authoritative for
        # links and routing so all pre-existing topology tests stay compatible.
        self.devices: Dict[str, Device] = {}
        self._build_default_topology()
        # Store original bandwidth for reset and bandwidth reduction tracking
        self._original_bandwidth: Dict[Tuple[str, str], float] = {}
        for u, v, data in self.graph.edges(data=True):
            key = tuple(sorted((u, v)))
            self._original_bandwidth[key] = data.get("bandwidth", 100.0)

    def _build_default_topology(self) -> None:
        nodes = [
            ("H1", "host"), ("H2", "host"),
            ("H3", "host"), ("H4", "host"),
            ("R1", "router"), ("R2", "router"),
            ("R3", "router"), ("R4", "router"),
            ("R5", "router"), ("R6", "router"),
        ]

        for node, node_type in nodes:
            self.add_node(node, node_type)

        links = [
            ("H1", "R1", 5, 100),
            ("H2", "R2", 5, 100),
            ("H3", "R5", 5, 100),
            ("H4", "R6", 5, 100),

            ("R1", "R2", 10, 100),
            ("R1", "R3", 15, 80),
            ("R2", "R4", 12, 100),
            ("R3", "R4", 8, 100),
            ("R3", "R5", 15, 80),
            ("R4", "R5", 10, 100),
            ("R4", "R6", 18, 80),
            ("R5", "R6", 10, 100),
        ]

        for u, v, latency, bandwidth in links:
            self.add_link(u, v, latency, bandwidth)

    def add_node(self, node: str, node_type: str = "router") -> None:
        self.graph.add_node(
            node,
            type=node_type,
            status="UP",
        )
        if node not in self.devices:
            address, prefix = default_address(node, node_type)
            gateway = {"H1": "R1", "H2": "R2", "H3": "R5", "H4": "R6"}.get(node)
            self.devices[node] = Device(
                node,
                node_type,
                [NetworkInterface("eth0", mac_address=deterministic_mac(f"{node}-eth0"), ip_address=address, prefix=prefix)],
                default_gateway=gateway if node_type in {"host", "pc"} else None,
            )

    def remove_node(self, node: str) -> None:
        if node in self.graph:
            self.graph.remove_node(node)
        self.devices.pop(node, None)

    def add_link(
        self,
        u: str,
        v: str,
        latency: float = 10.0,
        bandwidth: float = 100.0,
        packet_loss: float = 0.0,
        congestion: float = 0.0,
    ) -> None:
        self.graph.add_edge(
            u,
            v,
            latency=float(latency),
            bandwidth=float(bandwidth),
            packet_loss=float(packet_loss),
            congestion=float(congestion),
            status="UP",
            original_bandwidth=float(bandwidth),
        )
        key = tuple(sorted((u, v)))
        if not hasattr(self, '_original_bandwidth'):
            self._original_bandwidth = {}
        self._original_bandwidth[key] = float(bandwidth)
        if hasattr(self, "devices"):
            for endpoint, neighbour in ((u, v), (v, u)):
                device = self.devices.get(endpoint)
                if device is not None:
                    for interface in device.interfaces:
                        interface.associate_link(key[0] + "-" + key[1])
                        interface.associate_link(f"{endpoint}-{neighbour}")

    def remove_link(self, u: str, v: str) -> None:
        if self.graph.has_edge(u, v):
            self.graph.remove_edge(u, v)

    def fail_node(self, node: str) -> None:
        if node in self.graph:
            self.graph.nodes[node]["status"] = "DOWN"
            if node in self.devices:
                self.devices[node].set_status("DOWN")

    def recover_node(self, node: str) -> None:
        if node in self.graph:
            self.graph.nodes[node]["status"] = "UP"
            if node in self.devices:
                self.devices[node].set_status("UP")

    def fail_link(self, u: str, v: str) -> None:
        if self.graph.has_edge(u, v):
            self.graph[u][v]["status"] = "DOWN"

    def recover_link(self, u: str, v: str) -> None:
        if self.graph.has_edge(u, v):
            self.graph[u][v]["status"] = "UP"

    def update_link(self, u: str, v: str, **values: float) -> None:
        if not self.graph.has_edge(u, v):
            raise ValueError(f"Link {u}-{v} does not exist.")

        allowed = {"latency", "bandwidth", "packet_loss", "congestion"}

        for key, value in values.items():
            if key in allowed:
                self.graph[u][v][key] = float(value)

    def set_congestion(self, u: str, v: str, value: float) -> None:
        self.update_link(u, v, congestion=max(0.0, min(1.0, value)))

    def set_packet_loss(self, u: str, v: str, value: float) -> None:
        self.update_link(u, v, packet_loss=max(0.0, min(1.0, value)))

    def set_bandwidth(self, u: str, v: str, value: float) -> None:
        """Set bandwidth in Mbps, clamped to minimum 0.1 Mbps."""
        clamped = max(0.1, float(value))
        self.update_link(u, v, bandwidth=clamped)

    def get_link(self, u: str, v: str) -> Dict[str, Any] | None:
        if self.graph.has_edge(u, v):
            return dict(self.graph[u][v])
        return None

    def get_original_bandwidth(self, u: str, v: str) -> float:
        key = tuple(sorted((u, v)))
        return self._original_bandwidth.get(key, 100.0)

    def active_node(self, node: str) -> bool:
        return (
            node in self.graph
            and self.graph.nodes[node].get("status") == "UP"
        )

    def active_link(self, u: str, v: str) -> bool:
        return (
            self.graph.has_edge(u, v)
            and self.active_node(u)
            and self.active_node(v)
            and self.graph[u][v].get("status") == "UP"
        )

    def active_graph(self) -> nx.Graph:
        g = nx.Graph()

        for node, data in self.graph.nodes(data=True):
            if data.get("status") == "UP":
                g.add_node(node, **data)

        for u, v, data in self.graph.edges(data=True):
            if self.active_link(u, v):
                g.add_edge(u, v, **data)

        return g

    def get_device(self, node: str) -> Device | None:
        """Return the Stage 7 device associated with a topology node."""

        return self.devices.get(node)

    def add_interface(self, node: str, interface: NetworkInterface) -> NetworkInterface:
        if node not in self.devices:
            raise ValueError(f"Node {node} does not exist.")
        return self.devices[node].add_interface(interface)

    def fail_interface(self, node: str, interface_id: str) -> list[Tuple[str, str]]:
        """Mark an interface down and return the associated links to fail."""

        device = self.get_device(node)
        if device is None:
            raise ValueError(f"Node {node} does not exist.")
        interface = device.get_interface(interface_id)
        interface.set_status("DOWN")
        links: list[Tuple[str, str]] = []
        for association in interface.link_associations:
            if "-" in association:
                left, right = association.split("-", 1)
                if self.graph.has_edge(left, right):
                    links.append((left, right))
        return list(dict.fromkeys(links))

    def recover_interface(self, node: str, interface_id: str) -> list[Tuple[str, str]]:
        device = self.get_device(node)
        if device is None:
            raise ValueError(f"Node {node} does not exist.")
        device.get_interface(interface_id).set_status("UP")
        links: list[Tuple[str, str]] = []
        for association in device.get_interface(interface_id).link_associations:
            if "-" in association:
                left, right = association.split("-", 1)
                if self.graph.has_edge(left, right):
                    links.append((left, right))
        return list(dict.fromkeys(links))

    def clear(self) -> None:
        """Remove all graph nodes/links and device objects for an editable lab."""

        self.graph.clear()
        self.devices.clear()
        self._original_bandwidth.clear()

    def reset(self) -> None:
        for _, data in self.graph.nodes(data=True):
            data["status"] = "UP"
        for device in self.devices.values():
            device.set_status("UP")

        for u, v, data in self.graph.edges(data=True):
            data["status"] = "UP"
            data["packet_loss"] = 0.0
            data["congestion"] = 0.0
            # Restore original bandwidth
            key = tuple(sorted((u, v)))
            orig = self._original_bandwidth.get(key, data.get("original_bandwidth", 100.0))
            data["bandwidth"] = orig

    def nodes(self) -> list[str]:
        return list(self.graph.nodes)

    def links(self) -> list[tuple[str, str]]:
        return list(self.graph.edges)

    def summary(self) -> dict[str, Any]:
        return {
            "nodes": self.graph.number_of_nodes(),
            "links": self.graph.number_of_edges(),
            "active_nodes": sum(self.active_node(n) for n in self.graph.nodes),
            "active_links": sum(
                self.active_link(u, v) for u, v in self.graph.edges
            ),
        }

    def get_positions(self) -> Dict[str, Tuple[float, float]]:
        """Return fixed positions for visualization."""
        # Use default positions for known nodes, spring layout for others
        pos = {}
        for node in self.graph.nodes:
            if node in DEFAULT_POSITIONS:
                pos[node] = DEFAULT_POSITIONS[node]
        # For any extra nodes, use spring layout as fallback
        if len(pos) < self.graph.number_of_nodes():
            try:
                spring = nx.spring_layout(self.graph, seed=42)
                for node in self.graph.nodes:
                    if node not in pos:
                        pos[node] = tuple(spring[node])
            except Exception:
                for node in self.graph.nodes:
                    if node not in pos:
                        pos[node] = (0.0, 0.0)
        return pos

    def link_uses_node(self, link: Tuple[str, str], node: str) -> bool:
        return node in link

    def device(self, node: str) -> Device | None:
        """Alias for get_device used by diagnostics."""
        return self.get_device(node)
