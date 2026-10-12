"""
Adaptive routing for NetAdapt (Stage 5).

Two shortest-path algorithms are implemented from scratch and can be selected at
runtime:

* **Dijkstra** -- label-setting with a binary heap.
* **Bellman-Ford** -- label-correcting edge relaxation.

Both operate on the same ``NetworkTopology`` and the same dynamic link cost, so
Dijkstra and Bellman-Ford can be compared on identical network conditions. The
cost weights (latency / loss / congestion / bandwidth / hop count) are fully
configurable, and changing them changes the actual route selected whenever the
network conditions make alternative routes differ.
"""

from __future__ import annotations

from math import inf
from typing import Any, Dict, List, Optional, Tuple
import heapq

from topology import NetworkTopology

#: Cost components that can be weighted.
ROUTING_WEIGHT_NAMES: Tuple[str, ...] = (
    "latency",
    "loss",
    "congestion",
    "bandwidth",
    "hop",
)

DEFAULT_ROUTING_WEIGHTS: Dict[str, float] = {
    "latency": 1.0,
    "loss": 20.0,
    "congestion": 20.0,
    "bandwidth": 5.0,
    "hop": 2.0,
}

ROUTING_ALGORITHMS: Tuple[str, ...] = ("dijkstra", "bellman_ford")

ALGORITHM_LABELS: Dict[str, str] = {
    "dijkstra": "Dijkstra",
    "bellman_ford": "Bellman-Ford",
}


def normalize_algorithm(name: str) -> str:
    """Return a canonical algorithm key (``dijkstra`` / ``bellman_ford``)."""
    key = str(name).lower().replace("-", " ").replace("_", " ").strip()
    key = " ".join(key.split())
    if key in {"dijkstra", "dij"}:
        return "dijkstra"
    if key in {"bellman ford", "bellman", "bf"}:
        return "bellman_ford"
    raise ValueError(f"Unknown routing algorithm: {name}")


