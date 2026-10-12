"""
QoS laboratory for NetAdapt.

Provides three schedulers that all operate on real ``Packet`` objects:

* ``FIFOQueue``     -- first in, first out (no class awareness)
* ``PriorityQueue`` -- strict priority ordering across traffic classes
* ``WFQ``           -- weighted fair queueing with per-class service credits

Every scheduler is instrumented with the same queue statistics
(enqueued, served, current queue length, maximum queue length, waiting time)
so that FIFO, Priority and WFQ can be compared on the *same* workload under the
*same* network conditions.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Traffic classes and configuration
# --------------------------------------------------------------------------

TRAFFIC_CLASSES: List[str] = ["Emergency", "VoIP", "Video", "HTTP", "FTP"]

#: Default scheduling priority of each traffic class (higher == served first).
DEFAULT_PRIORITIES: Dict[str, float] = {
    "Emergency": 5.0,
    "VoIP": 4.0,
    "Video": 3.0,
    "HTTP": 2.0,
    "FTP": 1.0,
}

#: Live, user-configurable priorities (mutated through ``configure_priorities``).
PRIORITIES: Dict[str, float] = dict(DEFAULT_PRIORITIES)

#: Default WFQ service weights (higher == larger share of the output link).
DEFAULT_WFQ_WEIGHTS: Dict[str, float] = {
    "Emergency": 5.0,
    "VoIP": 4.0,
    "Video": 3.0,
    "HTTP": 2.0,
    "FTP": 1.0,
}

#: Canonical scheduler keys used across the simulator / dashboard / experiments.
SCHEDULER_KEYS: Tuple[str, ...] = ("fifo", "priority", "wfq")

SCHEDULER_LABELS: Dict[str, str] = {
    "fifo": "FIFO",
    "priority": "Priority Queue",
    "wfq": "WFQ",
}


def normalize_scheduler_name(name: str) -> str:
    """Return a canonical scheduler key (``fifo`` / ``priority`` / ``wfq``)."""
    key = str(name).lower().replace("-", " ").replace("_", " ").strip()
    key = " ".join(key.split())
    if key in {"fifo", "fifo queue", "first in first out"}:
        return "fifo"
    if key in {"priority", "priority queue", "pq", "prio"}:
        return "priority"
    if key in {"wfq", "weighted fair queueing", "weighted fair queuing"}:
        return "wfq"
    raise ValueError(f"Unknown scheduler: {name}")


def configure_priorities(priorities: Dict[str, float]) -> Dict[str, float]:
    """
    Update the scheduling priority of traffic classes.

    New packets pick up the configured priority, so this genuinely changes how
    ``PriorityQueue`` orders packets. Returns the updated mapping.
    """
    unknown = set(priorities) - set(PRIORITIES)
    if unknown:
        raise ValueError(f"Unknown traffic classes: {sorted(unknown)}")

    for name, value in priorities.items():
        PRIORITIES[name] = float(value)

    return dict(PRIORITIES)


def reset_priorities() -> Dict[str, float]:
    """Restore the default traffic-class priorities."""
    PRIORITIES.clear()
    PRIORITIES.update(DEFAULT_PRIORITIES)
    return dict(PRIORITIES)


def normalize_class_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Validate / complete a traffic-class -> weight mapping."""
    resolved = dict(DEFAULT_WFQ_WEIGHTS)

    if weights:
        unknown = set(weights) - set(resolved)
        if unknown:
            raise ValueError(f"Unknown traffic classes: {sorted(unknown)}")
        for name, value in weights.items():
            if float(value) <= 0:
                raise ValueError(f"Weight for {name} must be positive.")
            resolved[name] = float(value)

    return resolved


# --------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------


@dataclass
class Packet:
    packet_id: int
    source: str
    destination: str
    traffic_type: str = "HTTP"
    size: int = 1000
    creation_time: float = 0.0
    priority: float = field(init=False)
    route: list[str] = field(default_factory=list)
    delivery_status: str = "PENDING"
    delivery_time: float | None = None
    latency: float | None = None
    #: Simulation time at which the packet entered the scheduler.
    enqueue_time: float | None = None
    #: Time the packet spent waiting in the scheduler before being served.
    queue_wait_time: float | None = None
    #: Pre-drawn uniform random number used for the packet-loss decision.
    #: Drawing it at creation time keeps the loss realisation identical across
    #: schedulers and routing algorithms, which makes comparison fair.
    loss_roll: float | None = None
    #: IPv4 TTL used by the Stage 7 forwarding/ICMP model.
    ttl: int = 64

    def __post_init__(self) -> None:
        if self.traffic_type not in PRIORITIES:
            raise ValueError(f"Unknown traffic type: {self.traffic_type}")
        self.priority = float(PRIORITIES[self.traffic_type])
        if int(self.ttl) < 1:
            raise ValueError("Packet TTL must be at least 1")
        self.ttl = int(self.ttl)


# --------------------------------------------------------------------------
# Queue statistics
# --------------------------------------------------------------------------


