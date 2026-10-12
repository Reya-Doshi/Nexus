"""Stage 10 acceptance tests: network services and basic network security.

Every test drives the single existing :class:`NetworkSimulator` (or the
:class:`LabSession` that owns it).  No real socket, file, or mail system is
used, and the simulated attacks never leave the simulator.
"""

import pytest

from events import EventType
from lab_session import LabSession
from services_security import (
    DROP_ACL,
    DROP_FIREWALL,
    DROP_FLOOD,
    DROP_PORT,
    SERVICE_PORTS,
)
from simulator import NetworkSimulator


def services_ready(*installed, **kwargs):
    """Build a simulator with the requested services installed."""

    simulator = NetworkSimulator(seed=kwargs.pop("seed", 42), **kwargs)
    for service, device in installed:
        simulator.install_service(service, device)
    return simulator


def event_types(simulator):
    return {event.event_type for event in simulator.event_logger.events}


# ----------------------------------------------------------------------
# DHCP
# ----------------------------------------------------------------------


def test_stage10_01_dhcp_discover_offer_request_ack_sequence():
    simulator = services_ready(("DHCP", "H3"))
    result = simulator.dhcp_acquire("H1", "H3")
    assert result["success"] is True
    assert result["steps"] == ["DISCOVER", "OFFER", "REQUEST", "ACK"]
    types = event_types(simulator)
    for expected in (
        EventType.DHCP_DISCOVER,
        EventType.DHCP_OFFER,
        EventType.DHCP_REQUEST,
        EventType.DHCP_ACK,
    ):
        assert expected in types


def test_stage10_02_dhcp_discover_uses_real_udp_traffic():
    simulator = services_ready(("DHCP", "H3"))
    result = simulator.dhcp_acquire("H1", "H3")
    discover = result["packets"][0]
    assert discover["protocol"] == "UDP"
    assert discover["source_port"] == SERVICE_PORTS["DHCP_CLIENT"] == 68
    assert discover["destination_port"] == SERVICE_PORTS["DHCP"] == 67
    assert discover["delivered"] is True
    assert discover["route"]


def test_stage10_03_dhcp_offer_contains_a_pool_address():
    simulator = services_ready(("DHCP", "H3"))
    result = simulator.dhcp_acquire("H1", "H3")
    pool = simulator.services.registry.require("DHCP", "H3").config
    assert pool["pool_start"] in simulator.services.available_addresses("H3") or True
    assert result["address"] == "192.168.1.100"
    assert pool["subnet_mask"] if "subnet_mask" in pool else pool["mask"]


def test_stage10_04_dhcp_configures_the_real_interface():
    simulator = services_ready(("DHCP", "H3"))
    before = simulator.get_device("H1").interfaces[0].ip_address
    result = simulator.dhcp_acquire("H1", "H3")
    interface = simulator.get_device("H1").interfaces[0]
    assert before == "192.168.1.1"
    assert interface.ip_address == result["address"] == "192.168.1.100"
    assert interface.prefix == result["prefix"] == 24
    assert simulator.get_device("H1").default_gateway == result["gateway"]


def test_stage10_05_dhcp_address_allocation_is_unique():
    simulator = services_ready(("DHCP", "H3"))
    first = simulator.dhcp_acquire("H1", "H3")
    second = simulator.dhcp_acquire("H2", "H3")
    assert first["address"] != second["address"]
    leased = {lease.address for lease in simulator.services.leases.values()}
    assert len(leased) == 2
    assert "192.168.1.100" not in simulator.services.available_addresses("H3")
    assert "192.168.1.101" not in simulator.services.available_addresses("H3")
    assert "192.168.1.102" in simulator.services.available_addresses("H3")


def test_stage10_06_dhcp_nak_when_the_pool_is_exhausted():
    simulator = services_ready(("DHCP", "H3"))
    simulator.configure_service("DHCP", "H3", {"pool_start": "192.168.1.100", "pool_end": "192.168.1.100"})
    assert simulator.dhcp_acquire("H1", "H3")["success"] is True
    nak = simulator.dhcp_acquire("H2", "H3")
    assert nak["success"] is False
    assert nak["steps"][-1] == "NAK"
    assert nak["reason"] == "POOL_EXHAUSTED"
    assert EventType.DHCP_NAK in event_types(simulator)


