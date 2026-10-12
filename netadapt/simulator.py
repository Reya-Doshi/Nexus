from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from topology import NetworkTopology
from routing import (
    AdaptiveRouter,
    ALGORITHM_LABELS,
    ROUTING_WEIGHT_NAMES,
)
from qos import (
    Packet,
    PRIORITIES,
    TRAFFIC_CLASSES,
    configure_priorities,
    create_scheduler,
    normalize_class_weights,
    normalize_scheduler_name,
    reset_priorities,
)
from metrics import Metrics, RecoveryRecord, TransportMetrics
from events import EventLogger, EventType
from protocols import ProtocolPacket, ProtocolStack, SwitchFrame
from transport import TransportLayer
from services_security import ServiceSecurityLayer


@dataclass
class TrafficFlow:
    """Represents an active traffic flow between two endpoints."""

    flow_id: str
    source: str
    destination: str
    traffic_type: str = "HTTP"
    packet_count: int = 10
    packet_size: int = 1000
    pps: float = 10.0  # packets per second
    duration: float = 5.0  # seconds
    start_time: float = 0.0
    end_time: float | None = None
    status: str = "ACTIVE"  # ACTIVE, COMPLETED, FAILED, PENDING
    current_route: List[str] = field(default_factory=list)
    route_cost: float = 0.0
    route_status: str = "HEALTHY"  # HEALTHY, REROUTED, FAILED
    packets_sent: int = 0
    packets_delivered: int = 0
    packets_dropped: int = 0
    packet_ids: List[int] = field(default_factory=list)
    detection_time: float | None = None
    recovery_time: float | None = None
    previous_route: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "source": self.source,
            "destination": self.destination,
            "traffic_type": self.traffic_type,
            "packet_count": self.packet_count,
            "packet_size": self.packet_size,
            "pps": self.pps,
            "duration": self.duration,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "current_route": self.current_route,
            "route_cost": self.route_cost,
            "route_status": self.route_status,
            "packets_sent": self.packets_sent,
            "packets_delivered": self.packets_delivered,
            "packets_dropped": self.packets_dropped,
        }


