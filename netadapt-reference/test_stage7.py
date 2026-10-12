"""Stage 7 tests for network devices and educational ARP/ICMP behavior."""

import pytest

from addressing import IPv4Address, subnet_details, validate_ipv4
from devices import Device, Interface, NetworkInterface, RoutingTable, Switch
from events import EventType
from protocols import ProtocolPacket, SwitchFrame
from qos import Packet
from simulator import NetworkSimulator
from topology import NetworkTopology


def test_device_types_and_interfaces():
    host = Device("PC1", "pc")
    router = Device("R1", "router")
    switch = Switch("SW1")
    server = Device("S1", "server")

    assert host.device_type == "host"
    assert router.is_router
    assert switch.is_switch
    assert server.device_type == "server"
    assert host.interfaces[0].interface_id == "eth0"
    assert host.interfaces[0].mac_address


def test_interface_addressing_and_link_association():
    interface = Interface(
        "GigabitEthernet0/1",
        mac_address="02:00:00:00:00:01",
        ip_address="192.168.10.10/26",
        link_associations="R1-PC1",
    )
    assert interface.name == "GigabitEthernet0/1"
    assert interface.subnet_mask == "255.255.255.192"
    assert interface.network_address == "192.168.10.0"
    assert interface.broadcast_address == "192.168.10.63"
    assert interface.link_association == "R1-PC1"


def test_ipv4_validation_and_subnet_details():
    address = IPv4Address("10.1.2.3", 24)
    assert str(address) == "10.1.2.3/24"
    assert address.network_address == "10.1.2.0"
    assert address.contains("10.1.2.255")
    assert not address.contains("10.1.3.1")
    assert validate_ipv4("192.0.2.1").version == 4
    assert subnet_details("192.0.2.10", 25)["network"] == "192.0.2.0/25"
    with pytest.raises(ValueError):
        IPv4Address("not-an-ip", 24)
    with pytest.raises(ValueError):
        IPv4Address("10.0.0.1", 33)


def test_topology_exposes_proper_device_registry_and_unique_macs():
    topology = NetworkTopology()
    assert topology.get_device("H1").device_type == "host"
    assert topology.get_device("R1").device_type == "router"
    assert topology.get_device("H1").default_gateway == "R1"
    macs = [device.interfaces[0].mac_address for device in topology.devices.values()]
    assert len(macs) == len(set(macs))
    assert all("H1-R1" in device.interfaces[0].link_associations for device in [topology.get_device("H1")])


def test_custom_switch_and_server_nodes_are_supported():
    topology = NetworkTopology()
    topology.add_node("SW1", "switch")
    topology.add_node("S1", "server")
    topology.add_link("H1", "SW1")
    topology.add_link("SW1", "R1")
    assert topology.get_device("SW1").is_switch
    assert topology.get_device("S1").device_type == "server"
    assert topology.graph.has_edge("SW1", "R1")


def test_routing_table_longest_prefix_lookup():
    table = RoutingTable("R1")
    table.add("10.0.0.0", 8, "R2", "eth0", 2)
    table.add("10.1.0.0", 16, "R3", "eth1", 1)
    assert table.lookup("10.1.2.3").next_hop == "R3"
    assert table.lookup("10.2.2.3").next_hop == "R2"
    assert table.lookup("192.168.1.1") is None


def test_arp_request_reply_and_cache_events():
    sim = NetworkSimulator(seed=1)
    entry = sim.arp_lookup("H1", sim.get_device("R1").interfaces[0].ip_address)
    assert entry is not None
    assert entry.mac_address == sim.get_device("R1").interfaces[0].mac_address
    assert sim.arp.lookup(entry.ip_address, source="H1", now=sim.time) == entry
    events = [event.event_type for event in sim.event_logger.events]
    assert EventType.ARP_REQUEST in events
    assert EventType.ARP_REPLY in events
    assert EventType.ARP_CACHE_UPDATE in events


def test_arp_cache_expiration_and_reset():
    sim = NetworkSimulator(seed=1)
    entry = sim.arp_lookup("H1", sim.get_device("R1").interfaces[0].ip_address)
    sim.time = entry.expires_at + 1
    assert sim.arp.lookup(entry.ip_address, source="H1", now=sim.time) is None
    sim.reset()
    assert sim.arp.to_dict() == {}


def test_arp_miss_is_recorded():
    sim = NetworkSimulator(seed=1)
    assert sim.arp_lookup("H1", "203.0.113.99") is None
    assert EventType.ARP_MISS in [event.event_type for event in sim.event_logger.events]