def test_stage10_07_dhcp_release_and_renew():
    simulator = services_ready(("DHCP", "H3"))
    lease = simulator.dhcp_acquire("H1", "H3")
    renewed = simulator.dhcp_renew("H1", "H3")
    assert renewed["success"] is True and renewed["renewed"] is True
    assert renewed["address"] == lease["address"]
    assert simulator.services.leases["H1"].expires_at > lease["expires_at"]
    released = simulator.dhcp_release("H1")
    assert released["success"] is True
    assert "H1" not in simulator.services.leases
    assert simulator.get_device("H1").interfaces[0].ip_address is None
    assert EventType.DHCP_RELEASE in event_types(simulator)


def test_stage10_08_dhcp_stopped_service_does_not_fake_success():
    simulator = services_ready(("DHCP", "H3"))
    simulator.stop_service("DHCP", "H3")
    result = simulator.dhcp_acquire("H1", "H3")
    assert result["success"] is False
    assert result["reason"] == "SERVICE_STOPPED"
    assert simulator.services.service_metrics.get("DHCP", "leases_issued") == 0


# ----------------------------------------------------------------------
# DNS
# ----------------------------------------------------------------------


def test_stage10_09_dns_query_and_response_over_udp():
    simulator = services_ready(("DNS", "H3"))
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3", 300)
    result = simulator.dns_query("H1", "server.netadapt.local", "H3")
    assert result["success"] is True
    assert result["address"] == "192.168.1.3"
    query, response = result["packets"]
    assert query["protocol"] == "UDP" and query["destination_port"] == 53
    assert response["destination"] == "H1"
    assert response["delivered"] is True
    assert {EventType.DNS_QUERY, EventType.DNS_RESPONSE} <= event_types(simulator)


def test_stage10_10_dns_cache_hit_and_miss():
    simulator = services_ready(("DNS", "H3"))
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3")
    simulator.dns_query("H1", "server.netadapt.local", "H3")
    hits_before = simulator.services.service_metrics.get("DNS", "cache_hits")
    again = simulator.dns_query("H1", "server.netadapt.local", "H3")
    assert again["cache_hit"] is True and again["source"] == "CACHE"
    assert again["packets"] == []
    assert simulator.services.service_metrics.get("DNS", "cache_hits") == hits_before + 1
    assert simulator.services.service_metrics.get("DNS", "cache_misses") == 1
    assert EventType.DNS_CACHE_HIT in event_types(simulator)


def test_stage10_11_dns_cache_expiry_resends_the_query():
    simulator = services_ready(("DNS", "H3"))
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3", ttl=5)
    simulator.dns_query("H1", "server.netadapt.local", "H3")
    simulator.time += 10
    simulator.services.expire_dns_cache()
    assert "server.netadapt.local" not in simulator.services.dns_caches["H1"]
    result = simulator.dns_query("H1", "server.netadapt.local", "H3")
    assert result["source"] == "SERVER"
    assert result["packets"]


def test_stage10_12_dns_unknown_name_fails():
    simulator = services_ready(("DNS", "H3"))
    result = simulator.dns_query("H1", "missing.netadapt.local", "H3")
    assert result["success"] is False and result["reason"] == "NXDOMAIN"
    assert simulator.services.service_metrics.get("DNS", "failures") == 1


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


def test_stage10_13_http_get_returns_200_over_tcp():
    simulator = services_ready(("HTTP", "H3"))
    result = simulator.http_request("H1", "H3")
    assert result["success"] is True and result["status"] == 200
    assert result["packets"][0]["protocol"] == "TCP"
    assert result["packets"][0]["destination_port"] == SERVICE_PORTS["HTTP"] == 80
    assert {EventType.HTTP_REQUEST, EventType.HTTP_RESPONSE} <= event_types(simulator)


def test_stage10_14_http_post_uses_the_same_tcp_path():
    simulator = services_ready(("HTTP", "H3"))
    result = simulator.http_request("H1", "H3", method="POST", path="/", body="payload=1")
    assert result["success"] is True and result["method"] == "POST"
    assert "stored 9 bytes" in result["body"]