class NetworkSimulator:
    """
    Central simulation engine for NetAdapt.

    Connects Topology, Routing, QoS, Metrics, Events, and Traffic Flows.
    Implements self-healing behavior with heartbeat-based failure detection.
    """

    def __init__(
        self,
        topology: NetworkTopology | None = None,
        router: AdaptiveRouter | None = None,
        scheduler: str = "priority",
        seed: int = 42,
        heartbeat_interval: float = 1.0,
        failure_detection_timeout: float = 3.0,
        algorithm: str = "dijkstra",
        scheduler_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.topology = topology or NetworkTopology()
        self.router = router or AdaptiveRouter()
        if algorithm:
            self.router.set_algorithm(algorithm)
        self.scheduler_name = normalize_scheduler_name(scheduler)
        self.scheduler_params: Dict[str, Any] = dict(scheduler_params or {})
        self.scheduler = create_scheduler(self.scheduler_name, **self.scheduler_params)

        self.rng = random.Random(seed)
        self.seed = seed

        self.time = 0.0
        self.next_packet_id = 1
        self.metrics = Metrics()
        self.events: List[Dict[str, Any]] = []  # legacy format for backward compat
        self.packets: List[Packet] = []

        # Enhanced event system
        self.event_logger = EventLogger()
        # Stage 7 protocol services share the live topology, clock, and events.
        self.protocols = ProtocolStack(self)
        self.transport_metrics = TransportMetrics()
        self.transport = TransportLayer(self)
        # Stage 10 is an additive layer on this same simulator.  Service and
        # security traffic is still enqueued as ordinary network packets and
        # therefore uses the existing routing, QoS, loss, and failure paths.
        self.services = ServiceSecurityLayer(self)
        self.service_security = self.services
        self.arp = self.protocols.arp

        # Heartbeat / health check simulation
        self.heartbeat_interval = heartbeat_interval
        self.failure_detection_timeout = failure_detection_timeout
        self.last_heartbeat_check = 0.0

        # Failure tracking: component -> failure_time
        self._failed_links: Dict[Tuple[str, str], float] = {}
        self._failed_nodes: Dict[str, float] = {}
        # Detection tracking: component -> detection_time
        self._detected_links: Dict[Tuple[str, str], float] = {}
        self._detected_nodes: Dict[str, float] = {}

        # Traffic flows
        self.active_flows: Dict[str, TrafficFlow] = {}
        self.flow_counter = 1
        self.flow_history: List[TrafficFlow] = []

        # Performance comparison snapshots
        self.baseline_metrics: Optional[Dict[str, float]] = None
        self.during_failure_metrics: Optional[Dict[str, float]] = None
        self.after_recovery_metrics: Optional[Dict[str, float]] = None

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, event: str, **data) -> None:
        """Legacy log method for backward compatibility."""
        entry = {
            "time": round(self.time, 4),
            "event": event,
            **data,
        }
        self.events.append(entry)

        # Also log to structured logger
        component = data.get("link") or data.get("node") or data.get("component")
        flow_id = data.get("flow_id")
        message = data.get("message") or f"{event} {component or ''}".strip()
        try:
            self.event_logger.log(
                timestamp=self.time,
                event_type=event,
                message=message,
                component=component,
                flow_id=flow_id,
                **{k: v for k, v in data.items() if k not in ("link", "node", "component", "flow_id", "message")},
            )
        except Exception:
            pass

    def _log_event(
        self,
        event_type: str,
        message: str,
        component: Optional[str] = None,
        flow_id: Optional[str] = None,
        **details: Any,
    ) -> None:
        """Structured event logging."""
        self.event_logger.log(
            timestamp=self.time,
            event_type=event_type,
            message=message,
            component=component,
            flow_id=flow_id,
            **details,
        )
        # Legacy mirror
        legacy = {
            "time": round(self.time, 4),
            "event": event_type,
            "message": message,
        }
        if component:
            if "-" in component or "↔" in component:
                legacy["link"] = component
            else:
                legacy["node"] = component
            legacy["component"] = component
        if flow_id:
            legacy["flow_id"] = flow_id
        legacy.update(details)
        self.events.append(legacy)

    # ------------------------------------------------------------------
    # Packet generation (Stage 1 preserved)
    # ------------------------------------------------------------------
    def generate_packet(
        self,
        source: str,
        destination: str,
        traffic_type: str = "HTTP",
        size: int = 1000,
        flow_id: Optional[str] = None,
        creation_time: Optional[float] = None,
        ttl: int = 64,
    ) -> Packet:
        if source == destination:
            raise ValueError("Source and destination must differ.")

        if not self.topology.active_node(source):
            raise ValueError(f"Source {source} is unavailable.")

        if not self.topology.active_node(destination):
            raise ValueError(f"Destination {destination} is unavailable.")

        if traffic_type not in PRIORITIES:
            raise ValueError(f"Unknown traffic type: {traffic_type}")

        ctime = creation_time if creation_time is not None else self.time

        packet = Packet(
            packet_id=self.next_packet_id,
            source=source,
            destination=destination,
            traffic_type=traffic_type,
            size=size,
            creation_time=ctime,
            ttl=ttl,
        )

        # Draw the loss decision at creation so that every configuration being
        # compared (scheduler / routing algorithm) sees exactly the same
        # random realisation.
        packet.loss_roll = self.rng.random()

        self.next_packet_id += 1
        self.packets.append(packet)
        self.scheduler.push(packet, now=max(self.time, ctime))

        self._log_event(
            EventType.PACKET_GENERATED,
            f"Packet {packet.packet_id} generated {source}→{destination} [{traffic_type}]",
            flow_id=flow_id,
            packet_id=packet.packet_id,
            source=source,
            destination=destination,
            traffic_type=traffic_type,
        )

        return packet

    def generate_traffic(
        self,
        source: str,
        destination: str,
        count: int = 10,
        traffic_type: str = "HTTP",
        size: int = 1000,
        pps: float = 10.0,
        flow_id: Optional[str] = None,
    ) -> List[Packet]:
        packets = []
        interval = 1.0 / max(pps, 0.1)
        base_time = self.time
        for i in range(count):
            pkt = self.generate_packet(
                source,
                destination,
                traffic_type,
                size,
                flow_id=flow_id,
                creation_time=base_time + i * interval,
            )
            packets.append(pkt)
        return packets

    # ------------------------------------------------------------------
    # Traffic Flow Management (Stage 2+3)
    # ------------------------------------------------------------------
    def create_flow(
        self,
        source: str,
        destination: str,
        traffic_type: str = "HTTP",
        packet_count: int = 10,
        packet_size: int = 1000,
        pps: float = 10.0,
        duration: float = 5.0,
    ) -> TrafficFlow:
        flow = self._register_flow(
            source,
            destination,
            traffic_type,
            packet_count=packet_count,
            packet_size=packet_size,
            pps=pps,
            duration=duration,
        )

        # Generate packets for this flow
        self._generate_packets_for_flow(flow)

        # Snapshot baseline if first flow
        if len(self.metrics.history) == 0:
            self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))

        return flow

    def _register_flow(
        self,
        source: str,
        destination: str,
        traffic_type: str = "HTTP",
        packet_count: int = 10,
        packet_size: int = 1000,
        pps: float = 10.0,
        duration: float = 5.0,
    ) -> TrafficFlow:
        """Validate and register a traffic flow without generating packets."""
        if source == destination:
            raise ValueError("Source and destination must differ.")
        if not self.topology.active_node(source):
            raise ValueError(f"Source {source} is unavailable.")
        if not self.topology.active_node(destination):
            raise ValueError(f"Destination {destination} is unavailable.")
        if traffic_type not in PRIORITIES:
            raise ValueError(f"Unknown traffic type: {traffic_type}")

        flow_id = f"flow-{self.flow_counter}"
        self.flow_counter += 1

        # Calculate initial route using the live router configuration
        try:
            route, cost = self.router.shortest_path(self.topology, source, destination)
            route_status = "HEALTHY"
        except ValueError:
            route = []
            cost = float("inf")
            route_status = "FAILED"

        flow = TrafficFlow(
            flow_id=flow_id,
            source=source,
            destination=destination,
            traffic_type=traffic_type,
            packet_count=packet_count,
            packet_size=packet_size,
            pps=pps,
            duration=duration,
            start_time=self.time,
            current_route=route,
            route_cost=cost,
            route_status=route_status,
            status="ACTIVE" if route else "FAILED",
        )

        self.active_flows[flow_id] = flow
        self.metrics.active_flows_count = len(self.active_flows)

        self._log_event(
            EventType.TRAFFIC_STARTED,
            f"Flow {flow_id} started {source}→{destination} [{traffic_type}] {packet_count} packets",
            component=f"{source}→{destination}",
            flow_id=flow_id,
            source=source,
            destination=destination,
            traffic_type=traffic_type,
            packet_count=packet_count,
            route=route,
            route_cost=cost,
        )

        return flow

    def generate_interleaved_traffic(
        self,
        source: str,
        destination: str,
        classes: Optional[List[str]] = None,
        packets_per_class: int = 10,
        packet_size: int = 1000,
        pps: float = 10.0,
    ) -> Dict[str, TrafficFlow]:
        """
        Generate a mixed-class packet stream in strict arrival order.

        One flow per traffic class is created, and packets are emitted
        round-robin across the classes so that all classes compete at the same
        time. Emitting packets in arrival order is what makes FIFO, Priority
        and WFQ orderings genuinely comparable.
        """
        class_list = list(classes or TRAFFIC_CLASSES)
        unknown = [name for name in class_list if name not in PRIORITIES]
        if unknown:
            raise ValueError(f"Unknown traffic types: {unknown}")
        if not class_list:
            return {}

        interval = 1.0 / max(pps, 0.1)
        width = len(class_list)

        flows: Dict[str, TrafficFlow] = {}
        for traffic_class in class_list:
            flows[traffic_class] = self._register_flow(
                source,
                destination,
                traffic_class,
                packet_count=packets_per_class,
                packet_size=packet_size,
                pps=pps,
                duration=packets_per_class / max(pps, 0.1),
            )

        # Round-robin arrival schedule: every class appears in every cycle, so
        # arrivals are interleaved and FIFO is a true arrival-order queue.
        for cycle in range(packets_per_class):
            for index, traffic_class in enumerate(class_list):
                flow = flows[traffic_class]
                creation_time = self.time + (cycle * width + index) * interval
                packet = self.generate_packet(
                    source,
                    destination,
                    traffic_class,
                    packet_size,
                    flow_id=flow.flow_id,
                    creation_time=creation_time,
                )
                flow.packet_ids.append(packet.packet_id)
                flow.packets_sent += 1

        self.metrics.active_flows_count = len(self.active_flows)

        if len(self.metrics.history) == 0:
            self.metrics.snapshot(
                self.time, self.average_congestion(), len(self.active_flows)
            )

        return flows

    def _generate_packets_for_flow(self, flow: TrafficFlow) -> List[Packet]:
        """Generate actual Packet objects for a flow."""
        packets = []
        # Determine effective count: if duration specified and pps, count may be derived
        # For this implementation, use packet_count directly, but space them by pps
        count = flow.packet_count
        interval = 1.0 / max(flow.pps, 0.1)
        for i in range(count):
            ctime = flow.start_time + i * interval
            pkt = self.generate_packet(
                flow.source,
                flow.destination,
                flow.traffic_type,
                flow.packet_size,
                flow_id=flow.flow_id,
                creation_time=ctime,
            )
            packets.append(pkt)
            flow.packet_ids.append(pkt.packet_id)

        flow.packets_sent = len(packets)
        return packets

    def get_flow(self, flow_id: str) -> Optional[TrafficFlow]:
        return self.active_flows.get(flow_id)

    def list_flows(self) -> List[TrafficFlow]:
        return list(self.active_flows.values())

    # ------------------------------------------------------------------
    # Transmission modeling
    # ------------------------------------------------------------------
    def _transmission_time(self, path: List[str], packet: Packet) -> float:
        total_latency = 0.0

        for u, v in zip(path, path[1:]):
            if not self.topology.graph.has_edge(u, v):
                continue
            edge = self.topology.graph[u][v]

            bandwidth = max(edge.get("bandwidth", 100.0), 0.001)
            base_latency = edge.get("latency", 10.0)
            congestion = edge.get("congestion", 0.0)

            # Bandwidth affects serialization: size in bytes -> bits
            serialization = packet.size / (bandwidth * 125000)

            # Congestion increases both latency and serialization
            congestion_factor = 1.0 + congestion * 2.0

            total_latency += (base_latency / 1000.0) * congestion_factor
            total_latency += serialization * congestion_factor

        return total_latency

    def _packet_loss_probability(self, path: List[str]) -> float:
        probability_no_loss = 1.0

        for u, v in zip(path, path[1:]):
            if not self.topology.graph.has_edge(u, v):
                continue
            edge = self.topology.graph[u][v]
            loss = max(0.0, min(1.0, edge.get("packet_loss", 0.0)))
            congestion = max(0.0, min(1.0, edge.get("congestion", 0.0)))
            # Congestion contributes to effective loss
            effective_loss = min(1.0, loss + congestion * 0.05)
            probability_no_loss *= 1.0 - effective_loss

        return 1.0 - probability_no_loss

    def _route_uses_link(self, route: List[str], u: str, v: str) -> bool:
        """Check if route uses link u-v in either direction."""
        for a, b in zip(route, route[1:]):
            if (a == u and b == v) or (a == v and b == u):
                return True
        return False

    def _route_uses_node(self, route: List[str], node: str) -> bool:
        return node in route

    # ------------------------------------------------------------------
    # Packet processing
    # ------------------------------------------------------------------
    def process_next_packet(self) -> Packet | None:
        if self.scheduler.empty():
            return None

        # Pop with the current simulation time so the scheduler can measure the
        # real queue waiting time of the packet.
        packet = self.scheduler.pop(now=self.time)

        # Find flow if associated
        flow = None
        # Search flow by packet_id (could be optimized)
        for f in self.active_flows.values():
            if packet.packet_id in f.packet_ids:
                flow = f
                break

        try:
            path, route_cost = self.router.shortest_path(
                self.topology,
                packet.source,
                packet.destination,
            )
        except ValueError:
            packet.delivery_status = "DROPPED"

            self.metrics.record(
                packet.packet_id,
                packet.creation_time,
                None,
                None,
                packet.size,
                "DROPPED",
                flow_id=flow.flow_id if flow else None,
                route=None,
                traffic_type=packet.traffic_type,
                queue_wait=packet.queue_wait_time,
            )

            if flow:
                flow.packets_dropped += 1
                if flow.packets_dropped + flow.packets_delivered >= flow.packets_sent:
                    flow.status = "FAILED"
                    flow.end_time = self.time

            self._log_event(
                EventType.PACKET_DROPPED,
                f"Packet {packet.packet_id} dropped: no route {packet.source}→{packet.destination}",
                flow_id=flow.flow_id if flow else None,
                packet_id=packet.packet_id,
                reason="NO_ROUTE",
            )

            self.time += 0.001
            # Heartbeat check opportunistically
            self.check_heartbeats()
            self.transport.on_packet_processed(packet)
            return packet

        # Detect route change for flow
        if flow and flow.current_route and flow.current_route != path:
            flow.previous_route = flow.current_route.copy()
            flow.current_route = path
            flow.route_cost = route_cost
            if flow.route_status != "FAILED":
                flow.route_status = "REROUTED"

            self._log_event(
                EventType.ROUTE_RECALCULATED,
                f"Route recalculated for flow {flow.flow_id}: {' → '.join(path)} cost={route_cost:.2f}",
                component=f"{packet.source}→{packet.destination}",
                flow_id=flow.flow_id,
                old_route=flow.previous_route,
                new_route=path,
                route_cost=route_cost,
            )
            self._log_event(
                EventType.TRAFFIC_REROUTED,
                f"Traffic rerouted for flow {flow.flow_id}",
                component=f"{packet.source}→{packet.destination}",
                flow_id=flow.flow_id,
                route=path,
            )
            # Update recovery record recalculation time
            # Find most recent recovery record for components in previous route that are now avoided
            for comp, det_time in list(self._detected_links.items()):
                if self._route_uses_link(flow.previous_route, comp[0], comp[1]) and not self._route_uses_link(path, comp[0], comp[1]):
                    comp_str = f"{comp[0]}-{comp[1]}"
                    self.metrics.update_recovery_recalculation(comp_str, self.time)
        elif flow and not flow.current_route:
            flow.current_route = path
            flow.route_cost = route_cost
            flow.route_status = "HEALTHY"

        packet.route = path
        # Stage 10 policy evaluation happens after the real route lookup and
        # before transmission/loss.  A denied packet still records a normal
        # simulator metric, while the actual service/security layer supplies
        # the authoritative drop reason to the packet inspector.
        security_decision = self.services.inspect_packet(packet, path)
        if security_decision is not None:
            if not security_decision.get("allowed", True):
                self.services.last_block_reason = security_decision.get("reason") or "FIREWALL_BLOCK"
        if security_decision is not None and not security_decision.get("allowed", True):
            packet.delivery_status = "DROPPED"
            self.metrics.record(
                packet.packet_id,
                packet.creation_time,
                None,
                None,
                packet.size,
                "DROPPED",
                flow_id=flow.flow_id if flow else None,
                route=path,
                traffic_type=packet.traffic_type,
                queue_wait=packet.queue_wait_time,
            )
            if flow:
                flow.packets_dropped += 1
            self._log_event(
                EventType.PACKET_BLOCKED,
                f"Packet {packet.packet_id} blocked by simulated security policy",
                flow_id=flow.flow_id if flow else None,
                packet_id=packet.packet_id,
                reason=security_decision.get("reason") or "FIREWALL_BLOCK",
                security=security_decision,
                route=path,
            )
            self._log_event(
                EventType.PACKET_DROPPED,
                f"Packet {packet.packet_id} dropped by simulated security policy",
                flow_id=flow.flow_id if flow else None,
                packet_id=packet.packet_id,
                reason=security_decision.get("reason") or "FIREWALL_BLOCK",
                route=path,
            )
            self.time = max(self.time, packet.creation_time) + 0.001
            self.check_heartbeats()
            self.transport.on_packet_processed(packet)
            return packet

        # Ensure simulation time respects packet creation time (no negative latency)
        if packet.creation_time > self.time:
            self.time = packet.creation_time

        transmission_delay = self._transmission_time(path, packet)

        loss_probability = self._packet_loss_probability(path)

        # Advance simulation clock by transmission time
        self.time += transmission_delay

        # Heartbeat check after time advance
        self.check_heartbeats()

        # For latency calculation, use transmission delay plus any queuing
        latency = transmission_delay

        loss_draw = (
            packet.loss_roll
            if packet.loss_roll is not None
            else self.rng.random()
        )

        if loss_draw < loss_probability:
            packet.delivery_status = "DROPPED"

            self.metrics.record(
                packet.packet_id,
                packet.creation_time,
                None,
                None,
                packet.size,
                "DROPPED",
                flow_id=flow.flow_id if flow else None,
                route=path,
                traffic_type=packet.traffic_type,
                queue_wait=packet.queue_wait_time,
            )

            if flow:
                flow.packets_dropped += 1

            self._log_event(
                EventType.PACKET_DROPPED,
                f"Packet {packet.packet_id} dropped (loss prob {loss_probability:.2%})",
                flow_id=flow.flow_id if flow else None,
                packet_id=packet.packet_id,
                reason="PACKET_LOSS",
                route=path,
                loss_probability=loss_probability,
            )
        else:
            packet.delivery_status = "DELIVERED"
            packet.delivery_time = self.time
            packet.latency = self.time - packet.creation_time

            self.metrics.record(
                packet.packet_id,
                packet.creation_time,
                packet.delivery_time,
                packet.latency,
                packet.size,
                "DELIVERED",
                flow_id=flow.flow_id if flow else None,
                route=path,
                traffic_type=packet.traffic_type,
                queue_wait=packet.queue_wait_time,
            )

            if flow:
                flow.packets_delivered += 1

            self._log_event(
                EventType.PACKET_DELIVERED,
                f"Packet {packet.packet_id} delivered via {' → '.join(path)} latency={packet.latency*1000:.1f}ms",
                flow_id=flow.flow_id if flow else None,
                packet_id=packet.packet_id,
                route=path,
                route_cost=route_cost,
                latency=packet.latency,
            )

        # Check if flow completed
        if flow:
            if flow.packets_delivered + flow.packets_dropped >= flow.packets_sent:
                if flow.status != "FAILED":
                    flow.status = "COMPLETED"
                flow.end_time = self.time
                self._log_event(
                    EventType.FLOW_COMPLETED if flow.status == "COMPLETED" else EventType.FLOW_FAILED,
                    f"Flow {flow.flow_id} {flow.status.lower()}: delivered {flow.packets_delivered}/{flow.packets_sent}",
                    flow_id=flow.flow_id,
                    packets_delivered=flow.packets_delivered,
                    packets_dropped=flow.packets_dropped,
                )

        self.transport.on_packet_processed(packet)
        return packet

    def run(self, steps: int | None = None) -> List[Packet]:
        processed = []

        limit = steps if steps is not None else len(self.scheduler)

        for _ in range(limit):
            packet = self.process_next_packet()

            if packet is None:
                break

            processed.append(packet)

        congestion = self.average_congestion()

        self.metrics.snapshot(
            current_time=self.time,
            congestion=congestion,
            active_flows=len(self.active_flows),
        )

        return processed

    def run_until_empty(self) -> List[Packet]:
        """Run until scheduler is empty."""
        all_processed = []
        while not self.scheduler.empty():
            pkt = self.process_next_packet()
            if pkt is None:
                break
            all_processed.append(pkt)
        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))
        return all_processed

    def tick(self, delta: float = 1.0) -> None:
        """Advance simulation time by delta and check heartbeats."""
        self.time += delta
        self.transport.process_transport_tick(self.time)
        self.services.tick(delta)
        self.check_heartbeats()
        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))

    # ------------------------------------------------------------------
    # Heartbeat / Failure Detection
    # ------------------------------------------------------------------
    def check_heartbeats(self) -> List[str]:
        """
        Simulated heartbeat checking.

        Detects failures after failure_detection_timeout.
        Returns list of newly detected components.
        """
        if self.time - self.last_heartbeat_check < self.heartbeat_interval:
            # Still check for timeouts even if interval not elapsed? Requirement says periodic.
            # We'll allow detection check every time, but update last_heartbeat_check periodically.
            pass

        newly_detected = []

        # Check links
        for (u, v), failure_time in list(self._failed_links.items()):
            key = (u, v)
            if key in self._detected_links:
                continue
            elapsed = self.time - failure_time
            if elapsed >= self.failure_detection_timeout:
                self._detected_links[key] = self.time
                comp_str = f"{u}-{v}"
                newly_detected.append(comp_str)

                self.metrics.update_recovery_detection(comp_str, self.time)

                self._log_event(
                    EventType.FAILURE_DETECTED,
                    f"Failure detected on link {u}↔{v} after {elapsed:.2f}s",
                    component=comp_str,
                    failure_time=failure_time,
                    detection_time=self.time,
                    detection_delay=elapsed,
                )

                # Trigger rerouting
                self._recalculate_routes_for_failed_component(comp_str, "LINK", [(u, v)], [])

        # Check nodes
        for node, failure_time in list(self._failed_nodes.items()):
            if node in self._detected_nodes:
                continue
            elapsed = self.time - failure_time
            if elapsed >= self.failure_detection_timeout:
                self._detected_nodes[node] = self.time
                newly_detected.append(node)

                self.metrics.update_recovery_detection(node, self.time)

                self._log_event(
                    EventType.FAILURE_DETECTED,
                    f"Failure detected on node {node} after {elapsed:.2f}s",
                    component=node,
                    failure_time=failure_time,
                    detection_time=self.time,
                    detection_delay=elapsed,
                )

                self._recalculate_routes_for_failed_component(node, "NODE", [], [node])

        self.last_heartbeat_check = self.time
        return newly_detected

    def _recalculate_routes_for_failed_component(
        self,
        component: str,
        component_type: str,
        failed_links: List[Tuple[str, str]],
        failed_nodes: List[str],
    ) -> None:
        """Recalculate routes for flows affected by failure."""
        for flow in self.active_flows.values():
            if flow.status == "FAILED":
                continue

            affected = False
            for u, v in failed_links:
                if self._route_uses_link(flow.current_route, u, v):
                    affected = True
                    break
            for node in failed_nodes:
                if self._route_uses_node(flow.current_route, node):
                    affected = True
                    break

            if not affected:
                continue

            # Try to find alternative route
            try:
                new_route, new_cost = self.router.shortest_path(
                    self.topology, flow.source, flow.destination
                )
                if new_route != flow.current_route:
                    flow.previous_route = flow.current_route.copy()
                    flow.current_route = new_route
                    flow.route_cost = new_cost
                    flow.route_status = "REROUTED"

                    self._log_event(
                        EventType.ROUTE_RECALCULATED,
                        f"Route recalculated for flow {flow.flow_id}: {' → '.join(new_route)}",
                        component=component,
                        flow_id=flow.flow_id,
                        old_route=flow.previous_route,
                        new_route=new_route,
                        route_cost=new_cost,
                    )
                    self._log_event(
                        EventType.TRAFFIC_REROUTED,
                        f"Traffic rerouted for flow {flow.flow_id} via {' → '.join(new_route)}",
                        component=component,
                        flow_id=flow.flow_id,
                        route=new_route,
                    )
                    self.metrics.update_recovery_recalculation(component, self.time)

            except ValueError:
                # No alternative route
                flow.previous_route = flow.current_route.copy()
                flow.current_route = []
                flow.route_status = "FAILED"
                flow.status = "FAILED"
                self._log_event(
                    EventType.FLOW_FAILED,
                    f"Flow {flow.flow_id} failed: no alternative route after {component} failure",
                    component=component,
                    flow_id=flow.flow_id,
                )

    # ------------------------------------------------------------------
    # Failure injection / recovery
    # ------------------------------------------------------------------
    def fail_link(self, u: str, v: str) -> None:
        # Normalize key for tracking
        key = tuple(sorted((u, v)))
        # Check if already failed
        if key in self._failed_links:
            return

        if not self.topology.graph.has_edge(u, v):
            # Try to find edge in either order
            if not self.topology.graph.has_edge(v, u):
                raise ValueError(f"Link {u}-{v} does not exist.")

        self.topology.fail_link(u, v)
        failure_time = self.time
        self._failed_links[key] = failure_time

        # Record recovery tracking
        comp_str = f"{u}-{v}"
        rec = RecoveryRecord(
            failure_time=failure_time,
            component=comp_str,
            component_type="LINK",
        )
        self.metrics.add_recovery_record(rec)

        self._log_event(
            EventType.LINK_FAILED,
            f"Link {u}↔{v} failed at {failure_time:.2f}s",
            component=comp_str,
            failure_time=failure_time,
        )

        # Save baseline snapshot for comparison
        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))
        self.baseline_metrics = self.metrics.get_latest()

        # Immediate heartbeat check if timeout is 0
        if self.failure_detection_timeout <= 0:
            self.check_heartbeats()
        else:
            # For UI responsiveness, we still want to detect quickly if timeout is small
            # But we respect simulation clock: detection will happen after timeout
            # However, we can also attempt to pre-detect for rerouting demonstration
            # by checking if time already advanced past timeout (which it hasn't)
            pass

    def recover_link(self, u: str, v: str) -> None:
        key = tuple(sorted((u, v)))
        if not self.topology.graph.has_edge(u, v):
            if not self.topology.graph.has_edge(v, u):
                raise ValueError(f"Link {u}-{v} does not exist.")

        self.topology.recover_link(u, v)

        comp_str = f"{u}-{v}"
        recovery_time = self.time

        if key in self._failed_links:
            del self._failed_links[key]
        if key in self._detected_links:
            del self._detected_links[key]

        self.metrics.update_recovery_recovered(comp_str, recovery_time)

        self._log_event(
            EventType.LINK_RECOVERED,
            f"Link {u}↔{v} recovered at {recovery_time:.2f}s",
            component=comp_str,
            recovery_time=recovery_time,
        )

        # Recalculate routes - recovery may provide better paths
        for flow in self.active_flows.values():
            if flow.status == "FAILED":
                # Try to recover failed flows
                try:
                    new_route, new_cost = self.router.shortest_path(
                        self.topology, flow.source, flow.destination
                    )
                    flow.current_route = new_route
                    flow.route_cost = new_cost
                    flow.route_status = "HEALTHY"
                    flow.status = "ACTIVE"
                    self._log_event(
                        EventType.ROUTE_RECALCULATED,
                        f"Route restored for flow {flow.flow_id} after {comp_str} recovery",
                        component=comp_str,
                        flow_id=flow.flow_id,
                        new_route=new_route,
                        route_cost=new_cost,
                    )
                except ValueError:
                    pass
            else:
                # Check if new route is better
                try:
                    new_route, new_cost = self.router.shortest_path(
                        self.topology, flow.source, flow.destination
                    )
                    # If cost is significantly lower, switch back
                    if new_cost < flow.route_cost * 0.9:
                        flow.previous_route = flow.current_route.copy()
                        flow.current_route = new_route
                        flow.route_cost = new_cost
                        flow.route_status = "HEALTHY"
                        self._log_event(
                            EventType.ROUTE_RECALCULATED,
                            f"Route optimized for flow {flow.flow_id} after recovery",
                            component=comp_str,
                            flow_id=flow.flow_id,
                            new_route=new_route,
                            route_cost=new_cost,
                        )
                except ValueError:
                    pass

        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))
        self.after_recovery_metrics = self.metrics.get_latest()

    def fail_node(self, node: str) -> None:
        if node in self._failed_nodes:
            return
        if node not in self.topology.graph:
            raise ValueError(f"Node {node} does not exist.")

        self.topology.fail_node(node)
        failure_time = self.time
        self._failed_nodes[node] = failure_time

        rec = RecoveryRecord(
            failure_time=failure_time,
            component=node,
            component_type="NODE",
        )
        self.metrics.add_recovery_record(rec)

        self._log_event(
            EventType.NODE_FAILED,
            f"Node {node} failed at {failure_time:.2f}s",
            component=node,
            failure_time=failure_time,
        )

        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))
        self.baseline_metrics = self.metrics.get_latest()

        if self.failure_detection_timeout <= 0:
            self.check_heartbeats()

    def fail_interface(self, node: str, interface_id: str) -> list[tuple[str, str]]:
        """Fail an interface and its associated links through normal simulation paths."""

        links = self.topology.fail_interface(node, interface_id)
        for u, v in links:
            self.fail_link(u, v)
        return links

    def recover_interface(self, node: str, interface_id: str) -> list[tuple[str, str]]:
        """Recover an interface and associated links through normal simulation paths."""

        links = self.topology.recover_interface(node, interface_id)
        for u, v in links:
            self.recover_link(u, v)
        return links

    def recover_node(self, node: str) -> None:
        if node not in self.topology.graph:
            raise ValueError(f"Node {node} does not exist.")

        self.topology.recover_node(node)
        recovery_time = self.time

        if node in self._failed_nodes:
            del self._failed_nodes[node]
        if node in self._detected_nodes:
            del self._detected_nodes[node]

        self.metrics.update_recovery_recovered(node, recovery_time)

        self._log_event(
            EventType.NODE_RECOVERED,
            f"Node {node} recovered at {recovery_time:.2f}s",
            component=node,
            recovery_time=recovery_time,
        )

        # Similar recovery logic as link
        for flow in self.active_flows.values():
            if flow.status == "FAILED":
                try:
                    new_route, new_cost = self.router.shortest_path(
                        self.topology, flow.source, flow.destination
                    )
                    flow.current_route = new_route
                    flow.route_cost = new_cost
                    flow.route_status = "HEALTHY"
                    flow.status = "ACTIVE"
                    self._log_event(
                        EventType.ROUTE_RECALCULATED,
                        f"Route restored for flow {flow.flow_id} after {node} recovery",
                        component=node,
                        flow_id=flow.flow_id,
                        new_route=new_route,
                    )
                except ValueError:
                    pass

        self.metrics.snapshot(self.time, self.average_congestion(), len(self.active_flows))
        self.after_recovery_metrics = self.metrics.get_latest()

    # ------------------------------------------------------------------
    # Condition updates
    # ------------------------------------------------------------------
    def set_link_conditions(
        self,
        u: str,
        v: str,
        *,
        latency: float | None = None,
        bandwidth: float | None = None,
        packet_loss: float | None = None,
        congestion: float | None = None,
    ) -> None:
        values = {
            k: v
            for k, v in {
                "latency": latency,
                "bandwidth": bandwidth,
                "packet_loss": packet_loss,
                "congestion": congestion,
            }.items()
            if v is not None
        }

        self.topology.update_link(u, v, **values)

        comp_str = f"{u}-{v}"

        if latency is not None:
            self._log_event(
                EventType.LINK_UPDATED,
                f"Latency updated on {comp_str} to {latency}ms",
                component=comp_str,
                latency=latency,
            )
        if bandwidth is not None:
            self._log_event(
                EventType.BANDWIDTH_CHANGED,
                f"Bandwidth changed on {comp_str} to {bandwidth} Mbps",
                component=comp_str,
                bandwidth=bandwidth,
            )
        if packet_loss is not None:
            self._log_event(
                EventType.PACKET_LOSS_CHANGED,
                f"Packet loss changed on {comp_str} to {packet_loss:.2%}",
                component=comp_str,
                packet_loss=packet_loss,
            )
        if congestion is not None:
            self._log_event(
                EventType.CONGESTION_CHANGED,
                f"Congestion changed on {comp_str} to {congestion:.2%}",
                component=comp_str,
                congestion=congestion,
            )

        # Check if congestion/loss/bandwidth change should trigger rerouting
        if congestion is not None or packet_loss is not None or bandwidth is not None or latency is not None:
            for flow in self.active_flows.values():
                if flow.status == "FAILED":
                    continue
                if self._route_uses_link(flow.current_route, u, v):
                    try:
                        new_route, new_cost = self.router.shortest_path(
                            self.topology, flow.source, flow.destination
                        )
                        # Calculate current route cost after condition change
                        current_cost = self.router.route_cost(self.topology, flow.current_route)
                        # Reroute if new route is different and cheaper than current route's new cost
                        if new_route != flow.current_route and new_cost < current_cost:
                            flow.previous_route = flow.current_route.copy()
                            flow.current_route = new_route
                            flow.route_cost = new_cost
                            flow.route_status = "REROUTED"
                            self._log_event(
                                EventType.ROUTE_RECALCULATED,
                                f"Route changed due to condition change for flow {flow.flow_id}: {' → '.join(new_route)} cost={new_cost:.2f} (was {current_cost:.2f})",
                                component=comp_str,
                                flow_id=flow.flow_id,
                                old_route=flow.previous_route,
                                new_route=new_route,
                                route_cost=new_cost,
                                old_cost=current_cost,
                            )
                            self._log_event(
                                EventType.TRAFFIC_REROUTED,
                                f"Traffic rerouted for flow {flow.flow_id} due to condition change on {comp_str}",
                                component=comp_str,
                                flow_id=flow.flow_id,
                                route=new_route,
                            )
                    except ValueError:
                        pass

    def set_congestion(self, u: str, v: str, value: float) -> None:
        self.set_link_conditions(u, v, congestion=value)

    def set_packet_loss(self, u: str, v: str, value: float) -> None:
        self.set_link_conditions(u, v, packet_loss=value)

    def set_bandwidth(self, u: str, v: str, value: float) -> None:
        self.set_link_conditions(u, v, bandwidth=value)

    # ------------------------------------------------------------------
    # Stage 4/5 configuration: QoS scheduler, priorities, weights, routing
    # ------------------------------------------------------------------
    def set_scheduler(self, name: str, **params: Any) -> str:
        """Switch the packet scheduler and apply it to the simulation engine."""
        self.scheduler_name = normalize_scheduler_name(name)
        if params:
            self.scheduler_params.update(params)
        self.scheduler = create_scheduler(
            self.scheduler_name, **self.scheduler_params
        )
        self._log_event(
            EventType.SCHEDULER_CHANGED,
            f"QoS scheduler set to {self.scheduler_name}",
            scheduler=self.scheduler_name,
        )
        return self.scheduler_name

    def set_wfq_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Configure the WFQ service weights (applies to the live WFQ queue)."""
        resolved = normalize_class_weights(
            {**self.scheduler_params.get("weights", {}), **weights}
        )
        self.scheduler_params["weights"] = resolved
        if hasattr(self.scheduler, "set_weights"):
            self.scheduler.set_weights(resolved)
        self._log_event(
            EventType.QOS_CONFIG_CHANGED,
            "WFQ weights updated",
            weights=dict(resolved),
        )
        return resolved

    def get_wfq_weights(self) -> Dict[str, float]:
        if hasattr(self.scheduler, "get_weights"):
            return self.scheduler.get_weights()
        return normalize_class_weights(self.scheduler_params.get("weights"))

    def set_priority_config(self, priorities: Dict[str, float]) -> Dict[str, float]:
        """Configure traffic-class scheduling priorities."""
        resolved = configure_priorities(priorities)
        self.scheduler_params["priorities"] = resolved
        if hasattr(self.scheduler, "set_priorities"):
            self.scheduler.set_priorities(resolved)
        self._log_event(
            EventType.QOS_CONFIG_CHANGED,
            "Traffic-class priorities updated",
            priorities=dict(resolved),
        )
        return resolved

    def reset_traffic_priorities(self) -> Dict[str, float]:
        return reset_priorities()

    def get_priority_config(self) -> Dict[str, float]:
        return dict(PRIORITIES)

    def set_router_algorithm(self, algorithm: str) -> str:
        """Select Dijkstra or Bellman-Ford for all future route computation."""
        selected = self.router.set_algorithm(algorithm)
        self._log_event(
            EventType.ROUTING_ALGORITHM_CHANGED,
            f"Routing algorithm set to {ALGORITHM_LABELS.get(selected, selected)}",
            algorithm=selected,
        )
        return selected

    def get_router_algorithm(self) -> str:
        return self.router.get_algorithm()

    def set_routing_weights(self, **weights: float) -> Dict[str, float]:
        """Update the route-cost weights used by the live router."""
        unknown = set(weights) - set(ROUTING_WEIGHT_NAMES)
        if unknown:
            raise ValueError(f"Unknown routing weights: {sorted(unknown)}")

        resolved = self.router.configure_weights(dict(weights))
        self._log_event(
            EventType.ROUTING_WEIGHTS_CHANGED,
            "Routing weights updated",
            weights=dict(resolved),
        )

        # Re-evaluate active flows so weight changes take effect immediately.
        for flow in self.active_flows.values():
            if flow.status == "FAILED":
                continue
            try:
                new_route, new_cost = self.router.shortest_path(
                    self.topology, flow.source, flow.destination
                )
            except ValueError:
                continue
            if new_route != flow.current_route:
                flow.previous_route = flow.current_route.copy()
                flow.current_route = new_route
                flow.route_cost = new_cost
                flow.route_status = "REROUTED"
                self._log_event(
                    EventType.ROUTE_RECALCULATED,
                    f"Route recalculated after weight change for flow {flow.flow_id}: "
                    f"{' → '.join(new_route)} cost={new_cost:.2f}",
                    flow_id=flow.flow_id,
                    old_route=flow.previous_route,
                    new_route=new_route,
                    route_cost=new_cost,
                )

        return resolved

    def get_routing_weights(self) -> Dict[str, float]:
        return self.router.get_weights()

    # ------------------------------------------------------------------
    # QoS statistics (Stage 4)
    # ------------------------------------------------------------------
    def scheduler_statistics(self) -> Dict[str, float]:
        """Queue statistics of the active scheduler."""
        return self.scheduler.queue_statistics()

    def scheduler_class_statistics(self) -> Dict[str, Dict[str, float]]:
        """Per-class enqueue / serve counters and occupancy of the scheduler."""
        return self.scheduler.class_statistics()

    def class_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Per traffic-class QoS metrics from real packet records."""
        return self.metrics.class_metrics(self.time, list(TRAFFIC_CLASSES))

    def class_dataframe(self) -> pd.DataFrame:
        return self.metrics.class_dataframe(self.time, list(TRAFFIC_CLASSES))

    def average_congestion(self) -> float:
        edges = list(self.topology.graph.edges(data=True))

        if not edges:
            return 0.0

        return sum(
            data.get("congestion", 0.0)
            for _, _, data in edges
        ) / len(edges)

    def metrics_snapshot(self) -> Dict[str, Any]:
        return self.metrics.snapshot(
            self.time,
            self.average_congestion(),
            len(self.active_flows),
        )

    def get_current_route(self, source: str, destination: str) -> Tuple[List[str], float, str]:
        """Get current route with status for UI display."""
        try:
            route, cost = self.router.shortest_path(self.topology, source, destination)
            # Determine status based on whether route uses previously failed components
            status = "HEALTHY"
            # Check if any flow using this route is rerouted
            for flow in self.active_flows.values():
                if flow.source == source and flow.destination == destination:
                    status = flow.route_status
                    break
            return route, cost, status
        except ValueError as e:
            return [], float("inf"), f"FAILED: {e}"

    def get_event_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.event_logger.get_legacy_events(limit)

    def get_structured_events(self, limit: int = 50):
        return self.event_logger.get_events(limit)

    # ------------------------------------------------------------------
    # Stage 7 protocol diagnostics
    # ------------------------------------------------------------------
    def ping(self, source: str, destination: str, ttl: int = 64):
        return self.protocols.ping(source, destination, ttl=ttl)

    def arp_lookup(self, source: str, ip_address: str, packet_id: str | None = None):
        return self.protocols.arp_lookup(source, ip_address, packet_id=packet_id)

    # ------------------------------------------------------------------
    # Stage 8 network diagnostics and packet inspection
    # ------------------------------------------------------------------
    def diagnostic_ping(self, source: str, destination: str, count: int = 4, ttl: int = 64):
        return self.protocols.diagnostic_ping(source, destination, count=count, ttl=ttl)

    def traceroute(self, source: str, destination: str, ttl: int = 64):
        return self.protocols.traceroute(source, destination, ttl=ttl)

    def arp_table(self, device_name: str):
        return self.protocols.arp_table(device_name)

    def clear_arp(self, device_name: str) -> int:
        return self.protocols.clear_arp(device_name)

    def mac_table(self, switch_name: str):
        return self.protocols.mac_table(switch_name)

    def clear_mac_table(self, switch_name: str) -> int:
        return self.protocols.clear_mac_table(switch_name)

    def diagnostic_packet_details(self, packet_id: str):
        packet = self.protocols.protocol_packets.get(str(packet_id))
        return self.protocols.packet_details(packet) if packet is not None else None

    def create_tcp_connection(self, *args, **kwargs):
        return self.transport.create_tcp_connection(*args, **kwargs)

    def create_udp_flow(self, *args, **kwargs):
        return self.transport.create_udp_flow(*args, **kwargs)

    def send_tcp_data(self, *args, **kwargs):
        return self.transport.send_tcp_data(*args, **kwargs)

    def send_tcp_response(self, *args, **kwargs):
        return self.transport.send_tcp_response(*args, **kwargs)

    def send_udp_data(self, *args, **kwargs):
        return self.transport.send_udp_data(*args, **kwargs)

    def close_tcp_connection(self, *args, **kwargs):
        return self.transport.close_tcp_connection(*args, **kwargs)

    def process_transport_tick(self, *args, **kwargs):
        return self.transport.process_transport_tick(*args, **kwargs)

    def get_connection(self, *args, **kwargs):
        return self.transport.get_connection(*args, **kwargs)

    def get_transport_flows(self):
        return self.transport.get_transport_flows()

    def get_transport_statistics(self):
        return self.transport.get_transport_statistics()

    def get_tcp_state(self, *args, **kwargs):
        return self.transport.get_tcp_state(*args, **kwargs)

    def get_tcp_packet_info(self, *args, **kwargs):
        return self.transport.get_tcp_packet_info(*args, **kwargs)

    # ------------------------------------------------------------------
    # Stage 10 simulated network services and security
    # ------------------------------------------------------------------
    def install_service(self, name: str, device: str, **config):
        return self.services.registry.install(name, device, **config)

    def remove_service(self, name: str, device: str):
        return self.services.registry.remove(name, device)

    def start_service(self, name: str, device: str):
        return self.services.registry.start(name, device)

    def stop_service(self, name: str, device: str):
        return self.services.registry.stop(name, device)

    def restart_service(self, name: str, device: str):
        return self.services.registry.restart(name, device)

    def configure_service(self, name: str, device: str, values=None):
        return self.services.registry.configure(name, device, values or {})

    def list_services(self, device: str = None):
        return self.services.registry.to_dict(device)

    def service_state(self) -> Dict[str, Any]:
        return self.services.service_state()

    def security_state(self) -> Dict[str, Any]:
        return self.services.security_state()

    def service_status(self, device: str = None):
        return self.services.service_status(device) if device else self.services.service_status_all()

    def dhcp_acquire(self, *args, **kwargs):
        return self.services.dhcp_acquire(*args, **kwargs)

    def dhcp_renew(self, *args, **kwargs):
        return self.services.dhcp_renew(*args, **kwargs)

    def dhcp_release(self, *args, **kwargs):
        return self.services.dhcp_release(*args, **kwargs)

    def dns_query(self, *args, **kwargs):
        return self.services.dns_query(*args, **kwargs)

    def dns_add_record(self, *args, **kwargs):
        return self.services.dns_add_record(*args, **kwargs)

    def http_request(self, *args, **kwargs):
        result = self.services.http_request(*args, **kwargs)
        if not result.get("success") and result.get("reason") == "CONNECTION_FAILED":
            result["reason"] = self.services.last_block_reason or result["reason"]
        return result

    def ftp_connect(self, *args, **kwargs):
        return self.services.ftp_connect(*args, **kwargs)

    def ftp_command(self, *args, **kwargs):
        return self.services.ftp_command(*args, **kwargs)

    def smtp_send(self, *args, **kwargs):
        return self.services.smtp_send(*args, **kwargs)

    def add_firewall_rule(self, **kwargs):
        return self.services.add_firewall_rule(**kwargs)

    def add_port_filter(self, *args, **kwargs):
        return self.services.add_port_filter(*args, **kwargs)

    def create_acl(self, *args, **kwargs):
        return self.services.create_acl(*args, **kwargs)

    def add_acl_entry(self, *args, **kwargs):
        return self.services.add_acl_entry(*args, **kwargs)

    def attach_acl(self, *args, **kwargs):
        return self.services.attach_acl(*args, **kwargs)

    def arp_spoof(self, *args, **kwargs):
        return self.services.arp_spoof(*args, **kwargs)

    def start_flood(self, *args, **kwargs):
        return self.services.start_flood(*args, **kwargs)

    def service_state(self):
        return self.services.service_state()

    def security_state(self):
        return self.services.security_state()

    def routing_table(self, router_name: str):
        return self.protocols.routing_table(router_name)

    def routing_table_lookup(self, router_name: str, destination_ip: str):
        return self.protocols.routing_table_lookup(router_name, destination_ip)

    def mac_table_lookup(self, switch_name: str, mac_address: str):
        return self.protocols.mac_table_lookup(switch_name, mac_address)

    def switch_frame(self, switch_name: str, frame: SwitchFrame, available_interfaces=None):
        return self.protocols.switch_frame(switch_name, frame, available_interfaces)

    def route_inspection(self, source: str, destination: str):
        return self.protocols.route_inspection(source, destination)

    def forward_packet(self, packet: ProtocolPacket, route: list[str] | None = None, ttl: int | None = None):
        return self.protocols.forward_packet(packet, route=route, ttl=ttl)

    def get_device(self, node: str):
        return self.topology.get_device(node)

    def reset(self) -> None:
        self.topology.reset()
        self.metrics.reset()

        self.time = 0.0
        self.next_packet_id = 1
        self.packets.clear()
        self.events.clear()
        self.event_logger.clear()
        self.protocols.arp.clear()
        self.transport.reset()
        self.services.reset()
        self.protocols.routing_tables.clear()
        self.protocols.switch_tables.clear()
        self.protocols.protocol_packets.clear()
        self.protocols._protocol_packet_counter = 1

        self._failed_links.clear()
        self._failed_nodes.clear()
        self._detected_links.clear()
        self._detected_nodes.clear()

        self.active_flows.clear()
        self.flow_counter = 1
        self.flow_history.clear()

        # Re-create the scheduler with the configured QoS parameters.
        self.scheduler = create_scheduler(self.scheduler_name, **self.scheduler_params)

        self.last_heartbeat_check = 0.0
        self.baseline_metrics = None
        self.during_failure_metrics = None
        self.after_recovery_metrics = None

        self._log_event(
            EventType.SIMULATION_RESET,
            "Simulation reset",
        )

    # ------------------------------------------------------------------
    # Backward compatibility helpers
    # ------------------------------------------------------------------
    def get_failed_links(self) -> List[Tuple[str, str]]:
        return list(self._failed_links.keys())

    def get_failed_nodes(self) -> List[str]:
        return list(self._failed_nodes.keys())

    def is_link_failed(self, u: str, v: str) -> bool:
        key = tuple(sorted((u, v)))
        return key in self._failed_links

    def is_node_failed(self, node: str) -> bool:
        return node in self._failed_nodes
