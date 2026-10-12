"""IPv4 helpers for the NetAdapt educational network model.

The simulator intentionally models only the addressing facts needed by ARP,
routing tables, and diagnostics.  It does not implement a production IP stack.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any


IPNetwork = ipaddress.IPv4Network
IPv4AddressValue = ipaddress.IPv4Address


def validate_ipv4(address: str) -> ipaddress.IPv4Address:
    """Validate and return an IPv4 address."""

    try:
        return ipaddress.IPv4Address(str(address).split("/", 1)[0])
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"Invalid IPv4 address: {address}") from exc


def validate_prefix(prefix: int) -> int:
    """Validate an IPv4 prefix length."""

    try:
        prefix = int(prefix)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid IPv4 prefix: {prefix}") from exc
    if not 0 <= prefix <= 32:
        raise ValueError("IPv4 prefix must be between 0 and 32")
    return prefix


def subnet_details(address: str, prefix: int = 24) -> dict[str, Any]:
    """Return canonical address and subnet properties for an IPv4 assignment."""

    address_value = validate_ipv4(address)
    prefix = validate_prefix(prefix)
    network = ipaddress.ip_network(f"{address_value}/{prefix}", strict=False)
    return {
        "ip_address": str(address_value),
        "prefix": prefix,
        "subnet_mask": str(network.netmask),
        "network_address": str(network.network_address),
        "broadcast_address": str(network.broadcast_address),
        "network": str(network),
    }


@dataclass(frozen=True)
class IPv4Address:
    """A validated IPv4 address with an optional subnet prefix."""

    address: str
    prefix: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", str(validate_ipv4(self.address)))
        object.__setattr__(self, "prefix", validate_prefix(self.prefix))

    @property
    def ip(self) -> ipaddress.IPv4Address:
        return ipaddress.IPv4Address(self.address)

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f"{self.address}/{self.prefix}", strict=False)

    @property
    def network_address(self) -> str:
        return str(self.network.network_address)

    @property
    def broadcast_address(self) -> str:
        return str(self.network.broadcast_address)

    @property
    def subnet_mask(self) -> str:
        return str(self.network.netmask)

    def contains(self, other: "IPv4Address | str") -> bool:
        other_value = (
            other.ip if isinstance(other, IPv4Address) else validate_ipv4(str(other))
        )
        return other_value in self.network

    def __str__(self) -> str:
        return f"{self.address}/{self.prefix}"


def parse_interface(value: str) -> tuple[str, int]:
    """Parse either ``192.0.2.1`` or ``192.0.2.1/24``."""

    raw = str(value).strip()
    if "/" in raw:
        address, prefix = raw.split("/", 1)
        return str(validate_ipv4(address)), validate_prefix(int(prefix))
    return str(validate_ipv4(raw)), 32