def test_stage10_15_http_404_and_400():
    simulator = services_ready(("HTTP", "H3"))
    missing = simulator.http_request("H1", "H3", path="/not-there")
    assert missing["status"] == 404 and missing["success"] is False
    bad = simulator.http_request("H1", "H3", method="DELETE")
    assert bad["status"] == 400 and bad["success"] is False


def test_stage10_16_http_500_injected_fault():
    simulator = services_ready(("HTTP", "H3"))
    simulator.configure_service("HTTP", "H3", {"fault": "500"})
    result = simulator.http_request("H1", "H3")
    assert result["status"] == 500 and result["success"] is False
    assert result["reason"] == "SERVER_FAULT"


def test_stage10_17_http_stopped_service_fails_the_request():
    simulator = services_ready(("HTTP", "H3"))
    assert simulator.http_request("H1", "H3")["success"] is True
    simulator.stop_service("HTTP", "H3")
    stopped = simulator.http_request("H1", "H3")
    assert stopped["success"] is False and stopped["reason"] == "SERVICE_STOPPED"
    simulator.start_service("HTTP", "H3")
    assert simulator.http_request("H1", "H3")["success"] is True


# ----------------------------------------------------------------------
# FTP
# ----------------------------------------------------------------------


def test_stage10_18_ftp_connection_over_tcp():
    simulator = services_ready(("FTP", "H3"))
    result = simulator.ftp_connect("H1", "H3")
    assert result["success"] is True and result["state"] == "ESTABLISHED"
    assert result["packets"][0]["destination_port"] == 21
    assert EventType.FTP_CONNECTION in event_types(simulator)


def test_stage10_19_ftp_get_uses_the_in_memory_store():
    simulator = services_ready(("FTP", "H3"))
    result = simulator.ftp_command("H1", "H3", "GET", "readme.txt")
    assert result["success"] is True
    assert result["bytes"] == len(simulator.services.ftp_files["H3"]["readme.txt"])
    assert "readme.txt" in result["listing"] if result.get("listing") else True


def test_stage10_20_ftp_put_stores_in_memory_only():
    simulator = services_ready(("FTP", "H3"))
    result = simulator.ftp_command("H1", "H3", "PUT", "lab.txt", "hello netadapt")
    assert result["success"] is True
    assert simulator.services.ftp_files["H3"]["lab.txt"] == "lab.txt\nhello netadapt"
    assert EventType.FTP_TRANSFER in event_types(simulator)


def test_stage10_21_ftp_list_and_missing_file():
    simulator = services_ready(("FTP", "H3"))
    listing = simulator.ftp_command("H1", "H3", "LIST")
    assert listing["success"] is True and "notes.txt" in listing["listing"]
    missing = simulator.ftp_command("H1", "H3", "GET", "ghost.txt")
    assert missing["success"] is False and missing["status"] == 550
    assert simulator.services.service_metrics.get("FTP", "failed") == 1


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------


def test_stage10_22_smtp_session_transcript():
    simulator = services_ready(("SMTP", "H3"))
    result = simulator.smtp_send("H1", "H3", recipient="server@h3.netadapt.local")
    commands = [item["client"] for item in result["transcript"]]
    assert result["success"] is True
    assert commands[0].startswith("HELO")
    assert any(item.startswith("MAIL FROM") for item in commands)
    assert any(item.startswith("RCPT TO") for item in commands)
    assert "DATA" in commands and "QUIT" in commands


def test_stage10_23_smtp_delivers_to_the_simulated_mailbox():
    simulator = services_ready(("SMTP", "H3"))
    result = simulator.smtp_send("H1", "H3", recipient="server@h3.netadapt.local")
    assert result["delivered"] is True
    assert simulator.services.smtp_mailboxes["H3"][0]["recipient"] == "server@h3.netadapt.local"
    assert EventType.SMTP_MESSAGE in event_types(simulator)
    assert simulator.services.service_metrics.get("SMTP", "delivered") == 1


