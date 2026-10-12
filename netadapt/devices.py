"""Device and interface abstractions for NetAdapt Stage 7.

The existing graph in :mod:`topology` remains the source of truth for link
conditions and failure injection.  These lightweight objects provide the
network-infrastructure vocabulary used by ARP, switching, routing diagnostics,
and future protocol work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from typing import Any, Dict, Iterable, List, Optional

from addressing import IPv4Address, parse_interface, subnet_details, validate_ipv4, validate_prefix


DEVICE_TYPES = (
    "host", "pc", "laptop", "server", "printer", "router", "switch", "hub",
    "access_point", "cloud", "internet",
)
_INTERFACE_COUNTER = 0


def _normalise_device_type(device_type: str) -> str:
    value = str(device_type).lower().strip()
    aliases = {
        "pc": "host", "computer": "host", "laptop": "laptop", "server": "server",
        "printer": "printer", "switch": "switch", "router": "router", "hub": "hub",
        "access_point": "access_point", "access point": "access_point", "ap": "access_point",
        "cloud": "cloud", "internet": "internet", "external": "internet",
    }
    value = aliases.get(value, value)
    if value not in DEVICE_TYPES:
        raise ValueError(f"Unknown device type: {device_type}")
    return value


def deterministic_mac(seed: str) -> str:
    """Return a stable locally-administered MAC address for a device/interface."""

    # A small explicit checksum avoids Python's process-randomised hash().
    value = 0
    for character in seed:
        value = (value * 131 + ord(character)) & 0xFFFFFF
    return f"02:00:{value >> 16 & 0xff:02x}:{(value >> 8) & 0xff:02x}:{value & 0xff:02x}:00"


def default_address(device_name: str, device_type: str = "host") -> tuple[str, int]:
    """Assign a deterministic educational IPv4 address to a named device."""

    match = re.fullmatch(r"([A-Za-z]+)(\d+)", str(device_name))
    index = int(match.group(2)) if match else (sum(map(ord, str(device_name))) % 200) + 1
    kind = _normalise_device_type(device_type)
    if kind in {"host", "pc", "laptop", "printer"}:
        return f"192.168.1.{max(1, index)}", 24
    if kind == "router":
        return f"10.0.0.{max(1, index)}", 30
    if kind == "switch":
        return f"172.16.0.{max(1, index)}", 24
    return f"172.20.0.{max(1, index)}", 24


@dataclass
class NetworkInterface:
    """A named interface with L2/L3 addressing and link associations."""

    interface_id: str = "eth0"
    mac_address: Optional[str] = None
    ip_address: Optional[str] = None
    prefix: int = 24
    link_associations: List[str] = field(default_factory=list)
    status: str = "UP"

    def __post_init__(self) -> None:
        if not self.interface_id:
            raise ValueError("Interface ID cannot be empty")
        if self.mac_address is None:
            self.mac_address = deterministic_mac(self.interface_id)
        if not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", self.mac_address):
            raise ValueError(f"Invalid MAC address: {self.mac_address}")
        if self.ip_address is not None:
            address, parsed_prefix = parse_interface(str(self.ip_address))
            if self.prefix == 24 and parsed_prefix != 32:
                self.prefix = parsed_prefix
            self.ip_address = address
            validate_prefix(self.prefix)
        if str(self.status).upper() not in {"UP", "DOWN"}:
            raise ValueError("Interface status must be UP or DOWN")
        self.status = str(self.status).upper()
        if isinstance(self.link_associations, str):
            self.link_associations = [self.link_associations]
        else:
            self.link_associations = list(self.link_associations)

    @property
    def name(self) -> str:
        return self.interface_id

    @property
    def link_association(self) -> Optional[str]:
        return self.link_associations[0] if self.link_associations else None

    @property
    def address(self) -> Optional[IPv4Address]:
        if self.ip_address is None:
            return None
        return IPv4Address(self.ip_address, self.prefix)

    @property
    def network_address(self) -> Optional[str]:
        return self.address.network_address if self.address else None

    @property
    def broadcast_address(self) -> Optional[str]:
        return self.address.broadcast_address if self.address else None

    @property
    def subnet_mask(self) -> Optional[str]:
        return self.address.subnet_mask if self.address else None

    def assign_ip(self, address: str, prefix: Optional[int] = None) -> None:
        parsed, parsed_prefix = parse_interface(address)
        self.ip_address = parsed
        self.prefix = validate_prefix(prefix if prefix is not None else parsed_prefix)

    def associate_link(self, link_id: str) -> None:
        if link_id not in self.link_associations:
            self.link_associations.append(link_id)

    def set_status(self, status: str) -> None:
        normalized = str(status).upper()
        if normalized not in {"UP", "DOWN"}:
            raise ValueError("Interface status must be UP or DOWN")
        self.status = normalized

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interface_id": self.interface_id,
            "name": self.name,
            "mac_address": self.mac_address,
            "ip_address": self.ip_address,
            "prefix": self.prefix,
            "subnet_mask": self.subnet_mask,
            "network_address": self.network_address,
            "broadcast_address": self.broadcast_address,
            "link_associations": list(self.link_associations),
            "status": self.status,
        }


# Friendly aliases for callers that use the shorter educational names.
Interface = NetworkInterface


class Device:
    """A host, PC, router, switch, or server with one or more interfaces."""

    def __init__(
        self,
        name: str,
        device_type: str = "host",
        interfaces: Optional[Iterable[NetworkInterface]] = None,
        *,
        default_gateway: Optional[str] = None,
        status: str = "UP",
    ) -> None:
        if not name:
            raise ValueError("Device name cannot be empty")
        self.name = name
        self.device_type = _normalise_device_type(device_type)
        self.type = self.device_type
        self.status = str(status).upper()
        if self.status not in {"UP", "DOWN"}:
            raise ValueError("Device status must be UP or DOWN")
        self.default_gateway = default_gateway
        self.interfaces: List[NetworkInterface] = list(interfaces or [])
        if not self.interfaces:
            address, prefix = default_address(name, self.device_type)
            self.interfaces.append(NetworkInterface("eth0", ip_address=address, prefix=prefix))
        if self.device_type == "switch":
            self.mac_table: Dict[str, str] = {}

    @property
    def id(self) -> str:
        return self.name

    @property
    def is_router(self) -> bool:
        return self.device_type == "router"

    @property
    def is_switch(self) -> bool:
        return self.device_type == "switch"

    def add_interface(self, interface: NetworkInterface) -> NetworkInterface:
        if any(item.interface_id == interface.interface_id for item in self.interfaces):
            raise ValueError(f"Interface {interface.interface_id} already exists on {self.name}")
        self.interfaces.append(interface)
        return interface

    def get_interface(self, interface_id: Optional[str] = None) -> NetworkInterface:
        if interface_id is None:
            return self.interfaces[0]
        for interface in self.interfaces:
            if interface.interface_id == interface_id or interface.mac_address == interface_id:
                return interface
        raise KeyError(f"Unknown interface {interface_id} on {self.name}")

    def interface_for_link(self, link_id: str) -> NetworkInterface:
        for interface in self.interfaces:
            if link_id in interface.link_associations:
                return interface
        return self.interfaces[0]

    def set_status(self, status: str) -> None:
        normalized = str(status).upper()
        if normalized not in {"UP", "DOWN"}:
            raise ValueError("Device status must be UP or DOWN")
        self.status = normalized
        for interface in self.interfaces:
            interface.set_status(normalized)

    def learn_mac(self, mac_address: str, interface_id: str) -> None:
        if self.is_switch:
            self.mac_table[mac_address] = interface_id

    def lookup_mac(self, mac_address: str) -> Optional[str]:
        return self.mac_table.get(mac_address) if self.is_switch else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "device_type": self.device_type,
            "status": self.status,
            "default_gateway": self.default_gateway,
            "interfaces": [interface.to_dict() for interface in self.interfaces],
            "mac_table": dict(self.mac_table) if self.is_switch else {},
        }


class Switch(Device):
    """Explicit switch type for callers that prefer a concrete class."""

    def __init__(self, name: str, interfaces: Optional[Iterable[NetworkInterface]] = None) -> None:
        super().__init__(name, "switch", interfaces)


@dataclass(frozen=True)
class RoutingTableEntry:
    destination_network: str
    prefix: int
    next_hop: Optional[str]
    outgoing_interface: str
    metric: float = 0.0

    def __post_init__(self) -> None:
        validate_prefix(self.prefix)
        network = ipaddress.ip_network(
            f"{self.destination_network}/{self.prefix}", strict=False
        )
        object.__setattr__(self, "destination_network", str(network.network_address))

    def matches(self, destination_ip: str) -> bool:
        return validate_ipv4(destination_ip) in ipaddress.IPv4Network(
            f"{self.destination_network}/{self.prefix}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "destination_network": self.destination_network,
            "prefix": self.prefix,
            "next_hop": self.next_hop,
            "outgoing_interface": self.outgoing_interface,
            "metric": self.metric,
        }


class RoutingTable:
    """Longest-prefix-match routing table for one router."""

    def __init__(self, router_name: str = "router") -> None:
        self.router_name = router_name
        self.entries: List[RoutingTableEntry] = []

    def add(
        self,
        destination_network: str,
        prefix: int,
        next_hop: Optional[str],
        outgoing_interface: str,
        metric: float = 0.0,
    ) -> RoutingTableEntry:
        entry = RoutingTableEntry(
            destination_network, prefix, next_hop, outgoing_interface, float(metric)
        )
        self.entries = [item for item in self.entries if item.to_dict() != entry.to_dict()]
        self.entries.append(entry)
        return entry

    def lookup(self, destination_ip: str) -> Optional[RoutingTableEntry]:
        matches = [entry for entry in self.entries if entry.matches(destination_ip)]
        if not matches:
            return None
        return max(matches, key=lambda entry: entry.prefix)

    def clear(self) -> None:
        self.entries.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "router": self.router_name,
            "entries": [entry.to_dict() for entry in self.entries],
        }
