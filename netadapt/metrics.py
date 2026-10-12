from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
import pandas as pd

from qos import TRAFFIC_CLASSES


def jitter_from_latencies(latencies: List[float]) -> float:
    """
    Packet delay variation (RFC 3550 style mean absolute difference).

    Computed from the latencies of consecutive delivered packets, in delivery
    order. Returns seconds; ``0.0`` when fewer than two samples exist.
    """
    if len(latencies) < 2:
        return 0.0

    total = 0.0
    for previous, current in zip(latencies, latencies[1:]):
        total += abs(current - previous)

    return total / (len(latencies) - 1)


@dataclass
class PacketRecord:
    packet_id: int
    sent_time: float
    delivered_time: float | None
    latency: float | None
    size: int
    status: str
    flow_id: str | None = None
    route: List[str] | None = None
    traffic_type: str | None = None
    queue_wait: float | None = None


@dataclass
class RecoveryRecord:
    failure_time: float
    detection_time: float | None = None
    recalculation_time: float | None = None
    recovery_time: float | None = None
    component: str = ""
    component_type: str = ""  # LINK or NODE


class TransportMetrics:
    """Transport-specific counters layered beside the original metrics."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.records: Dict[str, List[PacketRecord]] = {"TCP": [], "UDP": []}
        self.retransmissions: Dict[str, int] = {"TCP": 0, "UDP": 0}
        self.timeouts: Dict[str, int] = {"TCP": 0, "UDP": 0}
        self.connection_setup_time: Dict[str, float] = {"TCP": 0.0, "UDP": 0.0}

    def record(
        self,
        protocol: str,
        packet_id: int,
        sent_time: float,
        delivered_time: float | None,
        latency: float | None,
        size: int,
        status: str,
        flow_id: str | None = None,
        route: List[str] | None = None,
        traffic_type: str | None = None,
    ) -> None:
        key = str(protocol).upper()
        if key not in self.records:
            return
        self.records[key].append(
            PacketRecord(
                packet_id,
                sent_time,
                delivered_time,
                latency,
                size,
                status,
                flow_id=flow_id,
                route=route,
                traffic_type=traffic_type,
            )
        )

    def calculate(self, protocol: str, current_time: float, retransmissions: int = 0, timeout_count: int = 0, setup_time: float = 0.0) -> Dict[str, float]:
        key = str(protocol).upper()
        records = self.records.get(key, [])
        delivered = [record for record in records if record.status == "DELIVERED"]
        dropped = sum(record.status in {"DROPPED", "LOST"} for record in records)
        latencies = [record.latency for record in delivered if record.latency is not None]
        duration = max(float(current_time), 0.001)
        return {
            "packets_sent": float(len(records)),
            "packets_delivered": float(len(delivered)),
            "packets_lost": float(dropped),
            "packet_delivery_ratio": (len(delivered) / len(records) * 100.0) if records else 100.0,
            "average_latency": (sum(latencies) / len(latencies)) if latencies else 0.0,
            "throughput": sum(record.size for record in delivered) / duration,
            "bytes_transferred": float(sum(record.size for record in delivered)),
            "retransmissions": float(retransmissions),
            "timeout_count": float(timeout_count),
            "connection_setup_time": float(setup_time),
        }


class ServiceMetrics:
    """Per-service counters layered beside the original metrics (Stage 10).

    One registry instance tracks DHCP, DNS, HTTP, FTP, and SMTP activity so the
    numbers are aggregated from real simulated packet exchanges rather than
    fabricated by the frontend.
    """

    #: Services that always report a counter block, even before first use.
    SERVICES = ("DHCP", "DNS", "HTTP", "FTP", "SMTP")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counters: Dict[str, Dict[str, float]] = {
            name: {} for name in self.SERVICES
        }

    def increment(self, service: str, key: str, amount: float = 1.0) -> float:
        name = str(service).upper()
        bucket = self.counters.setdefault(name, {})
        bucket[key] = bucket.get(key, 0.0) + float(amount)
        return bucket[key]

    def record_latency(self, service: str, latency: float) -> None:
        if latency is None:
            return
        self.increment(service, "latency_total", float(latency))
        self.increment(service, "latency_samples", 1.0)

    def get(self, service: str, key: str, default: float = 0.0) -> float:
        return self.counters.get(str(service).upper(), {}).get(key, default)

    def calculate(self, service: str) -> Dict[str, float]:
        name = str(service).upper()
        bucket = dict(self.counters.get(name, {}))
        samples = bucket.pop("latency_samples", 0.0)
        total = bucket.pop("latency_total", 0.0)
        bucket["latency_samples"] = samples
        bucket["average_latency"] = (total / samples) if samples else 0.0
        return bucket

    def to_dict(self) -> Dict[str, Dict[str, float]]:
        return {name: self.calculate(name) for name in sorted(self.counters)}


class SecurityMetrics:
    """Firewall, ACL, ARP, and flood counters (Stage 10)."""

    KEYS = (
        "packets_inspected",
        "packets_allowed",
        "packets_blocked",
        "firewall_blocks",
        "port_blocks",
        "acl_blocks",
        "arp_conflicts",
        "spoof_attempts",
        "poisoned_entries",
        "floods_detected",
        "attack_packets_dropped",
        "normal_packets",
        "suspicious_packets",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counters: Dict[str, float] = {key: 0.0 for key in self.KEYS}

    def increment(self, key: str, amount: float = 1.0) -> float:
        self.counters[key] = self.counters.get(key, 0.0) + float(amount)
        return self.counters[key]

    def get(self, key: str) -> float:
        return self.counters.get(key, 0.0)

    def calculate(self) -> Dict[str, float]:
        return dict(self.counters)

    def to_dict(self) -> Dict[str, float]:
        return self.calculate()


class Metrics:
    def __init__(self) -> None:
        self.records: List[PacketRecord] = []
        self.history: List[Dict[str, Any]] = []
        self.recovery_records: List[RecoveryRecord] = []
        self.active_flows_count: int = 0
        self._snapshots_before_failure: List[Dict[str, Any]] = []
        self._snapshots_during_failure: List[Dict[str, Any]] = []
        self._snapshots_after_recovery: List[Dict[str, Any]] = []

    def record(
        self,
        packet_id: int,
        sent_time: float,
        delivered_time: float | None,
        latency: float | None,
        size: int,
        status: str,
        flow_id: str | None = None,
        route: List[str] | None = None,
        *,
        traffic_type: str | None = None,
        queue_wait: float | None = None,
    ) -> None:
        self.records.append(
            PacketRecord(
                packet_id,
                sent_time,
                delivered_time,
                latency,
                size,
                status,
                flow_id=flow_id,
                route=route,
                traffic_type=traffic_type,
                queue_wait=queue_wait,
            )
        )

    def calculate(
        self,
        current_time: float | None = None,
        congestion: float = 0.0,
        active_flows: int | None = None,
    ) -> Dict[str, float]:
        sent = len(self.records)
        delivered = sum(r.status == "DELIVERED" for r in self.records)
        dropped = sum(r.status == "DROPPED" for r in self.records)

        latencies = [
            r.latency
            for r in self.records
            if r.status == "DELIVERED" and r.latency is not None
        ]

        delivery_order_latencies = [
            r.latency
            for r in sorted(
                (r for r in self.records if r.status == "DELIVERED" and r.latency is not None),
                key=lambda r: (r.delivered_time if r.delivered_time is not None else r.sent_time),
            )
        ]

        total_bytes = sum(
            r.size for r in self.records if r.status == "DELIVERED"
        )

        duration = max(
            float(current_time or 0.0),
            0.001,
        )

        # Recovery metrics
        detection_times = [
            (r.detection_time - r.failure_time)
            for r in self.recovery_records
            if r.detection_time is not None
        ]
        recovery_times = [
            (r.recovery_time - r.failure_time)
            for r in self.recovery_records
            if r.recovery_time is not None
        ]
        recalc_times = [
            (r.recalculation_time - r.detection_time)
            for r in self.recovery_records
            if r.recalculation_time is not None and r.detection_time is not None
        ]

        avg_detection = sum(detection_times) / len(detection_times) if detection_times else 0.0
        avg_recovery = sum(recovery_times) / len(recovery_times) if recovery_times else 0.0
        avg_recalc = sum(recalc_times) / len(recalc_times) if recalc_times else 0.0

        result: Dict[str, float] = {
            "packets_sent": float(sent),
            "packets_delivered": float(delivered),
            "packets_dropped": float(dropped),
            "average_latency": (
                sum(latencies) / len(latencies)
                if latencies else 0.0
            ),
            "jitter": jitter_from_latencies(delivery_order_latencies),
            "packet_loss": (
                dropped / sent * 100
                if sent else 0.0
            ),
            "packet_delivery_ratio": (
                delivered / sent * 100
                if sent else 0.0
            ),
            "throughput": total_bytes / duration,
            "congestion": max(0.0, min(1.0, congestion)),
            "active_flows": float(active_flows if active_flows is not None else self.active_flows_count),
            "failure_detection_time": float(avg_detection),
            "recovery_time": float(avg_recovery),
            "route_recalculation_time": float(avg_recalc),
        }
        return result

    def snapshot(
        self,
        current_time: float,
        congestion: float = 0.0,
        active_flows: int | None = None,
    ) -> Dict[str, float]:
        values = self.calculate(current_time, congestion, active_flows)
        values["time"] = current_time
        self.history.append(values.copy())
        return values

    def add_recovery_record(self, record: RecoveryRecord) -> None:
        self.recovery_records.append(record)

    def update_recovery_detection(self, component: str, detection_time: float) -> None:
        for r in reversed(self.recovery_records):
            if r.component == component and r.detection_time is None:
                r.detection_time = detection_time
                break

    def update_recovery_recalculation(self, component: str, recalc_time: float) -> None:
        for r in reversed(self.recovery_records):
            if r.component == component and r.recalculation_time is None:
                r.recalculation_time = recalc_time
                break

    def update_recovery_recovered(self, component: str, recovery_time: float) -> None:
        for r in reversed(self.recovery_records):
            if r.component == component and r.recovery_time is None:
                r.recovery_time = recovery_time
                break

    # ------------------------------------------------------------------
    # Per traffic-class QoS metrics (Stage 4)
    # ------------------------------------------------------------------
    def records_for_class(self, traffic_class: str) -> List[PacketRecord]:
        return [r for r in self.records if r.traffic_type == traffic_class]

    def class_metrics(
        self,
        current_time: float | None = None,
        classes: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Latency, jitter, throughput, loss, PDR and queue waiting time for every
        traffic class, aggregated from real packet records.
        """
        duration = max(float(current_time or 0.0), 0.001)
        result: Dict[str, Dict[str, Any]] = {}

        for traffic_class in (classes or TRAFFIC_CLASSES):
            records = self.records_for_class(traffic_class)
            sent = len(records)
            delivered_records = [r for r in records if r.status == "DELIVERED"]
            dropped = sum(1 for r in records if r.status == "DROPPED")
            delivered = len(delivered_records)

            latencies = [
                r.latency for r in delivered_records if r.latency is not None
            ]
            ordered_latencies = [
                r.latency
                for r in sorted(
                    delivered_records,
                    key=lambda r: (
                        r.delivered_time if r.delivered_time is not None else r.sent_time
                    ),
                )
                if r.latency is not None
            ]
            waits = [r.queue_wait for r in records if r.queue_wait is not None]
            delivered_bytes = sum(r.size for r in delivered_records)

            result[traffic_class] = {
                "traffic_class": traffic_class,
                "packets_sent": float(sent),
                "packets_delivered": float(delivered),
                "packets_dropped": float(dropped),
                "average_latency": (
                    sum(latencies) / len(latencies) if latencies else 0.0
                ),
                "jitter": jitter_from_latencies(ordered_latencies),
                "throughput": delivered_bytes / duration,
                "packet_loss": (dropped / sent * 100) if sent else 0.0,
                "packet_delivery_ratio": (delivered / sent * 100) if sent else 0.0,
                "average_queue_wait": (
                    sum(waits) / len(waits) if waits else 0.0
                ),
                "max_queue_wait": max(waits) if waits else 0.0,
                "delivered_bytes": float(delivered_bytes),
            }

        return result

    def class_dataframe(
        self,
        current_time: float | None = None,
        classes: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Per-class QoS metrics as a DataFrame (one row per traffic class)."""
        metrics = self.class_metrics(current_time, classes)
        rows = [dict(values) for values in metrics.values()]
        return pd.DataFrame(rows)

    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.history)

    def records_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(asdict(r) for r in self.records)

    def recovery_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(asdict(r) for r in self.recovery_records)

    def reset(self) -> None:
        self.records.clear()
        self.history.clear()
        self.recovery_records.clear()
        self.active_flows_count = 0
        self._snapshots_before_failure.clear()
        self._snapshots_during_failure.clear()
        self._snapshots_after_recovery.clear()

    def export_csv(self, path: str) -> None:
        self.dataframe().to_csv(path, index=False)

    def get_latest(self) -> Dict[str, float]:
        if self.history:
            return self.history[-1]
        return self.calculate(0.0)

    # Before/during/after support
    def save_baseline(self) -> None:
        if self.history:
            self._snapshots_before_failure.append(self.history[-1].copy())

    def save_during_failure(self) -> None:
        if self.history:
            self._snapshots_during_failure.append(self.history[-1].copy())

    def save_after_recovery(self) -> None:
        if self.history:
            self._snapshots_after_recovery.append(self.history[-1].copy())

    def get_comparison(self) -> Dict[str, Dict[str, float]]:
        def avg_snapshots(snapshots: List[Dict[str, Any]]) -> Dict[str, float]:
            if not snapshots:
                return {}
            keys = ["average_latency", "throughput", "packet_loss", "packet_delivery_ratio", "congestion"]
            result = {}
            for k in keys:
                vals = [s.get(k, 0.0) for s in snapshots if k in s]
                result[k] = sum(vals) / len(vals) if vals else 0.0
            return result

        # If no explicit baseline saved, use first half of history as baseline
        if not self._snapshots_before_failure and self.history:
            mid = len(self.history) // 3
            if mid > 0:
                return {
                    "before": avg_snapshots(self.history[:mid]),
                    "during": avg_snapshots(self.history[mid:2*mid]),
                    "after": avg_snapshots(self.history[2*mid:]),
                }

        return {
            "before": avg_snapshots(self._snapshots_before_failure),
            "during": avg_snapshots(self._snapshots_during_failure),
            "after": avg_snapshots(self._snapshots_after_recovery),
        }