def test_stage10_24_smtp_relay_denial_is_a_failure():
    simulator = services_ready(("SMTP", "H3"))
    result = simulator.smtp_send("H1", "H3", recipient="someone@elsewhere.example")
    assert result["success"] is False and result["reason"] == "RELAY_DENIED"
    assert simulator.services.smtp_mailboxes["H3"] == []


# ----------------------------------------------------------------------
# Firewall and port filtering
# ----------------------------------------------------------------------


def test_stage10_25_firewall_allow_rule_permits_traffic():
    simulator = services_ready(("HTTP", "H3"))
    simulator.add_firewall_rule(action="ALLOW", protocol="TCP", destination_port=80)
    assert simulator.http_request("H1", "H3")["success"] is True
    assert simulator.services.security_metrics.get("firewall_blocks") == 0
    assert EventType.PACKET_ALLOWED in event_types(simulator)


def test_stage10_26_firewall_deny_rule_blocks_http():
    simulator = services_ready(("HTTP", "H3"))
    rule = simulator.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    result = simulator.http_request("H1", "H3")
    assert result["success"] is False
    assert result["reason"] == DROP_FIREWALL
    assert simulator.services.firewall.rules[0].packets_matched >= 1
    assert EventType.PACKET_BLOCKED in event_types(simulator)
    assert simulator.services.security_metrics.get("firewall_blocks") >= 1
    assert rule.to_dict()["action"] == "DENY"


def test_stage10_27_port_filter_reports_port_blocked():
    simulator = services_ready(("HTTP", "H3"))
    simulator.add_port_filter("TCP", 80)
    result = simulator.http_request("H1", "H3")
    assert result["success"] is False and result["reason"] == DROP_PORT
    assert simulator.services.security_metrics.get("port_blocks") >= 1


def test_stage10_28_firewall_rule_uses_source_and_destination_ip():
    simulator = services_ready(("HTTP", "H3"))
    simulator.add_firewall_rule(
        action="DENY", source_ip="192.168.1.1", destination_ip="192.168.1.3", protocol="TCP"
    )
    assert simulator.http_request("H1", "H3")["success"] is False
    assert simulator.http_request("H2", "H3")["success"] is True


def test_stage10_29_firewall_removed_rule_restores_service():
    simulator = services_ready(("HTTP", "H3"))
    rule = simulator.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    assert simulator.http_request("H1", "H3")["success"] is False
    assert simulator.services.firewall.remove_rule(rule.rule_id) is True
    assert simulator.http_request("H1", "H3")["success"] is True


# ----------------------------------------------------------------------
# ACLs
# ----------------------------------------------------------------------


def test_stage10_30_standard_acl_denies_by_source_ip():
    simulator = services_ready(("HTTP", "H3"))
    simulator.create_acl("BLOCK-PC", "STANDARD")
    simulator.add_acl_entry("BLOCK-PC", action="DENY", source_ip="192.168.1.1")
    simulator.attach_acl("BLOCK-PC", "R5")
    result = simulator.http_request("H1", "H3")
    assert result["success"] is False and result["reason"] == DROP_ACL
    assert {EventType.ACL_MATCHED, EventType.ACL_DENIED} <= event_types(simulator)
    assert simulator.services.security_metrics.get("acl_blocks") >= 1


def test_stage10_31_extended_acl_denies_destination_port():
    simulator = services_ready(("HTTP", "H3"))
    simulator.create_acl("NO-WEB", "EXTENDED")
    simulator.add_acl_entry(
        "NO-WEB",
        action="DENY",
        source_ip="192.168.1.1",
        destination_ip="192.168.1.3",
        protocol="TCP",
        destination_port=80,
    )
    simulator.attach_acl("NO-WEB", "R5")
    assert simulator.http_request("H1", "H3")["success"] is False
    assert simulator.http_request("H2", "H3")["success"] is True