def test_icmp_ping_uses_route_arp_and_records_rtt_hops():
    sim = NetworkSimulator(seed=1)
    result = sim.ping("H1", "H3")
    assert result.success is True
    assert result.route[0] == "H1" and result.route[-1] == "H3"
    assert result.hops == len(result.route) - 1
    assert result.rtt > 0
    assert result.arp_resolutions == len(result.route) - 1
    events = [event.event_type for event in sim.event_logger.events]
    assert EventType.ICMP_ECHO_REQUEST in events
    assert EventType.ICMP_ECHO_REPLY in events
    assert EventType.PACKET_FORWARDED in events


def test_ping_is_deterministic_for_same_topology_and_seed():
    first = NetworkSimulator(seed=77).ping("H1", "H3").to_dict()
    second = NetworkSimulator(seed=77).ping("H1", "H3").to_dict()
    assert first == second


def test_ping_ttl_expiry_generates_icmp_event():
    sim = NetworkSimulator(seed=1)
    result = sim.ping("H1", "H3", ttl=1)
    assert result.success is False
    assert result.reason == "TTL_EXCEEDED"
    assert EventType.ICMP_TTL_EXCEEDED in [event.event_type for event in sim.event_logger.events]


def test_packet_ttl_defaults_and_is_configurable():
    packet = Packet(1, "H1", "H3")
    assert packet.ttl == 64
    assert Packet(2, "H1", "H3", ttl=8).ttl == 8
    with pytest.raises(ValueError):
        Packet(3, "H1", "H3", ttl=0)


def test_router_forwarding_accepts_protocol_packet():
    sim = NetworkSimulator(seed=1)
    packet = ProtocolPacket("H1", "H3", "IP", ttl=64)
    forwarded = sim.forward_packet(packet)
    assert forwarded.delivered is True
    assert forwarded.hops == ["R1", "R3", "R5", "H3"]
    assert EventType.PACKET_FORWARDED in [event.event_type for event in sim.event_logger.events]


def test_router_forwarding_accepts_qos_packet():
    sim = NetworkSimulator(seed=1)
    packet = sim.generate_packet("H1", "H3", ttl=2)
    forwarded = sim.forward_packet(packet)
    assert forwarded.dropped is True
    assert forwarded.reason == "TTL_EXCEEDED"


def test_destination_unreachable_is_structured():
    sim = NetworkSimulator(seed=1)
    sim.fail_node("H3")
    result = sim.ping("H1", "H3")
    assert result.success is False
    assert result.reason == "NO_ROUTE"
    assert EventType.DESTINATION_UNREACHABLE in [event.event_type for event in sim.event_logger.events]


def test_routing_table_is_derived_from_live_adaptive_routes():
    sim = NetworkSimulator(seed=1)
    entry = sim.routing_table_lookup("R1", sim.get_device("H3").interfaces[0].ip_address)
    assert entry is not None
    assert entry.outgoing_interface == "eth0"
    assert entry.next_hop in sim.router.shortest_path(sim.topology, "R1", "H3")[0][1:]


def test_switch_learns_forwards_and_floods_unknown_macs():
    topology = NetworkTopology()
    topology.add_node("SW1", "switch")
    topology.add_interface("SW1", NetworkInterface("eth1", mac_address="02:00:00:00:00:11"))
    topology.add_link("H1", "SW1")
    topology.add_link("SW1", "R1")
    sim = NetworkSimulator(topology=topology, seed=1)
    source = "02:00:00:00:00:01"
    unknown = sim.switch_frame("SW1", SwitchFrame(source, "02:00:00:00:00:99", "eth0"), ["eth0", "eth1"])
    assert unknown.action == "FLOOD"
    assert unknown.output_interfaces == ["eth1"]
    known = sim.switch_frame("SW1", SwitchFrame("02:00:00:00:00:22", source, "eth1"), ["eth0", "eth1"])
    assert known.action == "FORWARD"
    assert known.output_interfaces == ["eth0"]
    assert sim.mac_table_lookup("SW1", source) == "eth0"


def test_device_and_interface_failure_integrates_with_existing_faults():
    sim = NetworkSimulator(seed=1, failure_detection_timeout=1.0)
    sim.fail_interface("R3", "eth0")
    assert sim.get_device("R3").get_interface("eth0").status == "DOWN"
    assert sim.is_link_failed("R3", "R1")
    sim.tick(1.1)
    assert EventType.FAILURE_DETECTED in [event.event_type for event in sim.event_logger.events]
    sim.recover_interface("R3", "eth0")
    assert sim.get_device("R3").get_interface("eth0").status == "UP"
    assert not sim.is_link_failed("R3", "R1")


def test_protocol_reset_clears_arp_and_diagnostic_tables():
    sim = NetworkSimulator(seed=1)
    sim.arp_lookup("H1", "10.0.0.1")
    sim.routing_table("R1")
    sim.reset()
    assert sim.arp.to_dict() == {}
    assert sim.protocols.routing_tables == {}
    assert sim.protocols.switch_tables == {}