@dataclass
class QueueStats:
    """Queue statistics collected by every scheduler."""

    packets_enqueued: int = 0
    packets_served: int = 0
    current_length: int = 0
    max_length: int = 0
    total_waiting_time: float = 0.0
    max_waiting_time: float = 0.0

    @property
    def average_waiting_time(self) -> float:
        if self.packets_served <= 0:
            return 0.0
        return self.total_waiting_time / self.packets_served

    def as_dict(self) -> Dict[str, float]:
        return {
            "packets_enqueued": float(self.packets_enqueued),
            "packets_served": float(self.packets_served),
            "current_queue_length": float(self.current_length),
            "max_queue_length": float(self.max_length),
            "average_waiting_time": self.average_waiting_time,
            "max_waiting_time": self.max_waiting_time,
            "total_waiting_time": self.total_waiting_time,
        }


# --------------------------------------------------------------------------
# Schedulers
# --------------------------------------------------------------------------


class BaseQueue:
    """
    Common instrumentation shared by all schedulers.

    ``push``/``pop`` accept an optional simulation timestamp so real queue
    waiting times can be measured. When no timestamp is supplied the queue uses
    the arrival time recorded at enqueue, which keeps the schedulers usable
    stand-alone.
    """

    name: str = "BASE"

    def __init__(self) -> None:
        self.stats = QueueStats()
        self._now: float = 0.0
        self._enqueue_times: Dict[int, float] = {}
        self.class_enqueued: Dict[str, int] = {c: 0 for c in TRAFFIC_CLASSES}
        self.class_served: Dict[str, int] = {c: 0 for c in TRAFFIC_CLASSES}
        self._served_order: List[str] = []
        self._served_order_ids: List[int] = []

    # -- instrumentation ---------------------------------------------------
    def _note_enqueue(self, packet: Packet, now: Optional[float]) -> None:
        timestamp = self._now if now is None else float(now)
        if packet.enqueue_time is None:
            packet.enqueue_time = timestamp
        self._enqueue_times[packet.packet_id] = packet.enqueue_time
        self._now = max(self._now, timestamp)
        self.stats.packets_enqueued += 1
        self.class_enqueued[packet.traffic_type] = (
            self.class_enqueued.get(packet.traffic_type, 0) + 1
        )

    def _note_dequeue(self, packet: Packet, now: Optional[float]) -> None:
        timestamp = self._now if now is None else float(now)
        arrival = self._enqueue_times.pop(packet.packet_id, packet.enqueue_time)
        if arrival is None:
            arrival = timestamp
        # A packet cannot be served before it arrives.
        service_start = max(timestamp, arrival)
        waiting = max(0.0, service_start - arrival)
        self._now = max(self._now, service_start)
        packet.queue_wait_time = waiting
        self.stats.packets_served += 1
        self.stats.total_waiting_time += waiting
        self.stats.max_waiting_time = max(self.stats.max_waiting_time, waiting)
        self.class_served[packet.traffic_type] = (
            self.class_served.get(packet.traffic_type, 0) + 1
        )

    def _sync_length(self) -> None:
        length = len(self)
        self.stats.current_length = length
        self.stats.max_length = max(self.stats.max_length, length)

    def _track_served(self, packet: Packet) -> None:
        self._served_order.append(packet.traffic_type)
        self._served_order_ids.append(packet.packet_id)

    # -- queue statistics API ---------------------------------------------
    def queue_statistics(self) -> Dict[str, float]:
        """Current / maximum queue length, packets served, waiting times."""
        self._sync_length()
        stats = self.stats.as_dict()
        stats["scheduler"] = self.name
        return stats

    def class_lengths(self) -> Dict[str, int]:
        """Current queue length per traffic class."""
        lengths = {c: 0 for c in TRAFFIC_CLASSES}
        for packet in self._iter_packets():
            lengths[packet.traffic_type] = lengths.get(packet.traffic_type, 0) + 1
        return lengths

    def class_statistics(self) -> Dict[str, Dict[str, float]]:
        """Per traffic-class enqueue/serve counters and queue occupancy."""
        lengths = self.class_lengths()
        result: Dict[str, Dict[str, float]] = {}
        for traffic_class in TRAFFIC_CLASSES:
            result[traffic_class] = {
                "enqueued": float(self.class_enqueued.get(traffic_class, 0)),
                "served": float(self.class_served.get(traffic_class, 0)),
                "current_length": float(lengths.get(traffic_class, 0)),
            }
        return result

    def class_served_order(self) -> List[str]:
        """Traffic class of each packet in the order it was served."""
        return list(self._served_order)

    def service_order(self) -> List[int]:
        """Packet ids in the order they were served."""
        return list(self._served_order_ids)

    def reset_stats(self) -> None:
        self.stats = QueueStats()
        self._enqueue_times.clear()
        self.class_enqueued = {c: 0 for c in TRAFFIC_CLASSES}
        self.class_served = {c: 0 for c in TRAFFIC_CLASSES}
        self._served_order.clear()
        self._served_order_ids.clear()

    # -- helpers ----------------------------------------------------------
    def _iter_packets(self) -> List[Packet]:
        raise NotImplementedError

    def empty(self) -> bool:
        return len(self) == 0

    def __len__(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError


class FIFOQueue(BaseQueue):
    """First in, first out -- traffic classes are not differentiated."""

    name = "FIFO"

    def __init__(self) -> None:
        super().__init__()
        self._queue: deque = deque()

    def push(self, packet: Packet, now: Optional[float] = None) -> None:
        self._queue.append(packet)
        self._note_enqueue(packet, now)
        self._sync_length()

    def pop(self, now: Optional[float] = None) -> Packet:
        if not self._queue:
            raise IndexError("FIFOQueue is empty.")
        packet = self._queue.popleft()
        self._note_dequeue(packet, now)
        self._track_served(packet)
        self._sync_length()
        return packet

    def _iter_packets(self) -> List[Packet]:
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)