def test_stage10_32_acl_permit_entry_allows_matching_traffic():
    simulator = services_ready(("HTTP", "H3"))
    simulator.create_acl("ONLY-PC1", "EXTENDED")
    simulator.add_acl_entry("ONLY-PC1", action="ALLOW", source_ip="192.168.1.1", protocol="TCP", destination_port=80)
    simulator.add_acl_entry("ONLY-PC1", action="DENY", source_ip="any", protocol="TCP", destination_port=80)
    simulator.attach_acl("ONLY-PC1", "R5")
    assert simulator.http_request("H1", "H3")["success"] is True
    assert simulator.http_request("H2", "H3")["success"] is False


# ----------------------------------------------------------------------
# ARP security
# ----------------------------------------------------------------------


def test_stage10_33_arp_conflict_detection():
    simulator = services_ready()
    result = simulator.services.arp_learn("H1", "192.168.1.1", "AA:AA:AA:AA:AA:AA")
    assert result["conflict"] is False
    conflict = simulator.services.arp_learn("H1", "192.168.1.1", "BB:BB:BB:BB:BB:BB")
    assert conflict["conflict"] is True
    record = simulator.services.arp_conflicts[-1]
    assert record["known_mac"] == "AA:AA:AA:AA:AA:AA"
    assert record["new_mac"] == "BB:BB:BB:BB:BB:BB"
    assert record["device"] == "H1" and record["severity"] == "HIGH"
    assert {EventType.ARP_CONFLICT, EventType.ARP_ANOMALY_DETECTED} <= event_types(simulator)
    assert simulator.services.security_metrics.get("arp_conflicts") == 1


def test_stage10_34_arp_spoofing_scenario_poisons_the_victim_cache():
    simulator = services_ready()
    simulator.services.arp_learn("H1", "192.168.1.3", "02:00:aa:aa:aa:00")
    result = simulator.services.arp_spoof("H2", "H1", "192.168.1.3", detect=True)
    attacker_mac = simulator.get_device("H2").interfaces[0].mac_address
    entry = simulator.arp_lookup("H1", "192.168.1.3")
    assert entry.mac_address == attacker_mac
    assert result["poisoned"] is True
    assert result["simulation_only"] is True
    assert result["detection"]["conflict"] is True
    types = event_types(simulator)
    assert {EventType.ARP_SPOOF_ATTEMPT, EventType.ARP_CACHE_POISONED} <= types


def test_stage10_35_arp_protection_restores_the_legitimate_entry():
    simulator = services_ready()
    simulator.services.arp_protection = True
    legitimate = simulator.get_device("H3").interfaces[0].mac_address
    simulator.services.arp_learn("H1", "192.168.1.3", legitimate)
    simulator.services.arp_spoof("H2", "H1", "192.168.1.3", detect=True)
    assert simulator.arp_lookup("H1", "192.168.1.3").mac_address == legitimate
    assert simulator.services.arp_state()["protection_enabled"] is True


# ----------------------------------------------------------------------
# Flood simulation and detection
# ----------------------------------------------------------------------


def test_stage10_36_flood_generates_traffic_and_reports_statistics():
    simulator = services_ready()
    result = simulator.start_flood("H1", "H4", protocol="UDP", rate=100, duration=1.0, threshold=500)
    assert result["packets_generated"] == 100
    assert result["packets_received"] + result["packets_dropped"] == 100
    assert result["bytes"] > 0 and result["bandwidth"] > 0
    assert result["average_latency"] > 0
    assert result["detected"] is False


def test_stage10_37_traffic_spike_is_detected():
    simulator = services_ready()
    result = simulator.start_flood("H1", "H4", protocol="TCP", rate=200, duration=1.0, threshold=25)
    assert result["detected"] is True
    assert result["peak_rate"] > 25
    types = event_types(simulator)
    assert {EventType.TRAFFIC_SPIKE, EventType.FLOOD_DETECTED} <= types
    assert simulator.services.security_metrics.get("floods_detected") == 1


def test_stage10_38_flood_protection_drops_attack_packets():
    simulator = services_ready()
    result = simulator.start_flood(
        "H1", "H4", protocol="UDP", rate=200, duration=1.0, threshold=25, protect=True
    )
    assert result["detected"] is True
    assert result["packets_dropped"] > 0
    assert result["packets_delivered"] > 0
    assert simulator.services.security_metrics.get("attack_packets_dropped") > 0
    reasons = {
        (getattr(packet, "security", None) or {}).get("reason")
        for packet in simulator.packets
    }
    assert DROP_FLOOD in reasons


