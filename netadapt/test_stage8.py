"""Stage 8 tests for diagnostics, cache inspection, and packet journeys."""

import pytest

from lab_session import LabSession
from protocols import SwitchFrame


def test_ping_success_uses_real_route_and_four_icmp_packets():
    session = LabSession(seed=42)
    state = session.run_ping("PC1", "PC2", count=4)
    result = state["diagnostic_result"]

    assert result["title"] == "PING PC1 → PC2"
    assert result["sent"] == 4
    assert result["received"] == 4
    assert result["lost"] == 0
    assert result["loss_percent"] == 0.0
    assert result["path"] == ["PC1", "SW1", "R1", "R2", "SW2", "PC2"]
    assert result["rtt_ms"]["min"] == pytest.approx(32.0)
    assert result["rtt_ms"]["average"] == pytest.approx(32.0)
    assert result["rtt_ms"]["max"] == pytest.approx(32.0)
    assert len(result["packet_results"]) == 4
    assert all(packet["protocol"] == "ICMP" for packet in result["packet_results"])


def test_ping_failure_reports_destination_unreachable_from_live_state():
    session = LabSession(seed=42)
    session.simulator.fail_link("PC1", "SW1")
    result = session.run_ping("PC1", "PC2", count=4)["diagnostic_result"]

    assert result["success"] is False
    assert result["received"] == 0
    assert result["lost"] == 4
    assert result["loss_percent"] == 100.0
    assert result["reason"] == "DESTINATION_UNREACHABLE"
    assert all(packet["drop_reason"] == "DESTINATION_UNREACHABLE" for packet in result["packet_results"])


def test_ping_packet_loss_is_drawn_from_configured_link_conditions():
    session = LabSession(seed=42)
    session.configure_link("PC1", "SW1", {"packet_loss": 1.0})
    result = session.run_ping("PC1", "PC2", count=4)["diagnostic_result"]

    assert result["sent"] == 4
    assert result["received"] == 0
    assert result["loss_percent"] == 100.0
    assert result["reason"] == "PACKET_LOSS"
    assert all(packet["drop_reason"] == "PACKET_LOSS" for packet in result["packet_results"])


def test_traceroute_returns_each_forwarded_hop_with_actual_response_time():
    session = LabSession(seed=42)
    result = session.run_traceroute("PC1", "PC2")["diagnostic_result"]

    assert result["success"] is True
    assert result["path"] == ["PC1", "SW1", "R1", "R2", "SW2", "PC2"]
    assert [hop["device"] for hop in result["hops"]] == ["SW1", "R1", "R2", "SW2", "PC2"]
    assert [hop["hop"] for hop in result["hops"]] == [1, 2, 3, 4, 5]
    assert all(hop["status"] == "REPLIED" for hop in result["hops"])
    assert all(hop["ip"] for hop in result["hops"])
    response_times = [hop["response_time_ms"] for hop in result["hops"]]
    assert response_times == sorted(response_times)
    assert response_times[-1] == pytest.approx(32.0)


def test_arp_table_inspection_reports_dynamically_learned_entries():
    session = LabSession(seed=42)
    session.run_ping("PC1", "PC2", count=1)
    result = session.inspect_arp("PC1")["diagnostic_result"]

    assert result["device"] == "PC1"
    assert result["entries"]
    for entry in result["entries"]:
        assert entry["learned_dynamically"] is True
        assert entry["state"] == "REACHABLE"
        assert entry["ip_address"]
        assert entry["mac_address"]
        assert entry["interface_id"]


def test_clear_arp_affects_the_actual_protocol_cache():
    session = LabSession(seed=42)
    session.run_ping("PC1", "PC2", count=1)
    assert session.simulator.arp.to_dict()["PC1"]

    result = session.clear_arp("PC1")["diagnostic_result"]

    assert result["entries"] == []
    assert result["cleared"] > 0
    assert "PC1" not in session.simulator.arp.to_dict()
    assert any(event["event"] == "ARP_CLEARED" for event in session.state()["events"])


def test_mac_table_inspection_reports_dynamic_learning():
    session = LabSession(seed=42)
    source_mac = session.simulator.get_device("PC1").interfaces[0].mac_address
    session.simulator.switch_frame(
        "SW1",
        SwitchFrame(source_mac, "ff:ff:ff:ff:ff:ff", "eth0"),
    )
    result = session.inspect_mac("SW1")["diagnostic_result"]

    assert result["entries"] == [
        {"mac_address": source_mac, "interface_id": "eth0", "type": "DYNAMIC"}
    ]
    assert session.simulator.mac_table_lookup("SW1", source_mac) == "eth0"


def test_clear_mac_table_affects_the_actual_switch_state():
    session = LabSession(seed=42)
    source_mac = session.simulator.get_device("PC1").interfaces[0].mac_address
    session.simulator.switch_frame(
        "SW1",
        SwitchFrame(source_mac, "ff:ff:ff:ff:ff:ff", "eth0"),
    )
    result = session.clear_mac("SW1")["diagnostic_result"]

    assert result["entries"] == []
    assert result["cleared"] == 1
    assert session.simulator.get_device("SW1").mac_table == {}
    assert session.simulator.mac_table_lookup("SW1", source_mac) is None
    assert any(event["event"] == "MAC_TABLE_CLEARED" for event in session.state()["events"])


def test_packet_journey_is_built_from_event_logger_history():
    session = LabSession(seed=42)
    result = session.run_ping("PC1", "PC2", count=1)["diagnostic_result"]
    packet = result["packet_results"][0]
    journey_events = [event["event"] for event in packet["journey"]]

    assert journey_events[0] == "PACKET_GENERATED"
    assert "ARP_CACHE_UPDATE" in journey_events
    assert "PACKET_FORWARDED" in journey_events
    assert "PACKET_DELIVERED" in journey_events
    assert "ICMP_ECHO_REPLY" in journey_events
    assert all(
        event["details"].get("packet_id") == packet["id"]
        for event in packet["journey"]
    )


def test_packet_drop_reason_is_exposed_by_inspector_state():
    session = LabSession(seed=42)
    session.configure_link("PC1", "SW1", {"packet_loss": 1.0})
    state = session.run_ping("PC1", "PC2", count=1)
    packet = state["diagnostic_result"]["packet_results"][0]

    assert packet["status"] == "DROPPED"
    assert packet["drop_reason"] == "PACKET_LOSS"
    assert state["last_packet"]["drop_reason"] == "PACKET_LOSS"
    drop_event = next(
        event for event in packet["journey"] if event["event"] == "PACKET_DROPPED"
    )
    assert drop_event["details"]["reason"] == "PACKET_LOSS"


def test_diagnostic_results_match_live_simulator_packet_registry():
    session = LabSession(seed=42)
    state = session.run_ping("PC1", "PC2", count=3)
    result_ids = [packet["id"] for packet in state["diagnostic_result"]["packet_results"]]
    registry_ids = list(session.simulator.protocols.protocol_packets)
    state_ids = [packet["id"] for packet in state["packets"]]

    assert result_ids == registry_ids
    assert state_ids[-3:] == result_ids
    assert all(packet["flow_id"] for packet in state["packets"] if packet["id"] in result_ids)
