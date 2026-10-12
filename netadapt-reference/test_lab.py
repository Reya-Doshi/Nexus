"""Integration tests for the editable network laboratory backend."""

import json

from lab_session import LabError, LabSession
from qos import PRIORITIES


def test_default_demo_is_not_the_legacy_fixed_topology():
    session = LabSession()
    state = session.state()
    assert state["name"] == "Self-Healing Demo"
    assert {device["id"] for device in state["devices"]} >= {"PC1", "SW1", "R1", "R2", "PC2"}
    assert "H1" not in {device["id"] for device in state["devices"]}


def test_add_move_rename_duplicate_and_delete_device_change_simulator():
    session = LabSession()
    session.new_network()
    session.add_device("pc", 100, 100, "ClientA")
    device = session.simulator.topology.get_device("ClientA")
    assert device is not None
    session.move_device("ClientA", 240, 180)
    session.rename_device("ClientA", "Workstation")
    assert "Workstation" in session.simulator.topology.devices
    assert "ClientA" not in session.simulator.topology.devices
    session.duplicate_device("Workstation")
    assert "Workstation_copy" in session.simulator.topology.devices
    session.delete_device("Workstation_copy")
    assert "Workstation_copy" not in session.simulator.topology.devices


def test_add_delete_and_configure_link_affect_live_topology():
    session = LabSession()
    session.new_network()
    session.add_device("router", 300, 120, "Core")
    session.add_device("host", 600, 120, "Client")
    session.add_link("Core", "Client", latency=12, bandwidth=25)
    assert session.simulator.topology.graph["Core"]["Client"]["latency"] == 12
    session.configure_link("Core", "Client", {"bandwidth": 10, "latency": 20, "packet_loss": 0.25, "congestion": 0.75, "status": "DOWN"})
    edge = session.simulator.topology.graph["Core"]["Client"]
    assert edge["bandwidth"] == 10
    assert edge["latency"] == 20
    assert edge["packet_loss"] == 0.25
    assert edge["congestion"] == 0.75
    assert edge["status"] == "DOWN"
    session.delete_link("Core", "Client")
    assert not session.simulator.topology.graph.has_edge("Core", "Client")


def test_interface_configuration_updates_backend_device_model():
    session = LabSession()
    session.configure_interface("PC1", "eth0", {"ip_address": "192.168.50.10/26", "status": "UP"})
    interface = session.simulator.get_device("PC1").get_interface("eth0")
    assert interface.ip_address == "192.168.50.10"
    assert interface.prefix == 26
    assert interface.network_address == "192.168.50.0"
    assert interface.status == "UP"


def test_device_failure_uses_existing_self_healing_path():
    session = LabSession(seed=42)
    session.simulator.failure_detection_timeout = 1.0
    session.start_traffic("PC1", "PC2", packet_count=2, pps=2)
    session.shutdown_device("R2")
    session.simulator.tick(1.1)
    assert session.simulator.get_device("R2").status == "DOWN"
    assert any(event["event"] == "FAILURE_DETECTED" for event in session.state()["events"])
    session.restart_device("R2")
    assert session.simulator.get_device("R2").status == "UP"


def test_traffic_and_step_return_real_packet_and_metrics():
    session = LabSession()
    session.start_traffic("PC1", "PC2", packet_count=3, pps=5, traffic_type="Video")
    state = session.step()
    assert state["last_packet"]["source"] == "PC1"
    assert state["last_packet"]["destination"] == "PC2"
    assert state["last_packet"]["status"] in {"DELIVERED", "DROPPED"}
    assert state["metrics"]["sent"] == 1
    assert state["metrics"]["delivered"] + state["metrics"]["dropped"] == 1


def test_failure_and_recovery_are_visible_in_lab_state():
    session = LabSession()
    session.simulator.failure_detection_timeout = 0.0
    session.start_traffic("PC1", "PC2", packet_count=1, pps=1)
    session.shutdown_device("R2")
    assert session.state()["devices"][3]["status"] == "DOWN"
    session.restart_device("R2")
    assert session.state()["devices"][3]["status"] == "UP"