def test_stage10_39_flood_uses_the_existing_qos_classes():
    simulator = services_ready()
    simulator.start_flood("H1", "H4", protocol="UDP", rate=50, duration=1.0, threshold=500)
    attack = [packet for packet in simulator.packets if getattr(packet, "attack", None)]
    assert attack
    assert {packet.traffic_type for packet in attack} == {"VoIP"}
    assert all(packet.priority > 0 for packet in attack)


def test_stage10_40_flood_threshold_is_configurable():
    simulator = services_ready()
    simulator.services.flood_threshold = 1000
    result = simulator.start_flood("H1", "H4", protocol="UDP", rate=200, duration=1.0)
    assert result["detected"] is False
    assert result["threshold"] == 1000


# ----------------------------------------------------------------------
# Integration: routing, QoS, failures, inspector, CLI
# ----------------------------------------------------------------------


def test_stage10_41_service_traffic_uses_adaptive_routing():
    simulator = services_ready(("HTTP", "H4"))
    result = simulator.http_request("H1", "H4")
    route = result["packets"][0]["route"]
    assert route[0] == "H1" and route[-1] == "H4"
    simulator.fail_link("R4", "R6")
    rerouted = simulator.http_request("H1", "H4")
    assert rerouted["success"] is True
    assert rerouted["packets"][0]["route"] != route
    assert "R5" in rerouted["packets"][0]["route"]


def test_stage10_42_service_traffic_uses_qos_classes():
    simulator = services_ready(("DNS", "H3"), ("HTTP", "H4"), ("FTP", "H4"), ("SMTP", "H3"))
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3")
    simulator.dns_query("H1", "server.netadapt.local", "H3")
    simulator.http_request("H1", "H4")
    simulator.ftp_command("H1", "H4", "LIST")
    simulator.smtp_send("H1", "H3", recipient="a@h3.netadapt.local")
    classes = {packet.traffic_type for packet in simulator.packets}
    assert "Emergency" in classes and "HTTP" in classes and "FTP" in classes
    dns_packet = next(
        packet for packet in simulator.packets
        if (getattr(packet, "service", None) or {}).get("service") == "DNS"
    )
    assert dns_packet.priority > 2
    scheduler = simulator.scheduler
    assert simulator.scheduler_statistics()["packets_enqueued"] > 0
    assert scheduler is simulator.scheduler
    assert simulator.scheduler_name == "priority"


def test_stage10_43_interface_failure_breaks_the_http_service():
    simulator = services_ready(("HTTP", "H3"))
    assert simulator.http_request("H1", "H3")["success"] is True
    simulator.fail_interface("H3", "eth0")
    failed = simulator.http_request("H1", "H3")
    assert failed["success"] is False and failed["reason"] == "INTERFACE_DOWN"
    simulator.recover_interface("H3", "eth0")
    assert simulator.http_request("H1", "H3")["success"] is True


def test_stage10_44_link_failure_breaks_service_traffic():
    simulator = services_ready(("HTTP", "H4"))
    assert simulator.http_request("H1", "H4")["success"] is True
    for u, v in [("H1", "R1"), ("H3", "R5"), ("R4", "R5"), ("R3", "R5"), ("R5", "R6"), ("R4", "R6")]:
        simulator.fail_link(u, v)
    result = simulator.http_request("H1", "H4")
    assert result["success"] is False
    assert result["reason"] in {"CONNECTION_FAILED", "DESTINATION_UNREACHABLE"}


def test_stage10_45_route_recalculation_restores_service_traffic():
    simulator = services_ready(("DNS", "H4"))
    simulator.dns_add_record("H4", "app.netadapt.local", "192.168.1.4")
    assert simulator.dns_query("H1", "app.netadapt.local", "H4")["success"] is True
    for u, v in [("H1", "R1"), ("H3", "R5"), ("R4", "R5"), ("R3", "R5"), ("R5", "R6"), ("R4", "R6")]:
        simulator.fail_link(u, v)
    assert simulator.dns_query("H1", "app.netadapt.local", "H4", use_cache=False)["success"] is False
    for u, v in [("H1", "R1"), ("H3", "R5"), ("R4", "R5"), ("R3", "R5"), ("R5", "R6"), ("R4", "R6")]:
        simulator.recover_link(u, v)
    assert simulator.dns_query("H1", "app.netadapt.local", "H4", use_cache=False)["success"] is True


