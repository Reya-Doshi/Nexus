"""
Event system for NetAdapt simulation.

Provides structured event logging with simulation timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
import enum


class EventType(str, enum.Enum):
    TRAFFIC_STARTED = "TRAFFIC_STARTED"
    PACKET_SENT = "PACKET_SENT"
    PACKET_DELIVERED = "PACKET_DELIVERED"
    PACKET_DROPPED = "PACKET_DROPPED"
    LINK_FAILED = "LINK_FAILED"
    NODE_FAILED = "NODE_FAILED"
    FAILURE_DETECTED = "FAILURE_DETECTED"
    ROUTE_RECALCULATED = "ROUTE_RECALCULATED"
    TRAFFIC_REROUTED = "TRAFFIC_REROUTED"
    LINK_RECOVERED = "LINK_RECOVERED"
    NODE_RECOVERED = "NODE_RECOVERED"
    CONGESTION_CHANGED = "CONGESTION_CHANGED"
    PACKET_LOSS_CHANGED = "PACKET_LOSS_CHANGED"
    BANDWIDTH_CHANGED = "BANDWIDTH_CHANGED"
    SIMULATION_RESET = "SIMULATION_RESET"
    LINK_UPDATED = "LINK_UPDATED"
    PACKET_GENERATED = "PACKET_GENERATED"
    FLOW_COMPLETED = "FLOW_COMPLETED"
    FLOW_FAILED = "FLOW_FAILED"
    SCHEDULER_CHANGED = "SCHEDULER_CHANGED"
    QOS_CONFIG_CHANGED = "QOS_CONFIG_CHANGED"
    ROUTING_ALGORITHM_CHANGED = "ROUTING_ALGORITHM_CHANGED"
    ROUTING_WEIGHTS_CHANGED = "ROUTING_WEIGHTS_CHANGED"
    ARP_REQUEST = "ARP_REQUEST"
    ARP_REPLY = "ARP_REPLY"
    ARP_CACHE_UPDATE = "ARP_CACHE_UPDATE"
    ARP_MISS = "ARP_MISS"
    ARP_CLEARED = "ARP_CLEARED"
    ICMP_ECHO_REQUEST = "ICMP_ECHO_REQUEST"
    ICMP_ECHO_REPLY = "ICMP_ECHO_REPLY"
    ICMP_TTL_EXCEEDED = "ICMP_TTL_EXCEEDED"
    DESTINATION_UNREACHABLE = "DESTINATION_UNREACHABLE"
    PACKET_FORWARDED = "PACKET_FORWARDED"
    ROUTE_LOOKUP = "ROUTE_LOOKUP"
    MAC_LEARNED = "MAC_LEARNED"
    MAC_FLOOD = "MAC_FLOOD"
    MAC_TABLE_CLEARED = "MAC_TABLE_CLEARED"
    TCP_CONNECTION_STARTED = "TCP_CONNECTION_STARTED"
    TCP_SYN_SENT = "TCP_SYN_SENT"
    TCP_SYN_ACK_SENT = "TCP_SYN_ACK_SENT"
    TCP_SYN_ACK_RECEIVED = "TCP_SYN_ACK_RECEIVED"
    TCP_ACK_RECEIVED = "TCP_ACK_RECEIVED"
    TCP_HANDSHAKE_COMPLETE = "TCP_HANDSHAKE_COMPLETE"
    TCP_DATA_SENT = "TCP_DATA_SENT"
    TCP_PACKET_LOST = "TCP_PACKET_LOST"
    TCP_TIMEOUT = "TCP_TIMEOUT"
    TCP_RETRANSMISSION = "TCP_RETRANSMISSION"
    TCP_SLOW_START = "TCP_SLOW_START"
    TCP_CONGESTION_AVOIDANCE = "TCP_CONGESTION_AVOIDANCE"
    TCP_CONGESTION_WINDOW_CHANGED = "TCP_CONGESTION_WINDOW_CHANGED"
    TCP_CONGESTION_DETECTED = "TCP_CONGESTION_DETECTED"
    TCP_FIN_SENT = "TCP_FIN_SENT"
    TCP_FIN_RECEIVED = "TCP_FIN_RECEIVED"
    TCP_CONNECTION_CLOSED = "TCP_CONNECTION_CLOSED"
    UDP_FLOW_STARTED = "UDP_FLOW_STARTED"
    UDP_DATA_SENT = "UDP_DATA_SENT"
    # Stage 10 - simulated network services
    SERVICE_STARTED = "SERVICE_STARTED"
    SERVICE_STOPPED = "SERVICE_STOPPED"
    SERVICE_FAILED = "SERVICE_FAILED"
    SERVICE_REQUEST = "SERVICE_REQUEST"
    SERVICE_RESPONSE = "SERVICE_RESPONSE"
    DHCP_DISCOVER = "DHCP_DISCOVER"
    DHCP_OFFER = "DHCP_OFFER"
    DHCP_REQUEST = "DHCP_REQUEST"
    DHCP_ACK = "DHCP_ACK"
    DHCP_NAK = "DHCP_NAK"
    DHCP_RELEASE = "DHCP_RELEASE"
    DNS_QUERY = "DNS_QUERY"
    DNS_RESPONSE = "DNS_RESPONSE"
    DNS_CACHE_HIT = "DNS_CACHE_HIT"
    DNS_CACHE_MISS = "DNS_CACHE_MISS"
    HTTP_REQUEST = "HTTP_REQUEST"
    HTTP_RESPONSE = "HTTP_RESPONSE"
    FTP_CONNECTION = "FTP_CONNECTION"
    FTP_TRANSFER = "FTP_TRANSFER"
    SMTP_MESSAGE = "SMTP_MESSAGE"
    # Stage 10 - simulated network security
    FIREWALL_RULE_MATCHED = "FIREWALL_RULE_MATCHED"
    PACKET_ALLOWED = "PACKET_ALLOWED"
    PACKET_BLOCKED = "PACKET_BLOCKED"
    ACL_MATCHED = "ACL_MATCHED"
    ACL_DENIED = "ACL_DENIED"
    ARP_SPOOF_ATTEMPT = "ARP_SPOOF_ATTEMPT"
    ARP_CACHE_POISONED = "ARP_CACHE_POISONED"
    ARP_CONFLICT = "ARP_CONFLICT"
    ARP_ANOMALY_DETECTED = "ARP_ANOMALY_DETECTED"
    TRAFFIC_SPIKE = "TRAFFIC_SPIKE"
    FLOOD_DETECTED = "FLOOD_DETECTED"
    FLOOD_STARTED = "FLOOD_STARTED"


@dataclass
class Event:
    """Structured simulation event."""

    timestamp: float
    event_type: str
    message: str
    component: Optional[str] = None
    flow_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time": round(self.timestamp, 4),
            "event": self.event_type,
            "component": self.component,
            "flow_id": self.flow_id,
            "message": self.message,
            "details": self.details,
        }

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Convert to legacy flat dict format for backward compatibility."""
        d = {
            "time": round(self.timestamp, 4),
            "event": self.event_type,
            "message": self.message,
        }
        if self.component:
            # Preserve old keys like link, node for compatibility
            if "-" in self.component or "↔" in self.component:
                d["link"] = self.component
            else:
                d["node"] = self.component
            d["component"] = self.component
        if self.flow_id:
            d["flow_id"] = self.flow_id
        d.update(self.details)
        return d


class EventLogger:
    """Manages event logging with simulation clock."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._legacy_events: list[Dict[str, Any]] = []

    def log(
        self,
        timestamp: float,
        event_type: str,
        message: str,
        component: Optional[str] = None,
        flow_id: Optional[str] = None,
        **details: Any,
    ) -> Event:
        evt = Event(
            timestamp=timestamp,
            event_type=event_type,
            message=message,
            component=component,
            flow_id=flow_id,
            details=details,
        )
        self.events.append(evt)
        self._legacy_events.append(evt.to_legacy_dict())
        return evt

    def get_events(self, limit: Optional[int] = None) -> list[Event]:
        if limit is None:
            return list(self.events)
        return list(self.events[-limit:])

    def get_legacy_events(self, limit: Optional[int] = None) -> list[Dict[str, Any]]:
        if limit is None:
            return list(self._legacy_events)
        return list(self._legacy_events[-limit:])

    def clear(self) -> None:
        self.events.clear()
        self._legacy_events.clear()

    def to_dict_list(self) -> list[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]