def test_serialization_round_trip_preserves_devices_links_positions_and_conditions():
    session = LabSession()
    session.move_device("R1", 333, 222)
    session.configure_link("R1", "R2", {"bandwidth": 17, "latency": 33, "packet_loss": 0.1, "congestion": 0.2})
    document = session.export_document()
    restored = LabSession()
    restored.import_document(json.loads(json.dumps(document)), record=False)
    assert restored.state()["devices"] == session.state()["devices"]
    assert restored.state()["links"] == session.state()["links"]


def test_undo_and_redo_restore_topology_and_conditions():
    session = LabSession()
    original = {device["id"] for device in session.state()["devices"]}
    session.add_device("server", 500, 300, "App")
    assert "App" in {device["id"] for device in session.state()["devices"]}
    session.undo()
    assert {device["id"] for device in session.state()["devices"]} == original
    session.redo()
    assert "App" in {device["id"] for device in session.state()["devices"]}


def test_console_commands_return_backend_data():
    session = LabSession()
    output = session.console("R1", "show interfaces")["output"]
    assert "eth0" in output
    route = session.console("R1", "show ip route")
    assert "via" in route["output"] or route["output"] == "No routes"
    assert "192.168" in session.console("PC1", "ipconfig")["output"]


def test_preset_and_new_network_are_reusable():
    session = LabSession()
    assert len(session.state()["devices"]) > 3
    session.load_preset("simple_lan")
    assert session.state()["name"] == "Simple LAN"
    session.new_network()
    assert session.state()["devices"] == []
    assert session.state()["links"] == []


def test_scheduler_and_qos_configuration_change_live_engine_and_survive_reload():
    original_priorities = dict(PRIORITIES)
    try:
        session = LabSession()
        session.set_scheduler("wfq")
        session.set_qos_config(
            priorities={"Emergency": 9, "VoIP": 6, "Video": 4, "HTTP": 2, "FTP": 1},
            weights={"Emergency": 8, "VoIP": 6, "Video": 4, "HTTP": 2, "FTP": 1},
        )
        state = session.state()
        assert state["qos"]["scheduler"] == "wfq"
        assert state["qos"]["priorities"]["Emergency"] == 9
        assert state["qos"]["weights"]["Emergency"] == 8

        restored = LabSession()
        restored.import_document(session.export_document(), record=False)
        restored_state = restored.state()
        assert restored_state["qos"]["scheduler"] == "wfq"
        assert restored_state["qos"]["priorities"] == state["qos"]["priorities"]
        assert restored_state["qos"]["weights"] == state["qos"]["weights"]
    finally:
        PRIORITIES.clear()
        PRIORITIES.update(original_priorities)


def test_traffic_validation_rejects_invalid_endpoints_and_values():
    session = LabSession()
    for args in [
        ("missing", "PC2", 1, 1, "HTTP", 1000),
        ("PC1", "PC1", 1, 1, "HTTP", 1000),
        ("PC1", "PC2", 0, 1, "HTTP", 1000),
        ("PC1", "PC2", 1, 1, "INVALID", 1000),
    ]:
        try:
            session.start_traffic(*args)
        except LabError:
            pass
        else:
            raise AssertionError(f"Expected invalid traffic request to fail: {args}")


def test_editing_large_topology_remains_structurally_responsive():
    session = LabSession()
    session.new_network()
    for index in range(50):
        session.add_device("pc" if index % 2 else "router", 40 + (index % 10) * 100, 40 + (index // 10) * 90, f"N{index}")
    state = session.state()
    assert len(state["devices"]) == 50
    assert len(state["routing_tables"]) == 25


def test_reset_clears_diagnostic_packet_state():
    session = LabSession()
    session.run_ping("PC1", "Server1", count=2)
    assert session.state()["packets"]
    measured = session.state()["metrics"]
    assert measured["sent"] == 2
    assert measured["delivered"] == 2
    assert measured["pdr"] == 100.0
    reset = session.reset_simulation()
    assert reset["packets"] == []
    assert reset["last_packet"] is None
    assert reset["diagnostic_result"] is None
    assert reset["metrics"]["sent"] == 0
    assert reset["metrics"]["delivered"] == 0
    assert reset["metrics"]["dropped"] == 0
    assert reset["events"][-1]["event"] == "SIMULATION_RESET"
