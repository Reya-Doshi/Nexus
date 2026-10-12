"""Stage 10 - simulated network services and basic network security.

Everything in this module runs *inside* the NetAdapt simulator.  No real
socket, file, DNS, HTTP, FTP, or mail system is used, and no packet ever leaves
the simulated topology.  The layer is attached to the single existing
:class:`~simulator.NetworkSimulator` and therefore reuses the same routing,
QoS scheduler, packet loss, failure, event, and metric systems as Stages 1-9.

Security features (firewall, ACLs, ARP spoofing, floods) are educational
simulations of packet decisions inside NetAdapt.  They never touch the real
network of the machine running the lab.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from devices import Device
from events import EventType
from metrics import SecurityMetrics, ServiceMetrics
from transport import TCPState, TransportPacket


# --------------------------------------------------------------------------
# Service vocabulary
# --------------------------------------------------------------------------

SERVICE_NAMES: Tuple[str, ...] = ("DHCP", "DNS", "HTTP", "FTP", "SMTP")

#: Well-known ports used by the simulated services.
SERVICE_PORTS: Dict[str, int] = {
    "DHCP": 67,
    "DHCP_CLIENT": 68,
    "DNS": 53,
    "HTTP": 80,
    "FTP": 21,
    "SMTP": 25,
}

#: Transport protocol used by each simulated service.
SERVICE_PROTOCOLS: Dict[str, str] = {
    "DHCP": "UDP",
    "DNS": "UDP",
    "HTTP": "TCP",
    "FTP": "TCP",
    "SMTP": "TCP",
}

#: Existing NetAdapt QoS classes reused by service traffic (no second QoS system).
SERVICE_TRAFFIC_CLASS: Dict[str, str] = {
    "DHCP": "Emergency",   # small, high priority
    "DNS": "Emergency",    # small, high priority
    "HTTP": "HTTP",        # normal interactive priority
    "SMTP": "HTTP",        # normal interactive priority
    "FTP": "FTP",          # bulk transfer class
}

#: TCP/UDP port -> service identification used by the packet inspector.
PORT_SERVICES: Dict[Tuple[str, int], str] = {
    ("UDP", 53): "DNS",
    ("UDP", 67): "DHCP",
    ("UDP", 68): "DHCP",
    ("TCP", 21): "FTP",
    ("TCP", 25): "SMTP",
    ("TCP", 53): "DNS",
    ("TCP", 80): "HTTP",
}

#: Canonical drop reasons surfaced by the packet inspector and CLI.
DROP_FIREWALL = "FIREWALL_BLOCK"
DROP_ACL = "ACL_DENY"
DROP_PORT = "PORT_BLOCKED"
DROP_FLOOD = "FLOOD_PROTECTION"
DROP_ARP = "ARP_CONFLICT"

ATTACK_PROTOCOLS: Tuple[str, ...] = ("TCP", "UDP", "ICMP")

#: Attack packets reuse the existing QoS classes instead of a new system.
ATTACK_TRAFFIC_CLASS: Dict[str, str] = {
    "TCP": "HTTP",
    "UDP": "VoIP",
    "ICMP": "Emergency",
}

ATTACK_PORTS: Dict[str, Tuple[int, int]] = {
    "TCP": (49152, 80),
    "UDP": (49153, 53),
    "ICMP": (0, 0),
}


def normalise_service(name: str) -> str:
    """Return a canonical service name (``DHCP``/``DNS``/``HTTP``/``FTP``/``SMTP``)."""

    key = str(name).strip().upper()
    if key not in SERVICE_NAMES:
        raise ValueError(f"Unknown service: {name}")
    return key


def match_ip(pattern: Optional[str], address: Optional[str]) -> bool:
    """Match ``any``, an exact address, a ``*`` wildcard, or a CIDR block."""

    if pattern in (None, "", "any", "ANY", "*"):
        return True
    if address in (None, "", "any"):
        return False
    text = str(pattern).strip()
    if "*" in text:
        regex = "^" + "\\.".join(
            ".*" if part == "*" else part.replace(".", r"\.") for part in text.split(".")
        ) + "$"
        return re.match(regex, str(address)) is not None
    try:
        if "/" in text:
            return ipaddress.IPv4Address(str(address)) in ipaddress.IPv4Network(
                text, strict=False
            )
        return str(address) == str(ipaddress.IPv4Address(text))
    except ValueError:
        return False


def port_service(protocol: Optional[str], port: Optional[int]) -> Optional[str]:
    """Identify the service that owns ``protocol``/``port``."""

    if protocol is None or port is None:
        return None
    return PORT_SERVICES.get((str(protocol).upper(), int(port)))


# --------------------------------------------------------------------------
# Service registry
# --------------------------------------------------------------------------


@dataclass
class ServiceInstance:
    """One service hosted by one simulated device."""

    name: str
    device: str
    state: str = "RUNNING"
    port: int = 0
    protocol: str = "TCP"
    traffic_class: str = "HTTP"
    interface_id: str = "eth0"
    config: Dict[str, Any] = field(default_factory=dict)
    requests: int = 0
    failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.name,
            "name": self.name,
            "device": self.device,
            "state": self.state,
            "port": self.port,
            "protocol": self.protocol,
            "traffic_class": self.traffic_class,
            "interface_id": self.interface_id,
            "requests": self.requests,
            "failures": self.failures,
            "config": dict(self.config),
        }


class ServiceRegistry:
    """Device-hosted services with real RUNNING/STOPPED state."""

    def __init__(self, layer: "ServiceSecurityLayer") -> None:
        self.layer = layer
        self.services: Dict[Tuple[str, str], ServiceInstance] = {}

    def install(
        self,
        name: str,
        device: str,
        **config: Any,
    ) -> ServiceInstance:
        service = normalise_service(name)
        self.layer.device(device)
        key = (service, str(device))
        instance = self.services.get(key)
        if instance is None:
            instance = ServiceInstance(
                name=service,
                device=str(device),
                port=SERVICE_PORTS[service],
                protocol=SERVICE_PROTOCOLS[service],
                traffic_class=SERVICE_TRAFFIC_CLASS[service],
            )
            self.services[key] = instance
        if config:
            instance.config.update(config)
        self.layer.ensure_defaults(instance)
        self.layer.log(
            EventType.SERVICE_STARTED,
            f"{service} service installed on {device}:{instance.port}",
            component=str(device),
            service=service,
            port=instance.port,
            protocol=instance.protocol,
            state=instance.state,
        )
        return instance

    def remove(self, name: str, device: str) -> bool:
        key = (normalise_service(name), str(device))
        instance = self.services.pop(key, None)
        if instance is None:
            return False
        self.layer.log(
            EventType.SERVICE_STOPPED,
            f"{instance.name} service removed from {device}",
            component=str(device),
            service=instance.name,
        )
        return True

    def set_state(self, name: str, device: str, state: str) -> ServiceInstance:
        instance = self.require(name, device)
        normalized = str(state).upper()
        if normalized not in {"RUNNING", "STOPPED"}:
            raise ValueError("Service state must be RUNNING or STOPPED")
        instance.state = normalized
        self.layer.log(
            EventType.SERVICE_STARTED
            if normalized == "RUNNING"
            else EventType.SERVICE_STOPPED,
            f"{instance.name} service on {device} is {normalized}",
            component=str(device),
            service=instance.name,
            state=normalized,
            port=instance.port,
        )
        return instance

    def start(self, name: str, device: str) -> ServiceInstance:
        return self.set_state(name, device, "RUNNING")

    def stop(self, name: str, device: str) -> ServiceInstance:
        return self.set_state(name, device, "STOPPED")

    def restart(self, name: str, device: str) -> ServiceInstance:
        self.set_state(name, device, "STOPPED")
        return self.set_state(name, device, "RUNNING")

    def configure(self, name: str, device: str, values: Dict[str, Any]) -> ServiceInstance:
        instance = self.require(name, device)
        for key, value in dict(values).items():
            instance.config[str(key)] = value
        self.layer.ensure_defaults(instance)
        return instance

    def get(self, name: str, device: str) -> Optional[ServiceInstance]:
        return self.services.get((normalise_service(name), str(device)))

    def require(self, name: str, device: str) -> ServiceInstance:
        instance = self.get(name, device)
        if instance is None:
            raise ValueError(f"No {normalise_service(name)} service on {device}")
        return instance

    def find(self, name: str, device: Optional[str] = None) -> Optional[ServiceInstance]:
        service = normalise_service(name)
        if device:
            return self.get(service, device)
        for (candidate, _), instance in self.services.items():
            if candidate == service and instance.state == "RUNNING":
                return instance
        return None

    def list(self, device: Optional[str] = None) -> List[ServiceInstance]:
        instances = list(self.services.values())
        if device:
            instances = [item for item in instances if item.device == str(device)]
        return sorted(instances, key=lambda item: (item.device, SERVICE_NAMES.index(item.name)))

    def to_dict(self, device: Optional[str] = None) -> List[Dict[str, Any]]:
        return [instance.to_dict() for instance in self.list(device)]

    def clear(self) -> None:
        self.services.clear()


# --------------------------------------------------------------------------
# DHCP
# --------------------------------------------------------------------------


@dataclass
class DhcpLease:
    client: str
    address: str
    mask: str = "255.255.255.0"
    prefix: int = 24
    gateway: Optional[str] = None
    dns: Optional[str] = None
    lease_time: float = 3600.0
    granted_at: float = 0.0
    expires_at: float = 0.0
    server: str = ""
    state: str = "ACTIVE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client": self.client,
            "address": self.address,
            "ip_address": self.address,
            "mask": self.mask,
            "prefix": self.prefix,
            "gateway": self.gateway,
            "dns": self.dns,
            "lease_time": self.lease_time,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "server": self.server,
            "state": self.state,
        }


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------


@dataclass
class DnsRecord:
    hostname: str
    address: str
    ttl: float = 300.0
    type: str = "A"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hostname": self.hostname,
            "address": self.address,
            "type": self.type,
            "ttl": self.ttl,
        }


@dataclass
class DnsCacheEntry:
    hostname: str
    address: str
    expires_at: float
    hits: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hostname": self.hostname,
            "address": self.address,
            "expires_at": self.expires_at,
            "hits": self.hits,
        }


# --------------------------------------------------------------------------
# Firewall
# --------------------------------------------------------------------------


@dataclass
class FirewallRule:
    rule_id: int
    action: str = "DENY"
    source_ip: str = "any"
    destination_ip: str = "any"
    protocol: Optional[str] = None
    source_port: Optional[int] = None
    destination_port: Optional[int] = None
    service: Optional[str] = None
    device: Optional[str] = None
    reason: str = DROP_FIREWALL
    description: str = ""
    packets_matched: int = 0

    def matches(self, info: Dict[str, Any]) -> bool:
        if not match_ip(self.source_ip, info.get("source_ip")):
            return False
        if not match_ip(self.destination_ip, info.get("destination_ip")):
            return False
        if self.protocol and str(self.protocol).upper() != str(info.get("protocol") or "").upper():
            return False
        if self.source_port is not None and int(self.source_port) != info.get("source_port"):
            return False
        if self.destination_port is not None and int(self.destination_port) != info.get("destination_port"):
            return False
        if self.service and str(self.service).upper() != str(info.get("service") or "").upper():
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "source_ip": self.source_ip,
            "destination_ip": self.destination_ip,
            "protocol": self.protocol,
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "service": self.service,
            "device": self.device,
            "reason": self.reason,
            "description": self.description,
            "packets_matched": self.packets_matched,
        }


class Firewall:
    """Stateful simulated firewall (first matching rule wins)."""

    def __init__(self) -> None:
        self.rules: List[FirewallRule] = []
        self.enabled = True
        self._counter = 0
        #: 5-tuple -> decision, so a flow keeps the decision made for its first
        #: packet (stateful behaviour, not a second packet filter).
        self.sessions: Dict[Tuple[Any, ...], str] = {}
        self.packets_inspected = 0
        self.packets_allowed = 0
        self.packets_blocked = 0

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.rules)

    def add_rule(self, **kwargs: Any) -> FirewallRule:
        action = str(kwargs.pop("action", "DENY")).upper()
        if action not in {"ALLOW", "DENY"}:
            raise ValueError("Firewall action must be ALLOW or DENY")
        self._counter += 1
        rule = FirewallRule(
            rule_id=self._counter,
            action=action,
            source_ip=str(kwargs.pop("source_ip", "any") or "any"),
            destination_ip=str(kwargs.pop("destination_ip", "any") or "any"),
            protocol=(str(kwargs["protocol"]).upper() if kwargs.get("protocol") else None),
            source_port=(int(kwargs["source_port"]) if kwargs.get("source_port") else None),
            destination_port=(
                int(kwargs["destination_port"]) if kwargs.get("destination_port") else None
            ),
            service=(str(kwargs["service"]).upper() if kwargs.get("service") else None),
            device=(str(kwargs["device"]) if kwargs.get("device") else None),
            reason=str(kwargs.pop("reason", DROP_FIREWALL)),
            description=str(kwargs.pop("description", "")),
        )
        self.rules.append(rule)
        return rule

    def remove_rule(self, rule_id: int) -> bool:
        for index, rule in enumerate(self.rules):
            if rule.rule_id == int(rule_id):
                del self.rules[index]
                return True
        return False

    def clear(self) -> None:
        self.rules.clear()
        self.sessions.clear()

    def reset_counters(self) -> None:
        self.packets_inspected = 0
        self.packets_allowed = 0
        self.packets_blocked = 0
        self.sessions.clear()
        for rule in self.rules:
            rule.packets_matched = 0

    def applicable(self, info: Dict[str, Any], path: List[str]) -> List[FirewallRule]:
        devices = set(path or [])
        return [
            rule
            for rule in self.rules
            if rule.device is None or rule.device in devices
        ]

    def evaluate(
        self,
        info: Dict[str, Any],
        path: Optional[List[str]] = None,
    ) -> Tuple[Optional[FirewallRule], str, bool]:
        """Return ``(matched_rule, action, stateful)`` for a packet."""

        if not self.active:
            return None, "ALLOW", False
        self.packets_inspected += 1
        session_key = (
            info.get("source"),
            info.get("destination"),
            info.get("protocol"),
            info.get("source_port"),
            info.get("destination_port"),
        )
        if session_key in self.sessions:
            return None, self.sessions[session_key], True
        for rule in self.applicable(info, list(path or [])):
            if rule.matches(info):
                rule.packets_matched += 1
                self.sessions[session_key] = rule.action
                return rule, rule.action, False
        self.sessions[session_key] = "ALLOW"
        return None, "ALLOW", False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "default_action": "ALLOW",
            "packets_inspected": self.packets_inspected,
            "packets_allowed": self.packets_allowed,
            "packets_blocked": self.packets_blocked,
            "rules": [rule.to_dict() for rule in self.rules],
        }


# --------------------------------------------------------------------------
# Access control lists
# --------------------------------------------------------------------------


@dataclass
class AclEntry:
    sequence: int
    action: str = "DENY"
    source_ip: str = "any"
    destination_ip: str = "any"
    protocol: Optional[str] = None
    source_port: Optional[int] = None
    destination_port: Optional[int] = None
    remark: str = ""

    def matches(self, info: Dict[str, Any], extended: bool) -> bool:
        if not match_ip(self.source_ip, info.get("source_ip")):
            return False
        if not extended:
            return True
        if not match_ip(self.destination_ip, info.get("destination_ip")):
            return False
        if self.protocol and str(self.protocol).upper() != str(info.get("protocol") or "").upper():
            return False
        if self.source_port is not None and int(self.source_port) != info.get("source_port"):
            return False
        if self.destination_port is not None and int(self.destination_port) != info.get("destination_port"):
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": self.action,
            "source_ip": self.source_ip,
            "destination_ip": self.destination_ip,
            "protocol": self.protocol,
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "remark": self.remark,
        }


@dataclass
class AccessList:
    name: str
    acl_type: str = "STANDARD"
    entries: List[AclEntry] = field(default_factory=list)
    attachments: List[Dict[str, str]] = field(default_factory=list)

    @property
    def extended(self) -> bool:
        return self.acl_type.upper() == "EXTENDED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.acl_type,
            "entries": [entry.to_dict() for entry in self.entries],
            "attachments": [dict(item) for item in self.attachments],
        }


class AccessListManager:
    """Standard and extended ACLs attachable to simulated interfaces."""

    def __init__(self) -> None:
        self.acls: Dict[str, AccessList] = {}
        self.matches = 0
        self.denied = 0

    def create(self, name: str, acl_type: str = "STANDARD") -> AccessList:
        key = str(name).strip()
        if not key:
            raise ValueError("ACL name cannot be empty")
        normalized = str(acl_type).upper()
        if normalized not in {"STANDARD", "EXTENDED"}:
            raise ValueError("ACL type must be STANDARD or EXTENDED")
        acl = AccessList(name=key, acl_type=normalized)
        self.acls[key] = acl
        return acl

    def require(self, name: str) -> AccessList:
        acl = self.acls.get(str(name))
        if acl is None:
            raise ValueError(f"Unknown access list: {name}")
        return acl

    def add_entry(self, name: str, **kwargs: Any) -> AclEntry:
        acl = self.require(name)
        action = str(kwargs.pop("action", "DENY")).upper()
        if action not in {"ALLOW", "PERMIT", "DENY"}:
            raise ValueError("ACL action must be permit/allow or deny")
        if action in {"ALLOW", "PERMIT"}:
            action = "ALLOW"
        entry = AclEntry(
            sequence=acl.entries[-1].sequence + 1 if acl.entries else 10,
            action=action,
            source_ip=str(kwargs.pop("source_ip", "any") or "any"),
            destination_ip=str(kwargs.pop("destination_ip", "any") or "any"),
            protocol=(str(kwargs["protocol"]).upper() if kwargs.get("protocol") else None),
            source_port=(int(kwargs["source_port"]) if kwargs.get("source_port") else None),
            destination_port=(
                int(kwargs["destination_port"]) if kwargs.get("destination_port") else None
            ),
            remark=str(kwargs.pop("remark", "")),
        )
        acl.entries.append(entry)
        return entry

    def attach(self, name: str, device: str, interface_id: str = "eth0") -> AccessList:
        acl = self.require(name)
        attachment = {"device": str(device), "interface_id": str(interface_id)}
        if attachment not in acl.attachments:
            acl.attachments.append(attachment)
        return acl

    def detach(self, name: str, device: str, interface_id: str = "eth0") -> AccessList:
        acl = self.require(name)
        acl.attachments = [
            item
            for item in acl.attachments
            if not (item["device"] == str(device) and item["interface_id"] == str(interface_id))
        ]
        return acl

    def remove(self, name: str) -> bool:
        return self.acls.pop(str(name), None) is not None

    def clear(self) -> None:
        self.acls.clear()
        self.matches = 0
        self.denied = 0

    def attached_to_path(self, path: List[str]) -> List[AccessList]:
        devices = set(path or [])
        attached: List[AccessList] = []
        for acl in self.acls.values():
            if any(item["device"] in devices for item in acl.attachments):
                attached.append(acl)
        return attached

    def evaluate(
        self,
        info: Dict[str, Any],
        path: Optional[List[str]] = None,
    ) -> Tuple[Optional[AccessList], Optional[AclEntry]]:
        for acl in self.attached_to_path(list(path or [])):
            for entry in acl.entries:
                if entry.matches(info, acl.extended):
                    return acl, entry
        return None, None

    def to_dict(self) -> List[Dict[str, Any]]:
        return [acl.to_dict() for acl in self.acls.values()]


# --------------------------------------------------------------------------
# ARP security (simulated only)
# --------------------------------------------------------------------------


@dataclass
class ArpSecurityEntry:
    device: str
    ip_address: str
    mac_address: str
    state: str = "LEGITIMATE"
    known_mac: Optional[str] = None
    attacker: Optional[str] = None
    victim: Optional[str] = None
    gateway: Optional[str] = None
    timestamp: float = 0.0
    severity: str = "INFO"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device": self.device,
            "ip_address": self.ip_address,
            "mac_address": self.mac_address,
            "state": self.state,
            "known_mac": self.known_mac,
            "attacker": self.attacker,
            "victim": self.victim,
            "gateway": self.gateway,
            "timestamp": self.timestamp,
            "severity": self.severity,
        }


# --------------------------------------------------------------------------
# Flood simulation (inside the simulator only)
# --------------------------------------------------------------------------


@dataclass
class FloodScenario:
    scenario_id: int
    attacker: str
    target: str
    protocol: str
    rate: float
    duration: float
    threshold: float
    started_at: float
    protection: bool = False
    detected: bool = False
    detected_at: Optional[float] = None
    peak_rate: float = 0.0
    packets_generated: int = 0
    packets_delivered: int = 0
    packets_dropped: int = 0
    bytes: int = 0
    queue_growth: float = 0.0
    bandwidth: float = 0.0
    latency_increase: float = 0.0
    baseline_latency: float = 0.0
    average_latency: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "attacker": self.attacker,
            "target": self.target,
            "protocol": self.protocol,
            "rate": self.rate,
            "duration": self.duration,
            "threshold": self.threshold,
            "started_at": self.started_at,
            "protection": self.protection,
            "detected": self.detected,
            "detected_at": self.detected_at,
            "peak_rate": self.peak_rate,
            "packets_generated": self.packets_generated,
            "packets_received": self.packets_delivered,
            "packets_delivered": self.packets_delivered,
            "packets_dropped": self.packets_dropped,
            "bytes": self.bytes,
            "queue_growth": self.queue_growth,
            "bandwidth": self.bandwidth,
            "latency_increase": self.latency_increase,
            "baseline_latency": self.baseline_latency,
            "average_latency": self.average_latency,
        }


# --------------------------------------------------------------------------
# The layer attached to the simulator
# --------------------------------------------------------------------------


class ServiceSecurityLayer:
    """Services plus simulated security, layered on one NetworkSimulator."""

    def __init__(self, simulator: Any) -> None:
        self.simulator = simulator
        self.registry = ServiceRegistry(self)
        self.firewall = Firewall()
        self.acls = AccessListManager()
        self.service_metrics = ServiceMetrics()
        self.security_metrics = SecurityMetrics()

        # DHCP
        self.leases: Dict[str, DhcpLease] = {}
        self.dhcp_clients: Dict[str, Dict[str, Any]] = {}

        # DNS
        self.dns_zones: Dict[str, Dict[str, DnsRecord]] = {}
        self.dns_caches: Dict[str, Dict[str, DnsCacheEntry]] = {}

        # HTTP / FTP / SMTP in-memory stores (never touch the real filesystem)
        self.http_resources: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.ftp_files: Dict[str, Dict[str, str]] = {}
        self.smtp_mailboxes: Dict[str, List[Dict[str, Any]]] = {}
        self.smtp_domain: Dict[str, str] = {}

        # ARP security
        self.arp_detection = True
        self.arp_protection = False
        self.arp_entries: List[ArpSecurityEntry] = []
        self.arp_conflicts: List[Dict[str, Any]] = []
        self.arp_bindings: Dict[str, Dict[str, List[str]]] = {}
        self.spoof_attempts = 0
        self.poisoned_entries = 0

        # Flood protection
        self.flood_threshold = 100.0
        self.flood_protection = False
        self.floods: List[FloodScenario] = []
        self.detected_floods = 0
        self._flood_counter = 0

        self._pumping = False
        self._ephemeral = 49152
        self.recent_exchanges: List[Dict[str, Any]] = []
        self.last_block_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    @property
    def now(self) -> float:
        return float(self.simulator.time)

    def log(
        self,
        event: EventType,
        message: str,
        component: Optional[str] = None,
        **details: Any,
    ) -> None:
        self.simulator._log_event(event, message, component=component, **details)

    def device(self, name: str) -> Device:
        device = self.simulator.topology.get_device(str(name))
        if device is None:
            raise ValueError(f"Unknown device: {name}")
        return device

    def device_ip(self, name: str) -> Optional[str]:
        device = self.simulator.topology.get_device(str(name))
        if device is None:
            return None
        for interface in device.interfaces:
            if interface.ip_address:
                return interface.ip_address
        return None

    def _next_ephemeral_port(self) -> int:
        self._ephemeral += 1
        if self._ephemeral > 65000:
            self._ephemeral = 49152
        return self._ephemeral

    def _pump(self) -> None:
        """Process queued packets through the real routing/QoS path."""

        if self._pumping:
            return
        self._pumping = True
        try:
            self.simulator.run_until_empty()
        finally:
            self._pumping = False

    def _network_packet(self, transport_packet: TransportPacket):
        for packet in reversed(self.simulator.packets):
            if packet.packet_id == transport_packet.network_packet_id:
                return packet
        return None

    def _annotate(self, transport_packet: TransportPacket, info: Dict[str, Any]) -> None:
        transport_packet.service = dict(info)
        network_packet = self._network_packet(transport_packet)
        if network_packet is not None:
            network_packet.service = dict(info)
            network_packet.service_name = info.get("service")

    def _exchange(self, transport_packet: TransportPacket) -> Dict[str, Any]:
        network_packet = self._network_packet(transport_packet)
        record = {
            "packet_id": transport_packet.packet_id,
            "network_packet_id": transport_packet.network_packet_id,
            "protocol": transport_packet.protocol,
            "source": transport_packet.source,
            "destination": transport_packet.destination,
            "source_port": transport_packet.source_port,
            "destination_port": transport_packet.destination_port,
            "traffic_class": transport_packet.traffic_class,
            "status": transport_packet.status,
            "delivered": transport_packet.status == "DELIVERED",
            "route": list(transport_packet.route),
            "latency": transport_packet.latency,
            "drop_reason": transport_packet.drop_reason,
            "service": dict(getattr(transport_packet, "service", {}) or {}),
        }
        if network_packet is not None:
            record["security"] = dict(getattr(network_packet, "security", {}) or {})
        self.recent_exchanges.append(record)
        del self.recent_exchanges[:-200]
        return record

    # ------------------------------------------------------------------
    # Service availability
    # ------------------------------------------------------------------
    def service_ready(self, instance: ServiceInstance) -> Tuple[bool, str]:
        """Return ``(ready, reason)`` for a hosted service."""

        if instance.state != "RUNNING":
            return False, "SERVICE_STOPPED"
        device = self.device(instance.device)
        if device.status != "UP":
            return False, "DEVICE_DOWN"
        try:
            interface = device.get_interface(instance.interface_id)
        except KeyError:
            interface = device.interfaces[0]
        if interface.status != "UP":
            return False, "INTERFACE_DOWN"
        return True, ""

    def find_service(self, name: str, device: Optional[str] = None) -> ServiceInstance:
        instance = self.registry.find(name, device)
        if instance is None:
            if device:
                raise ValueError(f"No {normalise_service(name)} service on {device}")
            raise ValueError(f"No running {normalise_service(name)} service is installed")
        return instance

    def ensure_defaults(self, instance: ServiceInstance) -> None:
        """Fill in sensible per-service defaults derived from the device."""

        config = instance.config
        if instance.name == "DHCP":
            address = self.device_ip(instance.device) or "192.168.1.10"
            network = ipaddress.IPv4Network(
                f"{address}/24", strict=False
            )
            config.setdefault("pool_start", str(network.network_address + 100))
            config.setdefault("pool_end", str(network.network_address + 150))
            config.setdefault("prefix", 24)
            config.setdefault("mask", str(network.netmask))
            config.setdefault("gateway", address)
            config.setdefault("dns", address)
            config.setdefault("lease_time", 3600.0)
        elif instance.name == "DNS":
            config.setdefault("default_ttl", 300.0)
        elif instance.name == "HTTP":
            self.http_resources.setdefault(
                instance.device,
                {
                    "/": {"status": 200, "body": "NetAdapt simulated index"},
                    "/index.html": {"status": 200, "body": "NetAdapt simulated index"},
                    "/about": {"status": 200, "body": "Simulated HTTP service running inside NetAdapt"},
                },
            )
            config.setdefault("fault", "none")
        elif instance.name == "FTP":
            self.ftp_files.setdefault(
                instance.device,
                {
                    "readme.txt": "NetAdapt simulated FTP file store. Nothing touches the real disk.",
                    "notes.txt": "Stage 10 FTP transfer sample content.",
                },
            )
        elif instance.name == "SMTP":
            self.smtp_mailboxes.setdefault(instance.device, [])
            self.smtp_domain.setdefault(instance.device, f"{instance.device.lower()}.netadapt.local")
            config.setdefault("domain", self.smtp_domain[instance.device])

    def service_status(self, device: str) -> List[Dict[str, Any]]:
        """Human/service readable status list used by ``show services``."""

        rows = []
        for instance in self.registry.list(device):
            ready, reason = self.service_ready(instance)
            rows.append(
                {
                    **instance.to_dict(),
                    "available": ready,
                    "unavailable_reason": reason,
                    "ip": self.device_ip(instance.device),
                }
            )
        return rows

    # ------------------------------------------------------------------
    # DHCP
    # ------------------------------------------------------------------
    def _pool(self, instance: ServiceInstance) -> List[str]:
        start = ipaddress.IPv4Address(str(instance.config["pool_start"]))
        end = ipaddress.IPv4Address(str(instance.config["pool_end"]))
        if int(end) < int(start):
            start, end = end, start
        limit = int(start) + max(0, int(instance.config.get("pool_size", 51)))
        return [
            str(ipaddress.IPv4Address(value))
            for value in range(int(start), min(int(end), limit - 1) + 1)
        ]

    def available_addresses(self, server: str) -> List[str]:
        instance = self.registry.require("DHCP", server)
        leased = {lease.address for lease in self.leases.values() if lease.state == "ACTIVE"}
        return [address for address in self._pool(instance) if address not in leased]

    def _dhcp_message(
        self,
        source: str,
        destination: str,
        source_port: int,
        destination_port: int,
        message: str,
        detail: Dict[str, Any],
        size: int = 300,
    ) -> Dict[str, Any]:
        flow = self.simulator.create_udp_flow(
            source, destination, source_port, destination_port, size, "Emergency"
        )
        packet = self.simulator.send_udp_data(flow.flow_id, 1, size)[0]
        self._annotate(packet, {"service": "DHCP", "message": message, **detail})
        self._pump()
        record = self._exchange(packet)
        record["message"] = message
        return record

    def dhcp_acquire(self, client: str, server: Optional[str] = None) -> Dict[str, Any]:
        """Run DISCOVER -> OFFER -> REQUEST -> ACK and configure the client."""

        client_device = self.device(client)
        instance = self.find_service("DHCP", server)
        server = instance.device
        result: Dict[str, Any] = {
            "success": False,
            "client": client,
            "server": server,
            "service": "DHCP",
            "steps": [],
            "packets": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            result["reason"] = reason
            instance.failures += 1
            self.service_metrics.increment("DHCP", "failed_requests")
            self.log(
                EventType.SERVICE_FAILED,
                f"DHCP unavailable on {server}: {reason}",
                component=server,
                service="DHCP",
                client=client,
                reason=reason,
            )
            return result

        self.service_metrics.increment("DHCP", "discover")
        discover = self._dhcp_message(
            client, server, SERVICE_PORTS["DHCP_CLIENT"], SERVICE_PORTS["DHCP"],
            "DHCP DISCOVER", {"type": "DISCOVER", "client": client},
        )
        self.log(
            EventType.DHCP_DISCOVER,
            f"{client} broadcasts DHCP DISCOVER",
            component=client,
            client=client,
            server=server,
            delivered=discover["delivered"],
            packet_id=discover["network_packet_id"],
        )
        result["steps"].append("DISCOVER")
        result["packets"].append(discover)
        if not discover["delivered"]:
            result["reason"] = discover["drop_reason"] or "NO_RESPONSE"
            self.service_metrics.increment("DHCP", "failed_requests")
            return result

        pool = self._pool(instance)
        leased = {lease.address for lease in self.leases.values() if lease.state == "ACTIVE"}
        free = [address for address in pool if address not in leased]
        if not free:
            nak = self._dhcp_message(
                server, client, SERVICE_PORTS["DHCP"], SERVICE_PORTS["DHCP_CLIENT"],
                "DHCP NAK", {"type": "NAK", "client": client, "reason": "POOL_EXHAUSTED"},
            )
            self.log(
                EventType.DHCP_NAK,
                f"DHCP pool exhausted on {server}; NAK sent to {client}",
                component=server,
                client=client,
                reason="POOL_EXHAUSTED",
                packet_id=nak["network_packet_id"],
            )
            result["steps"].append("NAK")
            result["packets"].append(nak)
            result["reason"] = "POOL_EXHAUSTED"
            instance.failures += 1
            self.service_metrics.increment("DHCP", "failed_requests")
            return result

        current = self.leases.get(client)
        if current is not None and current.state == "ACTIVE" and current.address in free:
            offered = current.address
        else:
            offered = free[0]
        self.service_metrics.increment("DHCP", "offers")
        offer = self._dhcp_message(
            server, client, SERVICE_PORTS["DHCP"], SERVICE_PORTS["DHCP_CLIENT"],
            "DHCP OFFER", {"type": "OFFER", "client": client, "offered_ip": offered},
        )
        self.log(
            EventType.DHCP_OFFER,
            f"DHCP OFFER {offered} to {client}",
            component=server,
            client=client,
            offered_ip=offered,
            packet_id=offer["network_packet_id"],
        )
        result["steps"].append("OFFER")
        result["packets"].append(offer)
        if not offer["delivered"]:
            result["reason"] = offer["drop_reason"] or "NO_RESPONSE"
            self.service_metrics.increment("DHCP", "failed_requests")
            return result

        self.service_metrics.increment("DHCP", "requests")
        request = self._dhcp_message(
            client, server, SERVICE_PORTS["DHCP_CLIENT"], SERVICE_PORTS["DHCP"],
            "DHCP REQUEST", {"type": "REQUEST", "client": client, "requested_ip": offered},
        )
        self.log(
            EventType.DHCP_REQUEST,
            f"{client} requests DHCP lease {offered}",
            component=client,
            client=client,
            requested_ip=offered,
            packet_id=request["network_packet_id"],
        )
        result["steps"].append("REQUEST")
        result["packets"].append(request)
        if not request["delivered"]:
            result["reason"] = request["drop_reason"] or "NO_RESPONSE"
            self.service_metrics.increment("DHCP", "failed_requests")
            return result

        return self._dhcp_grant(instance, client, offered, result)

    def _dhcp_grant(
        self,
        instance: ServiceInstance,
        client: str,
        address: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = instance.config
        lease_time = float(config.get("lease_time", 3600.0))
        prefix = int(config.get("prefix", 24))
        device = self.device(client)
        interface = device.interfaces[0]
        interface.assign_ip(address, prefix)
        device.default_gateway = str(config.get("gateway") or self.device_ip(instance.device))
        lease = DhcpLease(
            client=client,
            address=address,
            mask=str(config.get("mask", "255.255.255.0")),
            prefix=prefix,
            gateway=device.default_gateway,
            dns=str(config.get("dns") or ""),
            lease_time=lease_time,
            granted_at=self.now,
            expires_at=self.now + lease_time,
            server=instance.device,
        )
        self.leases[client] = lease
        self.dhcp_clients[client] = {
            "address": address,
            "prefix": prefix,
            "mask": lease.mask,
            "gateway": lease.gateway,
            "dns_servers": [lease.dns] if lease.dns else [],
            "server": instance.device,
            "lease_time": lease_time,
            "expires_at": lease.expires_at,
        }
        ack = self._dhcp_message(
            instance.device, client, SERVICE_PORTS["DHCP"], SERVICE_PORTS["DHCP_CLIENT"],
            "DHCP ACK", {"type": "ACK", "client": client, "assigned_ip": address},
        )
        self.log(
            EventType.DHCP_ACK,
            f"DHCP ACK {address} to {client}",
            component=instance.device,
            client=client,
            assigned_ip=address,
            subnet_mask=lease.mask,
            gateway=lease.gateway,
            lease_time=lease_time,
            packet_id=ack["network_packet_id"],
        )
        result["steps"].append("ACK")
        result["packets"].append(ack)
        if not ack["delivered"]:
            result["reason"] = ack["drop_reason"] or "NO_RESPONSE"
            self.service_metrics.increment("DHCP", "failed_requests")
            return result
        self.service_metrics.increment("DHCP", "leases_issued")
        instance.requests += 1
        result.update(
            {
                "success": True,
                "address": address,
                "ip_address": address,
                "subnet_mask": lease.mask,
                "prefix": prefix,
                "gateway": lease.gateway,
                "dns": lease.dns,
                "lease_time": lease_time,
                "expires_at": lease.expires_at,
            }
        )
        return result

    def dhcp_renew(self, client: str, server: Optional[str] = None) -> Dict[str, Any]:
        """Renew an existing lease; a client without a lease performs a full acquire."""

        if client not in self.leases or self.leases[client].state != "ACTIVE":
            result = self.dhcp_acquire(client, server)
            result["renewed"] = False
            return result
        lease = self.leases[client]
        instance = self.find_service("DHCP", server or lease.server)
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("DHCP", "failed_requests")
            return {
                "success": False,
                "client": client,
                "server": instance.device,
                "service": "DHCP",
                "reason": reason,
                "steps": [],
                "packets": [],
            }
        result: Dict[str, Any] = {
            "success": False,
            "client": client,
            "server": instance.device,
            "service": "DHCP",
            "steps": ["REQUEST"],
            "packets": [],
            "renewed": True,
        }
        self.service_metrics.increment("DHCP", "requests")
        request = self._dhcp_message(
            client, instance.device, SERVICE_PORTS["DHCP_CLIENT"], SERVICE_PORTS["DHCP"],
            "DHCP REQUEST", {"type": "REQUEST", "client": client, "requested_ip": lease.address, "renewal": True},
        )
        self.log(
            EventType.DHCP_REQUEST,
            f"{client} renews DHCP lease {lease.address}",
            component=client,
            client=client,
            requested_ip=lease.address,
            renewal=True,
            packet_id=request["network_packet_id"],
        )
        result["packets"].append(request)
        if not request["delivered"]:
            result["reason"] = request["drop_reason"] or "NO_RESPONSE"
            self.service_metrics.increment("DHCP", "failed_requests")
            return result
        return self._dhcp_grant(instance, client, lease.address, result)

    def dhcp_release(self, client: str) -> Dict[str, Any]:
        """Release a lease and clear the client interface configuration."""

        lease = self.leases.get(client)
        if lease is None or lease.state != "ACTIVE":
            return {
                "success": False,
                "client": client,
                "service": "DHCP",
                "reason": "NO_LEASE",
                "steps": [],
                "packets": [],
            }
        device = self.device(client)
        interface = device.interfaces[0]
        interface.ip_address = None
        interface.prefix = 24
        device.default_gateway = None
        result: Dict[str, Any] = {
            "success": False,
            "client": client,
            "server": lease.server,
            "service": "DHCP",
            "steps": ["RELEASE"],
            "packets": [],
        }
        release = self._dhcp_message(
            client, lease.server, SERVICE_PORTS["DHCP_CLIENT"], SERVICE_PORTS["DHCP"],
            "DHCP RELEASE", {"type": "RELEASE", "client": client, "address": lease.address},
        )
        self.log(
            EventType.DHCP_RELEASE,
            f"{client} releases DHCP lease {lease.address}",
            component=client,
            client=client,
            address=lease.address,
            packet_id=release["network_packet_id"],
        )
        result["packets"].append(release)
        lease.state = "RELEASED"
        self.leases.pop(client, None)
        self.dhcp_clients.pop(client, None)
        self.service_metrics.increment("DHCP", "leases_released")
        result["success"] = True
        result["reason"] = release["drop_reason"]
        return result

    def expire_leases(self, now: Optional[float] = None) -> List[str]:
        moment = self.now if now is None else float(now)
        expired = [
            client
            for client, lease in self.leases.items()
            if lease.state == "ACTIVE" and lease.expires_at < moment
        ]
        for client in expired:
            lease = self.leases[client]
            lease.state = "EXPIRED"
            self.log(
                EventType.DHCP_RELEASE,
                f"DHCP lease {lease.address} expired for {client}",
                component=client,
                client=client,
                address=lease.address,
                reason="LEASE_EXPIRED",
            )
        return expired

    def dhcp_state(self) -> Dict[str, Any]:
        servers = []
        for instance in self.registry.list():
            if instance.name != "DHCP":
                continue
            pool = self._pool(instance)
            servers.append(
                {
                    **instance.to_dict(),
                    "ip": self.device_ip(instance.device),
                    "pool_start": instance.config.get("pool_start"),
                    "pool_end": instance.config.get("pool_end"),
                    "pool_size": len(pool),
                    "available": self.available_addresses(instance.device),
                    "leased": [
                        lease.to_dict()
                        for lease in self.leases.values()
                        if lease.state == "ACTIVE" and lease.server == instance.device
                    ],
                    "subnet_mask": instance.config.get("mask"),
                    "gateway": instance.config.get("gateway"),
                    "dns": instance.config.get("dns"),
                    "lease_time": instance.config.get("lease_time"),
                }
            )
        return {
            "servers": servers,
            "leases": [lease.to_dict() for lease in self.leases.values()],
            "clients": [
                {"client": client, **config}
                for client, config in self.dhcp_clients.items()
            ],
            "ports": {"server": SERVICE_PORTS["DHCP"], "client": SERVICE_PORTS["DHCP_CLIENT"]},
        }

    # ------------------------------------------------------------------
    # DNS
    # ------------------------------------------------------------------
    def dns_add_record(
        self,
        server: str,
        hostname: str,
        address: str,
        ttl: float = 300.0,
    ) -> DnsRecord:
        instance = self.find_service("DNS", server)
        record = DnsRecord(
            hostname=str(hostname).lower(),
            address=str(ipaddress.IPv4Address(address)),
            ttl=float(ttl),
        )
        self.dns_zones.setdefault(instance.device, {})[record.hostname] = record
        self.log(
            EventType.DNS_QUERY,
            f"DNS A record {record.hostname} -> {record.address} added to {instance.device}",
            component=instance.device,
            hostname=record.hostname,
            address=record.address,
            ttl=record.ttl,
            action="ZONE_UPDATE",
        )
        return record

    def dns_delete_record(self, server: str, hostname: str) -> bool:
        instance = self.find_service("DNS", server)
        return self.dns_zones.get(instance.device, {}).pop(str(hostname).lower(), None) is not None

    def dns_flush(self, client: Optional[str] = None) -> int:
        if client is None:
            count = sum(len(entries) for entries in self.dns_caches.values())
            self.dns_caches.clear()
            return count
        entries = self.dns_caches.pop(str(client), {})
        return len(entries)

    def expire_dns_cache(self, now: Optional[float] = None) -> int:
        moment = self.now if now is None else float(now)
        removed = 0
        for entries in self.dns_caches.values():
            for hostname in [key for key, entry in entries.items() if entry.expires_at < moment]:
                del entries[hostname]
                removed += 1
        return removed

    def dns_query(
        self,
        client: str,
        hostname: str,
        server: Optional[str] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Resolve ``hostname`` from a client using real simulated UDP traffic."""

        self.device(client)
        instance = self.find_service("DNS", server)
        server_device = instance.device
        name = str(hostname).lower()
        result: Dict[str, Any] = {
            "service": "DNS",
            "client": client,
            "server": server_device,
            "hostname": name,
            "packets": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("DNS", "failures")
            self.log(
                EventType.SERVICE_FAILED,
                f"DNS unavailable on {server_device}: {reason}",
                component=server_device,
                service="DNS",
                client=client,
                reason=reason,
            )
            result.update({"success": False, "reason": reason})
            return result

        cache = self.dns_caches.setdefault(client, {})
        self.expire_dns_cache()
        entry = cache.get(name) if use_cache else None
        if entry is not None:
            entry.hits += 1
            self.service_metrics.increment("DNS", "cache_hits")
            self.service_metrics.increment("DNS", "responses")
            self.log(
                EventType.DNS_CACHE_HIT,
                f"DNS cache hit on {client} for {name}",
                component=client,
                client=client,
                hostname=name,
                address=entry.address,
                expires_at=entry.expires_at,
            )
            result.update(
                {
                    "success": True,
                    "address": entry.address,
                    "source": "CACHE",
                    "cache_hit": True,
                    "latency": 0.0,
                }
            )
            return result

        self.service_metrics.increment("DNS", "cache_misses")
        self.log(
            EventType.DNS_CACHE_MISS,
            f"DNS cache miss on {client} for {name}",
            component=client,
            client=client,
            hostname=name,
        )
        self.service_metrics.increment("DNS", "queries")
        query = self._dns_message(
            client, server_device, self._next_ephemeral_port(), SERVICE_PORTS["DNS"],
            f"DNS QUERY {name}", {"type": "QUERY", "query": name, "client": client},
            size=120,
        )
        self.log(
            EventType.DNS_QUERY,
            f"{client} queried {name} on {server_device}",
            component=client,
            client=client,
            hostname=name,
            server=server_device,
            packet_id=query["network_packet_id"],
            delivered=query["delivered"],
        )
        result["packets"].append(query)
        if not query["delivered"]:
            self.service_metrics.increment("DNS", "failures")
            self.log(
                EventType.DNS_RESPONSE,
                f"DNS query for {name} failed: {query['drop_reason']}",
                component=client,
                client=client,
                hostname=name,
                success=False,
                reason=query["drop_reason"],
            )
            result.update({"success": False, "reason": query["drop_reason"] or "NO_RESPONSE"})
            return result

        record = self.dns_zones.get(server_device, {}).get(name)
        if record is None:
            self.service_metrics.increment("DNS", "failures")
            self.log(
                EventType.DNS_RESPONSE,
                f"DNS NXDOMAIN for {name} on {server_device}",
                component=server_device,
                hostname=name,
                client=client,
                success=False,
                rcode="NXDOMAIN",
            )
            result.update({"success": False, "reason": "NXDOMAIN"})
            return result

        response = self._dns_message(
            server_device, client, SERVICE_PORTS["DNS"], self._next_ephemeral_port(),
            f"DNS RESPONSE {name} {record.address}",
            {"type": "RESPONSE", "query": name, "response": record.address, "ttl": record.ttl},
            size=160,
        )
        cache[name] = DnsCacheEntry(name, record.address, self.now + record.ttl)
        self.service_metrics.increment("DNS", "responses")
        self.service_metrics.record_latency(
            "DNS", (query["latency"] or 0.0) + (response["latency"] or 0.0)
        )
        self.log(
            EventType.DNS_RESPONSE,
            f"DNS response {name} -> {record.address} for {client}",
            component=server_device,
            client=client,
            hostname=name,
            address=record.address,
            ttl=record.ttl,
            packet_id=response["network_packet_id"],
            delivered=response["delivered"],
        )
        result["packets"].append(response)
        result.update(
            {
                "success": bool(response["delivered"]),
                "address": record.address,
                "ttl": record.ttl,
                "cache_hit": False,
                "source": "SERVER",
                "latency": (query["latency"] or 0.0) + (response["latency"] or 0.0),
            }
        )
        if not response["delivered"]:
            self.service_metrics.increment("DNS", "failures")
            result["reason"] = response["drop_reason"]
        instance.requests += 1
        return result

    def _dns_message(
        self,
        source: str,
        destination: str,
        source_port: int,
        destination_port: int,
        message: str,
        detail: Dict[str, Any],
        size: int = 120,
    ) -> Dict[str, Any]:
        flow = self.simulator.create_udp_flow(
            source, destination, source_port, destination_port, size, "Emergency"
        )
        packet = self.simulator.send_udp_data(flow.flow_id, 1, size)[0]
        self._annotate(packet, {"service": "DNS", "message": message, **detail})
        self._pump()
        return self._exchange(packet)

    def dns_state(self) -> Dict[str, Any]:
        return {
            "servers": [
                instance.to_dict()
                for instance in self.registry.list()
                if instance.name == "DNS"
            ],
            "records": [
                {"server": device, **record.to_dict()}
                for device, zone in self.dns_zones.items()
                for record in zone.values()
            ],
            "caches": [
                {"client": client, "entries": [entry.to_dict() for entry in entries.values()]}
                for client, entries in self.dns_caches.items()
            ],
        }

    # ------------------------------------------------------------------
    # TCP based services (HTTP / FTP / SMTP)
    # ------------------------------------------------------------------
    def _tcp_open(
        self,
        source: str,
        destination: str,
        destination_port: int,
        traffic_class: str,
        payload_size: int = 256,
    ) -> Optional[Any]:
        connection = self.simulator.create_tcp_connection(
            source,
            destination,
            self._next_ephemeral_port(),
            int(destination_port),
            1,
            8,
            16.0,
            0.1,
            traffic_class,
        )
        self._pump()
        if connection.state != TCPState.ESTABLISHED:
            return None
        return connection

    def _tcp_exchange(
        self,
        connection: Any,
        request_size: int,
        response_size: int,
        request_info: Dict[str, Any],
        response_info: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        sent = self.simulator.send_tcp_data(
            connection.flow_id, 1, max(1, int(request_size))
        )
        if not sent:
            empty = {
                "packet_id": None,
                "network_packet_id": None,
                "protocol": "TCP",
                "source": connection.source,
                "destination": connection.destination,
                "source_port": connection.source_port,
                "destination_port": connection.destination_port,
                "traffic_class": connection.traffic_class,
                "status": "DROPPED",
                "delivered": False,
                "route": list(connection.route),
                "latency": None,
                "drop_reason": "WINDOW_EXHAUSTED",
                "service": dict(request_info),
            }
            return empty, dict(empty)
        request = sent[0]
        self._annotate(request, request_info)
        self._pump()
        request_record = self._exchange(request)
        if not request_record["delivered"]:
            return request_record, {"delivered": False, "status": "DROPPED", "latency": None,
                                    "drop_reason": request_record["drop_reason"]}
        response = self.simulator.send_tcp_response(
            connection.flow_id, max(1, int(response_size))
        )
        self._annotate(response, response_info)
        self._pump()
        return request_record, self._exchange(response)

    def _close_tcp(self, connection: Any) -> None:
        try:
            if connection is not None and connection.state == TCPState.ESTABLISHED:
                self.simulator.close_tcp_connection(connection.flow_id)
                self._pump()
        except ValueError:
            return

    def _transport_failure(
        self,
        service: str,
        client: str,
        server: str,
        reason: str,
        packets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        instance = self.registry.get(service, server)
        if instance is not None:
            instance.failures += 1
        self.service_metrics.increment(service, "failures")
        self.log(
            EventType.SERVICE_FAILED,
            f"{service} request from {client} to {server} failed: {reason}",
            component=server,
            service=service,
            client=client,
            server=server,
            reason=reason,
        )
        return {
            "service": service,
            "client": client,
            "server": server,
            "success": False,
            "reason": reason,
            "packets": packets,
        }

    # -- HTTP ----------------------------------------------------------
    def http_add_resource(
        self,
        server: str,
        path: str,
        body: str = "",
        status: int = 200,
    ) -> Dict[str, Any]:
        instance = self.find_service("HTTP", server)
        resource = {"status": int(status), "body": str(body)}
        self.http_resources.setdefault(instance.device, {})[str(path)] = resource
        return resource

    def http_request(
        self,
        client: str,
        server: Optional[str] = None,
        method: str = "GET",
        path: str = "/",
        body: Optional[str] = None,
    ) -> Dict[str, Any]:
        instance = self.find_service("HTTP", server)
        server_device = instance.device
        self.device(client)
        self.ensure_defaults(instance)
        result: Dict[str, Any] = {
            "service": "HTTP",
            "client": client,
            "server": server_device,
            "method": str(method).upper(),
            "path": path,
            "packets": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("HTTP", "failed")
            return self._transport_failure("HTTP", client, server_device, reason, [])

        self.service_metrics.increment("HTTP", "requests")
        instance.requests += 1
        request_line = f"{str(method).upper()} {path}"
        connection = self._tcp_open(client, server_device, instance.port, "HTTP", 256)
        if connection is None:
            self.service_metrics.increment("HTTP", "failed")
            return self._transport_failure(
                "HTTP", client, server_device, "CONNECTION_FAILED", []
            )
        request, response = self._tcp_exchange(
            connection,
            320,
            640,
            {
                "service": "HTTP",
                "message": request_line,
                "method": str(method).upper(),
                "path": path,
                "request": request_line,
                "body": body,
            },
            {"service": "HTTP", "message": "HTTP RESPONSE", "path": path},
        )
        result["packets"] = [request, response]
        self._close_tcp(connection)

        resources = self.http_resources.get(server_device, {})
        resource = resources.get(path)
        fault = str(instance.config.get("fault", "none")).lower()
        if fault == "500":
            status, payload, reason = 500, "Internal Server Error (simulated fault)", "SERVER_FAULT"
        elif str(method).upper() not in {"GET", "POST"}:
            status, payload, reason = 400, "Bad Request", "BAD_REQUEST"
        elif not str(path).startswith("/"):
            status, payload, reason = 400, "Bad Request", "BAD_REQUEST"
        elif resource is None:
            status, payload, reason = 404, "Not Found", "NOT_FOUND"
        else:
            status, payload, reason = int(resource["status"]), str(resource["body"]), ""
        if body is not None and str(method).upper() == "POST" and status == 200:
            payload = f"{payload} (stored {len(str(body))} bytes)"

        self.service_metrics.increment("HTTP", "responses")
        if not request["delivered"]:
            self.service_metrics.increment("HTTP", "failed")
            return self._transport_failure(
                "HTTP",
                client,
                server_device,
                request["drop_reason"] or "REQUEST_DROPPED",
                result["packets"],
            )
        latency = (request["latency"] or 0.0) + (response["latency"] or 0.0)
        self.service_metrics.record_latency("HTTP", latency)
        self.service_metrics.increment("HTTP", "bytes_transferred", 960)
        if 200 <= status < 300:
            self.service_metrics.increment("HTTP", "successful")
        else:
            self.service_metrics.increment("HTTP", "failed")
            instance.failures += 1

        self.log(
            EventType.HTTP_REQUEST,
            f"HTTP {request_line} from {client} to {server_device}",
            component=client,
            client=client,
            server=server_device,
            method=str(method).upper(),
            path=path,
            packet_id=request["network_packet_id"],
        )
        self.log(
            EventType.HTTP_RESPONSE,
            f"HTTP {status} {payload} for {path} ({client})",
            component=server_device,
            client=client,
            server=server_device,
            path=path,
            status=status,
            body=payload,
            latency=latency,
            packet_id=response.get("network_packet_id"),
        )
        result.update(
            {
                "success": 200 <= status < 300,
                "status": status,
                "status_text": payload,
                "body": payload,
                "latency": latency,
                "reason": reason,
                "request": request_line,
                "response": f"HTTP/1.1 {status} {payload}",
            }
        )
        return result

    def http_state(self) -> Dict[str, Any]:
        return {
            "servers": [
                instance.to_dict()
                for instance in self.registry.list()
                if instance.name == "HTTP"
            ],
            "resources": {
                device: {path: dict(item) for path, item in paths.items()}
                for device, paths in self.http_resources.items()
            },
        }

    # -- FTP -----------------------------------------------------------
    def ftp_connect(self, client: str, server: Optional[str] = None) -> Dict[str, Any]:
        instance = self.find_service("FTP", server)
        server_device = instance.device
        self.device(client)
        self.ensure_defaults(instance)
        result: Dict[str, Any] = {
            "service": "FTP",
            "client": client,
            "server": server_device,
            "packets": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("FTP", "failures")
            return self._transport_failure("FTP", client, server_device, reason, [])
        connection = self._tcp_open(client, server_device, instance.port, "FTP", 128)
        if connection is None:
            self.service_metrics.increment("FTP", "failures")
            return self._transport_failure(
                "FTP", client, server_device, "CONNECTION_FAILED", []
            )
        request, response = self._tcp_exchange(
            connection,
            128,
            256,
            {"service": "FTP", "message": "USER anonymous", "command": "USER"},
            {"service": "FTP", "message": "220 NetAdapt simulated FTP ready", "command": "BANNER"},
        )
        result["packets"] = [request, response]
        result.update(
            {
                "success": bool(request["delivered"] and response["delivered"]),
                "connection": connection.flow_id,
                "state": self.simulator.get_tcp_state(connection.flow_id),
                "port": instance.port,
                "banner": "220 NetAdapt simulated FTP ready",
                "reason": request["drop_reason"] or response["drop_reason"],
            }
        )
        self.service_metrics.increment("FTP", "connections")
        instance.requests += 1
        self.log(
            EventType.FTP_CONNECTION,
            f"FTP control connection {client} -> {server_device}:{instance.port}",
            component=server_device,
            client=client,
            server=server_device,
            port=instance.port,
            state=result["state"],
            connection_id=connection.flow_id,
        )
        return result

    def ftp_command(
        self,
        client: str,
        server: Optional[str] = None,
        command: str = "LIST",
        filename: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Dict[str, Any]:
        instance = self.find_service("FTP", server)
        server_device = instance.device
        self.device(client)
        self.ensure_defaults(instance)
        verb = str(command).upper()
        if verb not in {"LIST", "GET", "PUT"}:
            raise ValueError("FTP command must be LIST, GET, or PUT")
        result: Dict[str, Any] = {
            "service": "FTP",
            "client": client,
            "server": server_device,
            "command": verb,
            "filename": filename,
            "packets": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("FTP", "failures")
            return self._transport_failure("FTP", client, server_device, reason, [])

        store = self.ftp_files.setdefault(server_device, {})
        payload = content if content is not None else ""
        if verb == "PUT":
            if not filename:
                return self._transport_failure(
                    "FTP", client, server_device, "MISSING_FILENAME", []
                )
            payload = f"{filename}\n{payload}"
        elif verb == "GET":
            if filename not in store:
                self.service_metrics.increment("FTP", "failed")
                self.service_metrics.increment("FTP", "transfers")
                instance.failures += 1
                self.log(
                    EventType.FTP_TRANSFER,
                    f"FTP GET {filename} failed on {server_device}: file not found",
                    component=server_device,
                    client=client,
                    server=server_device,
                    command=verb,
                    filename=filename,
                    success=False,
                    reason="FILE_NOT_FOUND",
                )
                return {
                    **result,
                    "success": False,
                    "status": 550,
                    "response": f"550 {filename}: file not found",
                    "reason": "FILE_NOT_FOUND",
                }
            payload = store[str(filename)]
        else:
            payload = "\n".join(sorted(store)) or "(empty directory)"

        connection = self._tcp_open(client, server_device, instance.port, "FTP", 128)
        if connection is None:
            self.service_metrics.increment("FTP", "failures")
            return self._transport_failure(
                "FTP", client, server_device, "CONNECTION_FAILED", []
            )
        request_line = f"{verb} {filename}" if filename else verb
        started = self.now
        request, response = self._tcp_exchange(
            connection,
            max(128, len(str(payload).encode())),
            max(256, len(str(payload).encode())),
            {
                "service": "FTP",
                "message": request_line,
                "command": verb,
                "filename": filename,
                "bytes": len(str(payload).encode()),
            },
            {
                "service": "FTP",
                "message": f"150 {verb} transfer complete",
                "command": verb,
                "filename": filename,
                "bytes": len(str(payload).encode()),
            },
        )
        self._close_tcp(connection)
        result["packets"] = [request, response]
        transferred = len(str(payload).encode())
        if verb == "PUT" and request["delivered"] and response["delivered"]:
            store[str(filename)] = str(payload)
        success = bool(request["delivered"] and response["delivered"])
        elapsed = max(self.now - started, 0.0)
        self.service_metrics.increment("FTP", "transfers")
        self.service_metrics.increment("FTP", "bytes", transferred)
        self.service_metrics.record_latency("FTP", elapsed)
        if success:
            self.service_metrics.increment("FTP", "successful")
        else:
            self.service_metrics.increment("FTP", "failed")
            instance.failures += 1
        instance.requests += 1
        self.log(
            EventType.FTP_TRANSFER,
            f"FTP {request_line} {'ok' if success else 'failed'} ({transferred} bytes)",
            component=server_device,
            client=client,
            server=server_device,
            command=verb,
            filename=filename,
            bytes=transferred,
            success=success,
            transfer_time=elapsed,
            reason=request["drop_reason"] or response["drop_reason"],
        )
        result.update(
            {
                "success": success,
                "status": 226 if success else 550,
                "response": f"226 Transfer complete ({transferred} bytes)"
                if success
                else f"550 transfer failed: {request['drop_reason'] or response['drop_reason']}",
                "bytes": transferred,
                "transfer_time": elapsed,
                "listing": sorted(store) if verb == "LIST" else None,
                "reason": request["drop_reason"] or response["drop_reason"],
            }
        )
        return result

    def ftp_state(self) -> Dict[str, Any]:
        return {
            "servers": [
                instance.to_dict()
                for instance in self.registry.list()
                if instance.name == "FTP"
            ],
            "files": {
                device: {name: len(body) for name, body in files.items()}
                for device, files in self.ftp_files.items()
            },
        }

    # -- SMTP ----------------------------------------------------------
    def smtp_send(
        self,
        client: str,
        server: Optional[str] = None,
        sender: str = "student@netadapt.local",
        recipient: str = "server@netadapt.local",
        subject: str = "Stage 10 lab message",
        body: str = "Hello from the NetAdapt simulated SMTP service.",
    ) -> Dict[str, Any]:
        instance = self.find_service("SMTP", server)
        server_device = instance.device
        self.device(client)
        self.ensure_defaults(instance)
        domain = str(instance.config.get("domain") or self.smtp_domain.get(server_device, "netadapt.local"))
        result: Dict[str, Any] = {
            "service": "SMTP",
            "client": client,
            "server": server_device,
            "sender": sender,
            "recipient": recipient,
            "packets": [],
            "transcript": [],
        }
        ready, reason = self.service_ready(instance)
        if not ready:
            self.service_metrics.increment("SMTP", "failed")
            return self._transport_failure("SMTP", client, server_device, reason, [])
        self.service_metrics.increment("SMTP", "messages")
        connection = self._tcp_open(client, server_device, instance.port, "HTTP", 128)
        if connection is None:
            self.service_metrics.increment("SMTP", "failed")
            return self._transport_failure(
                "SMTP", client, server_device, "CONNECTION_FAILED", []
            )

        started = self.now
        commands = [
            ("HELO", f"HELO {client}", "250 {server_device}"),
            ("MAIL FROM", f"MAIL FROM:<{sender}>", "250 Sender ok"),
            ("RCPT TO", f"RCPT TO:<{recipient}>", "250 Recipient ok"),
            ("DATA", "DATA", "354 End data with <CRLF>.<CRLF>"),
            ("QUIT", "QUIT", f"221 {domain} closing connection"),
        ]
        delivered = True
        reason_text = ""
        for step, command, expected in commands:
            request, response = self._tcp_exchange(
                connection,
                max(128, len(command)),
                192,
                {
                    "service": "SMTP",
                    "message": command,
                    "command": step,
                    "sender": sender,
                    "recipient": recipient,
                },
                {"service": "SMTP", "message": expected, "command": f"{step} RESPONSE"},
            )
            result["packets"].extend([request, response])
            result["transcript"].append(
                {
                    "client": command,
                    "server": expected if response["delivered"] else "550 Request rejected",
                    "delivered": bool(response["delivered"]),
                }
            )
            if not (request["delivered"] and response["delivered"]):
                delivered = False
                reason_text = request["drop_reason"] or response["drop_reason"] or "NO_RESPONSE"
                break

        if delivered and str(recipient).rsplit("@", 1)[-1] != domain:
            delivered = False
            reason_text = "RELAY_DENIED"
            result["transcript"].append(
                {"client": "RCPT TO", "server": f"550 Relay denied for {domain}", "delivered": True}
            )

        message = {
            "sender": sender,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "received_at": self.now,
            "client": client,
        }
        if delivered:
            self.smtp_mailboxes.setdefault(server_device, []).append(message)
        self._close_tcp(connection)
        latency = self.now - started
        self.service_metrics.record_latency("SMTP", latency)
        if delivered:
            self.service_metrics.increment("SMTP", "delivered")
        else:
            self.service_metrics.increment("SMTP", "failed")
            instance.failures += 1
        instance.requests += 1
        self.log(
            EventType.SMTP_MESSAGE,
            f"SMTP message {'delivered' if delivered else 'failed'}: {sender} -> {recipient}",
            component=server_device,
            client=client,
            server=server_device,
            sender=sender,
            recipient=recipient,
            subject=subject,
            delivered=delivered,
            latency=latency,
            reason=reason_text,
        )
        result.update(
            {
                "success": delivered,
                "delivered": delivered,
                "reason": reason_text,
                "latency": latency,
                "transcript_text": "\n".join(
                    f"> {item['client']}\n< {item['server']}" for item in result["transcript"]
                ),
            }
        )
        return result

    def smtp_state(self) -> Dict[str, Any]:
        return {
            "servers": [
                instance.to_dict()
                for instance in self.registry.list()
                if instance.name == "SMTP"
            ],
            "mailboxes": {
                device: len(messages) for device, messages in self.smtp_mailboxes.items()
            },
            "messages": [
                {"server": device, **message}
                for device, messages in self.smtp_mailboxes.items()
                for message in messages
            ],
        }

    # ------------------------------------------------------------------
    # Packet security inspection (called by the simulator before delivery)
    # ------------------------------------------------------------------
    def _packet_info(self, packet: Any, path: Optional[List[str]] = None) -> Dict[str, Any]:
        transport_packet = getattr(packet, "transport", None)
        service_info = dict(getattr(packet, "service", {}) or {})
        attack = getattr(packet, "attack", None)
        if transport_packet is not None:
            protocol = transport_packet.protocol
            source_port = transport_packet.source_port
            destination_port = transport_packet.destination_port
        else:
            protocol = str(attack.get("protocol")) if attack else "IP"
            source_port = attack.get("source_port") if attack else None
            destination_port = attack.get("destination_port") if attack else None
        service = service_info.get("service") or port_service(protocol, destination_port)
        return {
            "packet_id": packet.packet_id,
            "source": packet.source,
            "destination": packet.destination,
            "source_ip": self.device_ip(packet.source),
            "destination_ip": self.device_ip(packet.destination),
            "protocol": protocol,
            "source_port": source_port,
            "destination_port": destination_port,
            "service": service,
            "traffic_class": packet.traffic_type,
            "creation_time": packet.creation_time,
            "route": list(path or []),
            "attack": attack,
            "flow_id": service_info.get("flow_id") or getattr(packet, "transport_flow_id", None),
        }

    def _security_active(self, packet: Any) -> bool:
        if getattr(packet, "attack", None):
            return True
        if self.firewall.active or self.acls.acls or self.floods:
            return True
        return bool(getattr(packet, "service", None))

    def inspect_packet(
        self,
        packet: Any,
        path: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate firewall, port filter, ACLs, and flood protection.

        Returns ``None`` when no security policy is configured, otherwise a
        decision dictionary consumed by :meth:`simulator.process_next_packet`.
        """

        if not self._security_active(packet):
            return None
        info = self._packet_info(packet, path)
        decision: Dict[str, Any] = {
            "packet_id": packet.packet_id,
            "source": info["source"],
            "destination": info["destination"],
            "source_ip": info["source_ip"],
            "destination_ip": info["destination_ip"],
            "protocol": info["protocol"],
            "source_port": info["source_port"],
            "destination_port": info["destination_port"],
            "service": info["service"],
            "firewall": "NO_RULE",
            "firewall_rule": None,
            "acl": "NO MATCH",
            "acl_name": None,
            "acl_entry": None,
            "arp": "NORMAL",
            "traffic": "NORMAL",
            "allowed": True,
            "reason": None,
            "decision": "ALLOW",
        }
        self.security_metrics.increment("packets_inspected")

        if info["attack"]:
            decision["traffic"] = "ANOMALOUS"
            self.security_metrics.increment("suspicious_packets")
        else:
            self.security_metrics.increment("normal_packets")

        # 1. Flood protection for controlled simulated attacks.
        scenario = self._flood_scenario_for(packet, info)
        if scenario is not None:
            decision["traffic"] = "ANOMALOUS"
            decision["flood"] = scenario.scenario_id
            if self.flood_protection and scenario.detected:
                if (
                    scenario.detected_at is None
                    or packet.creation_time >= scenario.detected_at
                ):
                    decision.update(
                        {
                            "allowed": False,
                            "reason": DROP_FLOOD,
                            "decision": "DROP",
                            "firewall": "DENY",
                        }
                    )
                    self._apply_decision(packet, decision)
                    return decision

        # 2. Firewall and port filtering.
        if self.firewall.active:
            rule, action, stateful = self.firewall.evaluate(info, list(path or []))
            if rule is not None:
                self.security_metrics.increment("rules_matched")
                decision["firewall"] = action
                decision["firewall_rule"] = rule.rule_id
                decision["stateful"] = stateful
                self.log(
                    EventType.FIREWALL_RULE_MATCHED,
                    f"Firewall rule {rule.rule_id} ({rule.action}) matched packet {packet.packet_id}",
                    component=rule.device or info["source"],
                    rule_id=rule.rule_id,
                    action=rule.action,
                    source=info["source"],
                    destination=info["destination"],
                    protocol=info["protocol"],
                    destination_port=info["destination_port"],
                    service=info["service"],
                )
                if action == "DENY":
                    decision.update(
                        {
                            "allowed": False,
                            "reason": rule.reason,
                            "decision": "DROP",
                        }
                    )
                    self._apply_decision(packet, decision)
                    return decision
            else:
                decision["firewall"] = "ALLOW"
                decision["stateful"] = stateful

        # 3. Interface access control lists along the traversed path.
        acl, entry = self.acls.evaluate(info, list(path or []))
        if acl is not None and entry is not None:
            decision["acl"] = "MATCH"
            decision["acl_name"] = acl.name
            decision["acl_entry"] = entry.sequence
            self.acls.matches += 1
            self.log(
                EventType.ACL_MATCHED,
                f"ACL {acl.name} entry {entry.sequence} ({entry.action}) matched packet {packet.packet_id}",
                component=acl.name,
                acl=acl.name,
                sequence=entry.sequence,
                action=entry.action,
                source=info["source"],
                destination=info["destination"],
                protocol=info["protocol"],
                destination_port=info["destination_port"],
            )
            if entry.action == "DENY":
                self.acls.denied += 1
                decision.update(
                    {
                        "allowed": False,
                        "reason": DROP_ACL,
                        "decision": "DROP",
                    }
                )
                self.log(
                    EventType.ACL_DENIED,
                    f"ACL {acl.name} denied packet {packet.packet_id} ({info['source']} -> {info['destination']})",
                    component=acl.name,
                    acl=acl.name,
                    sequence=entry.sequence,
                    source=info["source"],
                    destination=info["destination"],
                    protocol=info["protocol"],
                    destination_port=info["destination_port"],
                )
                self._apply_decision(packet, decision)
                return decision

        self._apply_decision(packet, decision)
        return decision

    def _apply_decision(self, packet: Any, decision: Dict[str, Any]) -> None:
        packet.security = dict(decision)
        if decision["allowed"]:
            self.security_metrics.increment("packets_allowed")
            self.firewall.packets_allowed += 1
            if self.firewall.active:
                self.log(
                    EventType.PACKET_ALLOWED,
                    f"Packet {packet.packet_id} allowed by policy "
                    f"({decision['source']} -> {decision['destination']})",
                    component=decision["source"],
                    source=decision["source"],
                    destination=decision["destination"],
                    protocol=decision["protocol"],
                    service=decision["service"],
                    destination_port=decision["destination_port"],
                )
        else:
            self.security_metrics.increment("packets_blocked")
            self.firewall.packets_blocked += 1
            reason = decision["reason"] or DROP_FIREWALL
            if reason == DROP_PORT:
                self.security_metrics.increment("port_blocks")
            elif reason == DROP_ACL:
                self.security_metrics.increment("acl_blocks")
            else:
                self.security_metrics.increment("firewall_blocks")
            if reason == DROP_FLOOD:
                self.security_metrics.increment("attack_packets_dropped")

    # -- firewall / ACL control ---------------------------------------
    def add_firewall_rule(self, **kwargs: Any) -> FirewallRule:
        rule = self.firewall.add_rule(**kwargs)
        self.log(
            EventType.FIREWALL_RULE_MATCHED,
            f"Firewall rule {rule.rule_id} added: {rule.action} "
            f"{rule.source_ip} -> {rule.destination_ip} {rule.protocol or 'any'} "
            f"port {rule.destination_port or 'any'}",
            component=rule.device,
            rule_id=rule.rule_id,
            action=rule.action,
            **{
                key: value
                for key, value in rule.to_dict().items()
                if key not in {"rule_id", "action"}
            },
        )
        return rule

    def add_port_filter(
        self,
        protocol: str,
        port: int,
        action: str = "DENY",
        device: Optional[str] = None,
    ) -> FirewallRule:
        """Add a pure port rule that reports ``PORT_BLOCKED`` when it denies."""

        return self.add_firewall_rule(
            action=action,
            protocol=str(protocol).upper(),
            destination_port=int(port),
            device=device,
            reason=DROP_PORT if str(action).upper() == "DENY" else DROP_FIREWALL,
            description=f"port filter {str(protocol).upper()}/{int(port)} {str(action).upper()}",
        )

    def add_acl_entry(self, name: str, **kwargs: Any) -> AclEntry:
        return self.acls.add_entry(name, **kwargs)

    def create_acl(self, name: str, acl_type: str = "STANDARD") -> AccessList:
        return self.acls.create(name, acl_type)

    def attach_acl(self, name: str, device: str, interface_id: str = "eth0") -> AccessList:
        self.device(device)
        return self.acls.attach(name, device, interface_id)

    # ------------------------------------------------------------------
    # ARP security (educational simulation only)
    # ------------------------------------------------------------------
    def _record_binding(self, device: str, ip_address: str, mac: str) -> Optional[str]:
        bindings = self.arp_bindings.setdefault(device, {})
        history = bindings.setdefault(ip_address, [])
        known = next((item for item in history if item != mac), None)
        if mac not in history:
            history.append(mac)
        return known

    def arp_learn(
        self,
        device: str,
        ip_address: str,
        mac_address: str,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an ARP binding and run conflict detection."""

        known = self._record_binding(device, ip_address, mac_address)
        entry = ArpSecurityEntry(
            device=device,
            ip_address=ip_address,
            mac_address=mac_address,
            known_mac=known,
            timestamp=self.now,
        )
        if known is None:
            entry.state = "LEGITIMATE"
            self.arp_entries.append(entry)
            return {"conflict": False, "entry": entry.to_dict()}
        return self._arp_conflict(device, ip_address, known, mac_address, actor, entry)

    def _arp_conflict(
        self,
        device: str,
        ip_address: str,
        known_mac: str,
        new_mac: str,
        actor: Optional[str],
        entry: ArpSecurityEntry,
    ) -> Dict[str, Any]:
        entry.state = "CONFLICT"
        entry.severity = "HIGH"
        self.arp_entries.append(entry)
        conflict = {
            "ip_address": ip_address,
            "known_mac": known_mac,
            "new_mac": new_mac,
            "device": device,
            "attacker": actor,
            "timestamp": self.now,
            "severity": "HIGH",
        }
        self.arp_conflicts.append(conflict)
        self.security_metrics.increment("arp_conflicts")
        self.log(
            EventType.ARP_CONFLICT,
            f"ARP conflict on {device}: {ip_address} mapped to {known_mac} and {new_mac}",
            component=device,
            ip_address=ip_address,
            known_mac=known_mac,
            new_mac=new_mac,
            attacker=actor,
            severity="HIGH",
        )
        self.log(
            EventType.ARP_ANOMALY_DETECTED,
            f"ARP anomaly detected on {device} for {ip_address} (educational detector)",
            component=device,
            ip_address=ip_address,
            known_mac=known_mac,
            new_mac=new_mac,
            attacker=actor,
            detector="NETADAPT_SIMULATED",
        )
        if self.arp_protection:
            existing = self.simulator.arp.lookup(ip_address, source=device, now=self.now)
            if existing is not None and existing.mac_address == new_mac:
                interface = self.device(device).interfaces[0]
                self.simulator.arp.insert(
                    device, ip_address, known_mac, interface.interface_id, now=self.now
                )
                conflict["action"] = "LEGITIMATE_BINDING_RESTORED"
        return {"conflict": True, "entry": entry.to_dict(), "details": conflict}

    def arp_spoof(
        self,
        attacker: str,
        victim: str,
        target_ip: Optional[str] = None,
        detect: bool = True,
        protect: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Poison a victim's simulated ARP cache (simulation only)."""

        attacker_device = self.device(attacker)
        victim_device = self.device(victim)
        gateway_ip = target_ip or victim_device.default_gateway
        if not gateway_ip:
            neighbours = [
                name
                for name in self.simulator.topology.devices
                if self.simulator.topology.graph.has_edge(name, victim)
            ]
            gateway_ip = self.device_ip(neighbours[0]) if neighbours else None
        if not gateway_ip:
            raise ValueError("ARP spoofing needs a gateway IP or an explicit target IP")
        legitimate_mac = None
        for name, device in self.simulator.topology.devices.items():
            for interface in device.interfaces:
                if interface.ip_address == gateway_ip:
                    legitimate_mac = interface.mac_address
                    break
        attacker_mac = attacker_device.interfaces[0].mac_address
        self.spoof_attempts += 1
        self.security_metrics.increment("spoof_attempts")
        existing = self.simulator.arp.lookup(gateway_ip, source=victim, now=self.now)
        if existing is not None:
            legitimate_mac = existing.mac_address
        self.log(
            EventType.ARP_SPOOF_ATTEMPT,
            f"ARP spoof attempt: {attacker} forges {gateway_ip} on {victim}",
            component=attacker,
            attacker=attacker,
            victim=victim,
            gateway=gateway_ip,
            legitimate_mac=legitimate_mac,
            forged_mac=attacker_mac,
            simulation_only=True,
        )
        if legitimate_mac is None:
            legitimate_mac = "00:00:00:00:00:00"
        if existing is None:
            self.arp_learn(victim, gateway_ip, legitimate_mac, actor=attacker)
        self.arp_bindings.setdefault(victim, {}).setdefault(gateway_ip, [legitimate_mac])

        # Poison the victim's simulated cache.
        interface = victim_device.interfaces[0]
        self.simulator.arp.insert(
            victim, gateway_ip, attacker_mac, interface.interface_id, now=self.now
        )
        self.poisoned_entries += 1
        self.security_metrics.increment("poisoned_entries")
        self.arp_entries.append(
            ArpSecurityEntry(
                device=victim,
                ip_address=gateway_ip,
                mac_address=attacker_mac,
                state="POISONED",
                known_mac=legitimate_mac,
                attacker=attacker,
                victim=victim,
                gateway=gateway_ip,
                timestamp=self.now,
                severity="CRITICAL",
            )
        )
        self.log(
            EventType.ARP_CACHE_POISONED,
            f"{victim} ARP cache poisoned: {gateway_ip} -> {attacker_mac}",
            component=victim,
            attacker=attacker,
            victim=victim,
            gateway=gateway_ip,
            poisoned_mac=attacker_mac,
            legitimate_mac=legitimate_mac,
            simulation_only=True,
        )
        detection: Dict[str, Any] = {"detected": False}
        if detect and self.arp_detection:
            detection = self.arp_learn(victim, gateway_ip, attacker_mac, actor=attacker)
        if protect is not None:
            self.arp_protection = bool(protect)
        return {
            "attacker": attacker,
            "victim": victim,
            "gateway": gateway_ip,
            "legitimate_mac": legitimate_mac,
            "attacker_mac": attacker_mac,
            "poisoned": True,
            "detection": detection,
            "simulation_only": True,
            "state": "CONFLICT" if detection.get("conflict") else "POISONED",
        }

    def arp_state(self) -> Dict[str, Any]:
        return {
            "detection_enabled": self.arp_detection,
            "protection_enabled": self.arp_protection,
            "spoof_attempts": self.spoof_attempts,
            "poisoned_entries": self.poisoned_entries,
            "conflicts": list(self.arp_conflicts),
            "entries": [entry.to_dict() for entry in self.arp_entries[-40:]],
            "note": "Educational simulation only - no real ARP traffic is generated.",
        }

    # ------------------------------------------------------------------
    # Flood simulation (inside the simulator only)
    # ------------------------------------------------------------------
    def _flood_scenario_for(self, packet: Any, info: Dict[str, Any]) -> Optional[FloodScenario]:
        attack = info.get("attack")
        if not attack:
            return None
        for scenario in reversed(self.floods):
            if scenario.scenario_id == attack.get("scenario_id"):
                return scenario
        return None

    def start_flood(
        self,
        attacker: str,
        target: str,
        protocol: str = "UDP",
        rate: float = 1000.0,
        duration: float = 1.0,
        threshold: Optional[float] = None,
        protect: Optional[bool] = None,
        size: int = 512,
    ) -> Dict[str, Any]:
        """Generate a controlled flood inside the simulator and detect it."""

        self.device(attacker)
        self.device(target)
        name = str(protocol).upper()
        if name not in ATTACK_PROTOCOLS:
            raise ValueError("Flood protocol must be TCP, UDP, or ICMP")
        if float(rate) <= 0 or float(duration) <= 0:
            raise ValueError("Flood rate and duration must be positive")
        limit = int(self.flood_threshold if threshold is None else float(threshold))
        self._flood_counter += 1
        count = max(1, min(400, int(float(rate) * float(duration))))
        baseline = self.simulator.metrics.calculate(
            self.now, self.simulator.average_congestion(), len(self.simulator.active_flows)
        )["average_latency"]
        queue_before = self.simulator.scheduler_statistics().get("current_queue_length", 0)
        scenario = FloodScenario(
            scenario_id=self._flood_counter,
            attacker=attacker,
            target=target,
            protocol=name,
            rate=float(rate),
            duration=float(duration),
            threshold=limit,
            started_at=self.now,
            protection=bool(self.flood_protection if protect is None else protect),
            baseline_latency=baseline,
        )
        self.floods.append(scenario)
        source_port, destination_port = ATTACK_PORTS[name]
        flow_id = f"FLOOD-{scenario.scenario_id:03d}"
        packets = []
        for index in range(count):
            creation = self.now + (index / float(rate))
            attack = {
                "scenario_id": scenario.scenario_id,
                "protocol": name,
                "flow_id": flow_id,
                "source_port": source_port,
                "destination_port": destination_port,
            }
            if name == "ICMP":
                packet = self.simulator.generate_packet(
                    attacker, target, ATTACK_TRAFFIC_CLASS[name], size, flow_id, creation_time=creation
                )
                packet.attack = attack
                packet.service = {
                    "service": "ICMP",
                    "message": f"ICMP ECHO flood #{index + 1}",
                    "attack": "ICMP_FLOOD",
                    "flow_id": flow_id,
                }
            else:
                transport_packet = self.simulator.transport.enqueue_raw(
                    name,
                    attacker,
                    target,
                    source_port,
                    destination_port,
                    size,
                    flags=["SYN"] if name == "TCP" else None,
                    kind="CONTROL" if name == "TCP" else "DATA",
                    traffic_class=ATTACK_TRAFFIC_CLASS[name],
                    flow_id=flow_id,
                    sequence_number=index,
                    creation_time=creation,
                )
                self._annotate(
                    transport_packet,
                    {
                        "service": "FLOOD",
                        "message": f"{name} flood packet #{index + 1}",
                        "attack": f"{name}_FLOOD",
                        "flow_id": flow_id,
                    },
                )
                packet = self._network_packet(transport_packet)
                if packet is not None:
                    packet.attack = attack
            packets.append(packet)

        # Rate analysis over one-second arrival windows.
        window_start = packets[0].creation_time if packets else self.now
        window_count = 0
        for packet in packets:
            if packet.creation_time - window_start >= 1.0:
                window_start = packet.creation_time
                window_count = 0
            window_count += 1
            scenario.peak_rate = max(scenario.peak_rate, float(window_count))
            if window_count > limit and not scenario.detected:
                scenario.detected = True
                scenario.detected_at = packet.creation_time
                self.detected_floods += 1
                self.security_metrics.increment("floods_detected")
                self.log(
                    EventType.TRAFFIC_SPIKE,
                    f"Traffic spike: {window_count} pps from {attacker} to {target} "
                    f"exceeds threshold {limit:g} pps",
                    component=attacker,
                    source=attacker,
                    target=target,
                    protocol=name,
                    rate=window_count,
                    threshold=limit,
                    scenario_id=scenario.scenario_id,
                    simulation_only=True,
                )
                self.log(
                    EventType.FLOOD_DETECTED,
                    f"Flood detected ({name}) from {attacker} to {target}",
                    component=attacker,
                    source=attacker,
                    target=target,
                    protocol=name,
                    rate=window_count,
                    threshold=limit,
                    scenario_id=scenario.scenario_id,
                    simulation_only=True,
                )
        self.log(
            EventType.FLOOD_STARTED,
            f"Controlled {name} flood started: {attacker} -> {target} "
            f"({count} packets @ {rate:g} pps)",
            component=attacker,
            source=attacker,
            target=target,
            protocol=name,
            rate=float(rate),
            duration=float(duration),
            packets=count,
            simulation_only=True,
        )
        self.flood_protection = scenario.protection or self.flood_protection
        self._pump()
        self.scenario_report(scenario, packets, queue_before)
        return scenario.to_dict()

    def scenario_report(
        self,
        scenario: FloodScenario,
        packets: List[Any],
        queue_before: float = 0.0,
    ) -> Dict[str, Any]:
        delivered = [item for item in packets if item.delivery_status == "DELIVERED"]
        dropped = [item for item in packets if item.delivery_status == "DROPPED"]
        latencies = [item.latency for item in delivered if item.latency is not None]
        scenario.packets_generated = len(packets)
        scenario.packets_delivered = len(delivered)
        scenario.packets_dropped = len(dropped)
        scenario.bytes = sum(item.size for item in packets)
        scenario.average_latency = sum(latencies) / len(latencies) if latencies else 0.0
        scenario.latency_increase = max(0.0, scenario.average_latency - scenario.baseline_latency)
        queue_after = self.simulator.scheduler_statistics().get("current_queue_length", 0)
        peak = self.simulator.scheduler_statistics().get("max_queue_length", 0)
        scenario.queue_growth = max(0.0, float(peak) - float(queue_before))
        scenario.bandwidth = (
            sum(item.size for item in delivered) / max(scenario.duration, 0.001)
        )
        if scenario.packets_dropped:
            self.security_metrics.increment(
                "attack_packets_dropped",
                sum(
                    1
                    for item in dropped
                    if (getattr(item, "security", {}) or {}).get("reason") == DROP_FLOOD
                ),
            )
        return scenario.to_dict()

    def flood_state(self) -> Dict[str, Any]:
        return {
            "threshold": self.flood_threshold,
            "protection_enabled": self.flood_protection,
            "detected_floods": self.detected_floods,
            "scenarios": [scenario.to_dict() for scenario in self.floods[-10:]],
            "note": "Controlled simulation only - no real network traffic is generated.",
        }

    # ------------------------------------------------------------------
    # State, metrics, tick, reset
    # ------------------------------------------------------------------
    def tick(self, delta: float = 1.0) -> None:
        self.expire_leases()
        self.expire_dns_cache()

    def security_state(self) -> Dict[str, Any]:
        return {
            "firewall": self.firewall.to_dict(),
            "access_lists": self.acls.to_dict(),
            "arp": self.arp_state(),
            "flood": self.flood_state(),
            "drop_reasons": [DROP_FIREWALL, DROP_ACL, DROP_PORT, DROP_FLOOD, DROP_ARP],
            "metrics": self.security_metrics.to_dict(),
            "result": None,
        }

    def service_state(self) -> Dict[str, Any]:
        return {
            "ports": dict(SERVICE_PORTS),
            "protocols": dict(SERVICE_PROTOCOLS),
            "traffic_classes": dict(SERVICE_TRAFFIC_CLASS),
            "registry": self.registry.to_dict(),
            "status": self.service_status_all(),
            "dhcp": self.dhcp_state(),
            "dns": self.dns_state(),
            "http": self.http_state(),
            "ftp": self.ftp_state(),
            "smtp": self.smtp_state(),
            "metrics": self.service_metrics.to_dict(),
            "exchanges": self.recent_exchanges[-20:],
            "result": None,
        }

    def service_status_all(self) -> List[Dict[str, Any]]:
        rows = []
        for instance in self.registry.list():
            ready, reason = self.service_ready(instance)
            rows.append(
                {
                    **instance.to_dict(),
                    "available": ready,
                    "unavailable_reason": reason,
                    "ip": self.device_ip(instance.device),
                }
            )
        return rows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "services": self.service_state(),
            "security": self.security_state(),
        }

    def reset(self) -> None:
        """Clear runtime state but keep installed services and policies."""

        self.leases.clear()
        self.dhcp_clients.clear()
        self.dns_caches.clear()
        self.dns_zones.clear()
        self.http_resources.clear()
        self.ftp_files.clear()
        self.smtp_mailboxes.clear()
        self.smtp_domain.clear()
        self.arp_entries.clear()
        self.arp_conflicts.clear()
        self.arp_bindings.clear()
        self.spoof_attempts = 0
        self.poisoned_entries = 0
        self.floods.clear()
        self.detected_floods = 0
        self._flood_counter = 0
        self.firewall.reset_counters()
        self.acls.matches = 0
        self.acls.denied = 0
        self.service_metrics.reset()
        self.security_metrics.reset()
        self.recent_exchanges.clear()
        for instance in self.registry.list():
            self.ensure_defaults(instance)