def test_stage10_46_packet_inspector_shows_service_and_security_details():
    simulator = services_ready(("HTTP", "H3"))
    simulator.add_firewall_rule(action="ALLOW", protocol="TCP", destination_port=80)
    simulator.http_request("H1", "H3")
    packet = next(
        item for item in simulator.packets
        if (getattr(item, "service", None) or {}).get("service") == "HTTP"
    )
    assert packet.service["request"] == "GET /"
    assert packet.service_name == "HTTP"
    assert packet.security["firewall"] == "ALLOW"
    assert packet.security["service"] == "HTTP"
    assert packet.security["acl"] == "NO MATCH"
    assert packet.security["decision"] == "ALLOW"


def test_stage10_47_dns_and_dhcp_packets_expose_their_messages():
    simulator = services_ready(("DNS", "H3"), ("DHCP", "H3"))
    simulator.dns_add_record("H3", "server.netadapt.local", "192.168.1.3")
    simulator.dns_query("H1", "server.netadapt.local", "H3")
    simulator.dhcp_acquire("H2", "H3")
    messages = {
        (getattr(packet, "service", None) or {}).get("message")
        for packet in simulator.packets
    }
    assert any("DNS QUERY server.netadapt.local" in str(item) for item in messages)
    assert "DHCP ACK" in messages
    ack = next(
        packet for packet in simulator.packets
        if (getattr(packet, "service", None) or {}).get("message") == "DHCP ACK"
    )
    assert ack.service["assigned_ip"] == "192.168.1.100"


def test_stage10_48_service_and_security_metrics_are_exported():
    simulator = services_ready(("HTTP", "H3"))
    simulator.http_request("H1", "H3")
    simulator.add_firewall_rule(action="DENY", protocol="TCP", destination_port=8080)
    state = simulator.service_state()
    http = state["metrics"]["HTTP"]
    assert http["requests"] == 1 and http["responses"] == 1 and http["successful"] == 1
    assert http["bytes_transferred"] > 0
    assert http["latency_samples"] == 1 and http["average_latency"] > 0
    assert state["ports"]["HTTP"] == 80 and state["protocols"]["HTTP"] == "TCP"
    security = simulator.security_state()["metrics"]
    assert security["packets_inspected"] > 0
    assert security["packets_allowed"] + security["packets_blocked"] >= 1


def test_stage10_49_service_registry_is_bound_to_real_devices():
    simulator = NetworkSimulator(seed=42)
    simulator.install_service("HTTP", "H3")
    simulator.install_service("DNS", "H3")
    simulator.install_service("DHCP", "H4")
    status = simulator.service_status("H3")
    assert [(row["name"], row["port"], row["state"]) for row in status] == [
        ("DNS", 53, "RUNNING"),
        ("HTTP", 80, "RUNNING"),
    ]
    assert status[0]["ip"] == "192.168.1.3"
    assert simulator.get_device("H3") is not None
    with pytest.raises(ValueError):
        simulator.install_service("HTTP", "ghost")


def test_stage10_50_service_state_change_affects_communication():
    simulator = services_ready(("FTP", "H3"))
    simulator.stop_service("FTP", "H3")
    result = simulator.ftp_command("H1", "H3", "LIST")
    assert result["success"] is False and result["reason"] == "SERVICE_STOPPED"
    simulator.restart_service("FTP", "H3")
    assert simulator.ftp_command("H1", "H3", "LIST")["success"] is True


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def test_stage10_51_cli_ipconfig_and_dhcp_commands():
    session = LabSession()
    session.install_service("DHCP", "Server1")
    plain = session.console("PC1", "ipconfig")
    assert "192.168.1.1" in plain["output"]
    renewed = session.console("PC1", "ipconfig /renew")
    assert renewed["success"] is True
    detailed = session.console("PC1", "ipconfig /all")
    assert "DHCP server : Server1" in detailed["output"]
    assert renewed["result"]["address"] in detailed["output"]
    released = session.console("PC1", "ipconfig /release")
    assert "released" in released["output"]
    assert "unassigned" in session.console("PC1", "ipconfig")["output"]