class PriorityQueue(BaseQueue):
    """Strict priority queueing; ties are broken by arrival order."""

    name = "Priority"

    def __init__(self, priorities: Optional[Dict[str, float]] = None) -> None:
        super().__init__()
        self.priorities: Dict[str, float] = dict(priorities) if priorities else dict(PRIORITIES)
        self._queue: List[tuple] = []
        self._counter = 0

    def set_priorities(self, priorities: Dict[str, float]) -> None:
        unknown = set(priorities) - set(self.priorities)
        if unknown:
            raise ValueError(f"Unknown traffic classes: {sorted(unknown)}")
        self.priorities.update({k: float(v) for k, v in priorities.items()})

    def push(self, packet: Packet, now: Optional[float] = None) -> None:
        priority = float(self.priorities.get(packet.traffic_type, packet.priority))
        heapq.heappush(self._queue, (-priority, self._counter, packet))
        self._counter += 1
        self._note_enqueue(packet, now)
        self._sync_length()

    def pop(self, now: Optional[float] = None) -> Packet:
        if not self._queue:
            raise IndexError("PriorityQueue is empty.")
        packet = heapq.heappop(self._queue)[2]
        self._note_dequeue(packet, now)
        self._track_served(packet)
        self._sync_length()
        return packet

    def _iter_packets(self) -> List[Packet]:
        return [entry[2] for entry in self._queue]

    def __len__(self) -> int:
        return len(self._queue)


class WFQ(BaseQueue):
    """
    Weighted Fair Queueing.

    Each traffic class owns a backlog queue and accumulates service credit at a
    rate proportional to its configured weight. On every dequeue the backlogged
    class with the highest credit is selected and charged the total backlogged
    weight, which makes the long-run service share of a class proportional to
    its weight while guaranteeing that no class starves.
    """

    name = "WFQ"

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        super().__init__()
        self.weights: Dict[str, float] = normalize_class_weights(weights)
        self._credits: Dict[str, float] = dict(self.weights)
        self.queues: Dict[str, deque] = {name: deque() for name in self.weights}

    # -- configuration ----------------------------------------------------
    def set_weights(self, weights: Dict[str, float]) -> None:
        """Replace the per-class service weights."""
        self.weights = normalize_class_weights({**self.weights, **weights})
        for name in self.weights:
            self.queues.setdefault(name, deque())
        self._credits = dict(self.weights)

    def get_weights(self) -> Dict[str, float]:
        return dict(self.weights)

    # -- queue interface ---------------------------------------------------
    def push(self, packet: Packet, now: Optional[float] = None) -> None:
        self.queues.setdefault(packet.traffic_type, deque()).append(packet)
        self._credits.setdefault(
            packet.traffic_type, self.weights.get(packet.traffic_type, 1.0)
        )
        self._note_enqueue(packet, now)
        self._sync_length()

    def pop(self, now: Optional[float] = None) -> Packet:
        available = [name for name, queue in self.queues.items() if queue]
        if not available:
            raise IndexError("WFQ is empty.")

        selected = max(available, key=lambda name: self._credits.get(name, 0.0))
        packet = self.queues[selected].popleft()

        # Charge the selected class and credit every backlogged class so that
        # share of service converges to share of weight.
        total_weight = sum(self.weights.get(name, 0.0) for name in available) or 1.0
        for name in available:
            self._credits[name] = (
                self._credits.get(name, 0.0) + self.weights.get(name, 0.0)
            )
        self._credits[selected] -= total_weight

        self._note_dequeue(packet, now)
        self._track_served(packet)
        self._sync_length()
        return packet

    def class_lengths(self) -> Dict[str, int]:
        return {name: len(queue) for name, queue in self.queues.items()}

    def _iter_packets(self) -> List[Packet]:
        packets: List[Packet] = []
        for queue in self.queues.values():
            packets.extend(queue)
        return packets

    def __len__(self) -> int:
        return sum(len(queue) for queue in self.queues.values())


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def create_scheduler(name: str, **params: Any) -> BaseQueue:
    """
    Create a scheduler by name.

    ``params`` may contain ``weights`` (WFQ) or ``priorities`` (Priority Queue)
    so experiments can configure a scheduler through a single entry point.
    """
    key = normalize_scheduler_name(name)

    if key == "fifo":
        return FIFOQueue()

    if key == "priority":
        return PriorityQueue(priorities=params.get("priorities"))

    return WFQ(weights=params.get("weights"))