def normalize_routing_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Validate / complete a routing weight mapping."""
    resolved = dict(DEFAULT_ROUTING_WEIGHTS)

    if weights:
        unknown = set(weights) - set(resolved)
        if unknown:
            raise ValueError(f"Unknown routing weight names: {sorted(unknown)}")
        for name, value in weights.items():
            if float(value) < 0:
                raise ValueError(f"Routing weight {name} must be non-negative.")
            resolved[name] = float(value)

    return resolved


def effective_link_loss(data: Dict[str, Any]) -> float:
    """Packet loss used for routing decisions: configured loss plus congestion."""
    loss = max(0.0, min(1.0, float(data.get("packet_loss", 0.0))))
    congestion = max(0.0, min(1.0, float(data.get("congestion", 0.0))))
    return min(1.0, loss + congestion * 0.05)


class AdaptiveRouter:
    """Dynamic cost router with selectable Dijkstra / Bellman-Ford."""

    def __init__(
        self,
        latency_weight: float = 1.0,
        loss_weight: float = 20.0,
        congestion_weight: float = 20.0,
        bandwidth_weight: float = 5.0,
        hop_weight: float = 2.0,
        algorithm: str = "dijkstra",
        weights: Optional[Dict[str, float]] = None,
    ) -> None:
        self.weights: Dict[str, float] = normalize_routing_weights(
            weights
            if weights is not None
            else {
                "latency": latency_weight,
                "loss": loss_weight,
                "congestion": congestion_weight,
                "bandwidth": bandwidth_weight,
                "hop": hop_weight,
            }
        )
        self.algorithm: str = normalize_algorithm(algorithm)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def get_weights(self) -> Dict[str, float]:
        return dict(self.weights)

    def configure_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Merge new routing weights (validated, non-negative)."""
        self.weights = normalize_routing_weights({**self.weights, **weights})
        return dict(self.weights)

    def set_weights(self, **weights: float) -> Dict[str, float]:
        """Set routing weights by keyword, e.g. ``set_weights(loss=50)``."""
        return self.configure_weights(dict(weights))

    def reset_weights(self) -> Dict[str, float]:
        self.weights = dict(DEFAULT_ROUTING_WEIGHTS)
        return dict(self.weights)

    def set_algorithm(self, algorithm: str) -> str:
        self.algorithm = normalize_algorithm(algorithm)
        return self.algorithm

    def get_algorithm(self) -> str:
        return self.algorithm

    # ------------------------------------------------------------------
    # Link cost
    # ------------------------------------------------------------------
    def _cost(self, data: Dict[str, Any]) -> float:
        """
        Dynamic link cost.

        ``cost = w_latency * latency
                 + w_loss * loss * 100
                 + w_congestion * congestion * 100
                 + w_bandwidth * (100 / bandwidth)
                 + w_hop``

        Weights are read on every call, so weight changes immediately affect the
        route computation.
        """
        latency = max(0.0, float(data.get("latency", 0.0)))
        loss = effective_link_loss(data)
        congestion = max(0.0, min(1.0, float(data.get("congestion", 0.0))))
        bandwidth = max(0.001, float(data.get("bandwidth", 100.0)))

        bandwidth_cost = 100.0 / bandwidth

        return (
            self.weights["latency"] * latency
            + self.weights["loss"] * loss * 100.0
            + self.weights["congestion"] * congestion * 100.0
            + self.weights["bandwidth"] * bandwidth_cost
            + self.weights["hop"]
        )

    # Alias kept for readability in callers that reason about link cost.
    def link_cost(self, data: Dict[str, Any]) -> float:
        return self._cost(data)

    # ------------------------------------------------------------------
    # Algorithms
    # ------------------------------------------------------------------
    def _require_graph(
        self, topology: NetworkTopology, source: str, destination: str
    ):
        graph = topology.active_graph()

        if source not in graph or destination not in graph:
            raise ValueError("Source or destination is unavailable.")

        return graph

    def _dijkstra(
        self, graph, source: str, destination: str
    ) -> Tuple[List[str], float, Dict[str, Any]]:
        distances: Dict[str, float] = {node: inf for node in graph}
        previous: Dict[str, Optional[str]] = {node: None for node in graph}
        distances[source] = 0.0

        queue: List[Tuple[float, str]] = [(0.0, source)]
        settled: List[str] = []
        relaxations = 0

        while queue:
            distance, current = heapq.heappop(queue)

            if distance > distances[current]:
                continue

            settled.append(current)

            if current == destination:
                break

            for neighbor in sorted(graph.neighbors(current)):
                edge = graph[current][neighbor]
                new_distance = distance + self._cost(edge)

                if new_distance < distances[neighbor] - 1e-12:
                    distances[neighbor] = new_distance
                    previous[neighbor] = current
                    heapq.heappush(queue, (new_distance, neighbor))
                    relaxations += 1

        if distances[destination] == inf:
            raise ValueError(f"No active route from {source} to {destination}.")

        path = self._reconstruct(previous, source, destination)
        stats = {
            "algorithm": "Dijkstra",
            "settled_nodes": len(settled),
            "relaxations": relaxations,
            "nodes_visited": len(settled),
        }
        return path, distances[destination], stats

    def _bellman_ford(
        self, graph, source: str, destination: str
    ) -> Tuple[List[str], float, Dict[str, Any]]:
        nodes = sorted(graph.nodes)
        directed_edges: List[Tuple[str, str, float]] = []
        for u, v, data in graph.edges(data=True):
            cost = self._cost(data)
            directed_edges.append((u, v, cost))
            directed_edges.append((v, u, cost))

        distances: Dict[str, float] = {node: inf for node in nodes}
        previous: Dict[str, Optional[str]] = {node: None for node in nodes}
        distances[source] = 0.0

        relaxations = 0
        iterations = 0

        for _ in range(max(0, len(nodes) - 1)):
            iterations += 1
            changed = False
            for u, v, cost in directed_edges:
                if distances[u] == inf:
                    continue
                if distances[u] + cost < distances[v] - 1e-12:
                    distances[v] = distances[u] + cost
                    previous[v] = u
                    changed = True
                    relaxations += 1
            if not changed:
                break

        # Negative-cycle detection (all costs are non-negative by construction,
        # so this only ever fires on a genuine modelling bug).
        for u, v, cost in directed_edges:
            if distances[u] != inf and distances[u] + cost < distances[v] - 1e-9:
                raise ValueError("Negative cycle detected in routing cost.")

        if distances[destination] == inf:
            raise ValueError(f"No active route from {source} to {destination}.")

        path = self._reconstruct(previous, source, destination)
        stats = {
            "algorithm": "Bellman-Ford",
            "iterations": iterations,
            "relaxations": relaxations,
            "nodes_visited": len(nodes),
        }
        return path, distances[destination], stats

    @staticmethod
    def _reconstruct(
        previous: Dict[str, Optional[str]], source: str, destination: str
    ) -> List[str]:
        path: List[str] = []
        current: Optional[str] = destination

        while current is not None:
            path.append(current)
            if current == source:
                break
            current = previous[current]

        path.reverse()

        if not path or path[0] != source:
            raise ValueError(f"No active route from {source} to {destination}.")

        return path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def solve(
        self,
        topology: NetworkTopology,
        source: str,
        destination: str,
        algorithm: Optional[str] = None,
    ) -> Tuple[List[str], float, Dict[str, Any]]:
        """Return ``(path, cost, stats)`` using the selected algorithm."""
        graph = self._require_graph(topology, source, destination)

        selected = normalize_algorithm(algorithm or self.algorithm)

        if selected == "bellman_ford":
            return self._bellman_ford(graph, source, destination)

        return self._dijkstra(graph, source, destination)

    def shortest_path(
        self,
        topology: NetworkTopology,
        source: str,
        destination: str,
        algorithm: Optional[str] = None,
    ) -> Tuple[List[str], float]:
        path, cost, _stats = self.solve(topology, source, destination, algorithm)
        return path, cost

    def dijkstra(
        self, topology: NetworkTopology, source: str, destination: str
    ) -> Tuple[List[str], float]:
        return self.shortest_path(topology, source, destination, "dijkstra")

    def bellman_ford(
        self, topology: NetworkTopology, source: str, destination: str
    ) -> Tuple[List[str], float]:
        return self.shortest_path(topology, source, destination, "bellman_ford")

    def route(
        self,
        topology: NetworkTopology,
        source: str,
        destination: str,
        algorithm: Optional[str] = None,
    ) -> List[str]:
        return self.shortest_path(topology, source, destination, algorithm)[0]

    def route_cost(
        self,
        topology: NetworkTopology,
        path: List[str],
    ) -> float:
        if len(path) < 2:
            return 0.0

        total = 0.0

        for u, v in zip(path, path[1:]):
            if not topology.active_link(u, v):
                return inf
            total += self._cost(topology.graph[u][v])

        return total

    def analyze_route(
        self,
        topology: NetworkTopology,
        path: List[str],
    ) -> Dict[str, Any]:
        """
        Aggregate the real link conditions along ``path``.

        Returns hop count, summed latency, end-to-end loss probability, mean
        congestion, bottleneck bandwidth, minimum bandwidth, and route cost.
        """
        result: Dict[str, Any] = {
            "route": list(path),
            "hops": max(0, len(path) - 1),
            "cost": 0.0,
            "latency_ms": 0.0,
            "packet_loss": 0.0,
            "congestion": 0.0,
            "min_bandwidth_mbps": 0.0,
            "avg_bandwidth_mbps": 0.0,
            "reachable": bool(path),
        }

        if len(path) < 2:
            result["reachable"] = False
            return result

        probability_no_loss = 1.0
        bandwidths: List[float] = []
        congestion_total = 0.0
        latency_total = 0.0
        cost_total = 0.0

        for u, v in zip(path, path[1:]):
            if not topology.active_link(u, v):
                result["reachable"] = False
                result["cost"] = inf
                return result

            data = topology.graph[u][v]
            latency_total += float(data.get("latency", 0.0))
            congestion_total += max(0.0, min(1.0, float(data.get("congestion", 0.0))))
            bandwidth = max(0.001, float(data.get("bandwidth", 100.0)))
            bandwidths.append(bandwidth)
            probability_no_loss *= 1.0 - effective_link_loss(data)
            cost_total += self._cost(data)

        result.update(
            {
                "cost": cost_total,
                "latency_ms": latency_total,
                "packet_loss": min(1.0, 1.0 - probability_no_loss),
                "congestion": congestion_total / len(bandwidths),
                "min_bandwidth_mbps": min(bandwidths),
                "avg_bandwidth_mbps": sum(bandwidths) / len(bandwidths),
            }
        )
        return result

    def route_info(
        self,
        topology: NetworkTopology,
        source: str,
        destination: str,
        algorithm: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Selected route together with its cost, hops and link conditions."""
        path, cost, stats = self.solve(topology, source, destination, algorithm)
        info = self.analyze_route(topology, path)
        info["cost"] = cost
        info["source"] = source
        info["destination"] = destination
        info["algorithm"] = stats["algorithm"]
        info.update(
            {f"algo_{k}": v for k, v in stats.items() if k != "algorithm"}
        )
        return info