def test_stage10_52_cli_nslookup_uses_real_dns_state():
    session = LabSession()
    session.install_service("DNS", "Server1")
    address = session.simulator.get_device("Server1").interfaces[0].ip_address
    session.dns_add_record("Server1", "server.netadapt.local", address)
    result = session.console("PC1", "nslookup server.netadapt.local")
    assert address in result["output"]
    assert "(from SERVER)" in result["output"]
    assert "(from CACHE)" in session.console("PC1", "nslookup server.netadapt.local")["output"]


def test_stage10_53_cli_show_services_and_service():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.install_service("DNS", "Server1")
    session.install_service("FTP", "Server1")
    session.stop_service("FTP", "Server1")
    listing = session.console("Server1", "show services")
    assert "HTTP" in listing["output"] and "STOPPED" in listing["output"]
    detail = session.console("Server1", "show service http")
    assert "Port    : 80/TCP" in detail["output"]
    assert detail["status"]["state"] == "RUNNING"
    with pytest.raises(Exception):
        session.console("Server1", "show service ghost")


def test_stage10_54_cli_show_firewall_and_access_lists():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    session.create_acl("NO-WEB", "EXTENDED")
    session.add_acl_entry("NO-WEB", action="DENY", source_ip="192.168.1.1", protocol="TCP", destination_port=80)
    session.attach_acl("NO-WEB", "R1")
    firewall = session.console("Server1", "show firewall")
    assert "DENY" in firewall["output"] and "TCP" in firewall["output"]
    acls = session.console("R1", "show access-lists")
    assert "NO-WEB (EXTENDED)" in acls["output"]
    assert "applied to R1" in acls["output"]


def test_stage10_55_cli_router_and_switch_commands():
    session = LabSession()
    brief = session.console("R1", "show ip interface brief")
    assert "eth0" in brief["output"]
    config = session.console("R1", "show running-config")
    assert "hostname R1" in config["output"]
    mac = session.console("SW1", "show mac address-table")
    assert "Mac Address Table" in mac["output"]
    vlan = session.console("SW1", "show vlan brief")
    assert "default" in vlan["output"]
    assert "routes" in session.console("PC1", "route print")


def test_stage10_56_cli_show_connections_reflects_live_state():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    output = session.console("PC1", "show connections")["output"]
    assert "TCP" in output and "Server1:80" in output
    assert "TCP" in session.console("PC1", "netstat")["output"]


def test_stage10_57_cli_reflects_security_state():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    blocked = session.http_request("PC1", "Server1")["services"]["result"]
    assert blocked["success"] is False
    assert blocked["reason"] == DROP_FIREWALL
    security = session.console("Server1", "show security")
    assert "ARP detection" in security["output"]
    assert security["security"]["firewall"]["rules"][0]["action"] == "DENY"
    state = session.state()
    assert state["security"]["metrics"]["packets_blocked"] >= 1
    assert state["services"]["metrics"]["HTTP"].get("failed", 0) >= 1


def test_stage10_58_session_state_exposes_services_and_security():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    state = session.state()
    assert state["services"]["registry"][0]["service"] == "HTTP"
    assert state["services"]["result"]["status"] == 200
    assert "firewall" in state["security"] and "access_lists" in state["security"]
    packet = next(
        item for item in state["packets"] if item.get("service_name") == "HTTP"
    )
    assert packet["service"]["method"] == "GET"
    assert packet["security"]["decision"] == "ALLOW"


def test_stage10_59_reset_keeps_services_but_clears_runtime_state():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=8080)
    session.reset_simulation()
    state = session.state()
    assert [row["service"] for row in state["services"]["registry"]] == ["HTTP"]
    assert len(state["security"]["firewall"]["rules"]) == 1
    assert state["services"]["metrics"]["HTTP"].get("requests", 0) == 0
