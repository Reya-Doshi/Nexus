"""Stage 9 acceptance tests for NetAdapt's simulated TCP and UDP layer."""

from lab_session import LabSession
from simulator import NetworkSimulator
from transport import TCPState, compare_transport


def tcp_ready(**kwargs):
    simulator_keys = {
        "topology",
        "seed",
        "scheduler",
        "scheduler_params",
        "routing_algorithm",
        "routing_weights",
        "heartbeat_interval",
        "failure_detection_timeout",
    }
    simulator_args = {key: value for key, value in kwargs.items() if key in simulator_keys}
    simulator_args.setdefault("seed", 42)
    simulator = NetworkSimulator(**simulator_args)
    connection = simulator.create_tcp_connection(
        "H1",
        "H3",
        **{key: value for key, value in kwargs.items() if key not in simulator_keys},
    )
    simulator.run_until_empty()
    return simulator, connection


def udp_ready(count=1, **kwargs):
    simulator_keys = {"seed", "scheduler", "scheduler_params"}
    simulator_args = {key: value for key, value in kwargs.items() if key in simulator_keys}
    simulator_args.setdefault("seed", 42)
    simulator = NetworkSimulator(**simulator_args)
    flow = simulator.create_udp_flow(
        "H1",
        "H3",
        **{key: value for key, value in kwargs.items() if key not in simulator_keys},
    )
    simulator.send_udp_data(flow.flow_id, count, 1000)
    simulator.run_until_empty()
    return simulator, flow


def test_stage9_01_udp_flow_creation():
    simulator = NetworkSimulator(seed=42)
    flow = simulator.create_udp_flow("H1", "H3", 5300, 5301)
    assert (flow.flow_id, flow.source_port, flow.destination_port) == (
        "UDP-001",
        5300,
        5301,
    )


def test_stage9_02_udp_packet_delivery():
    _, flow = udp_ready(2)
    assert (flow.packets_delivered, flow.bytes_delivered) == (2, 2000)


def test_stage9_03_udp_packet_loss():
    simulator = NetworkSimulator(seed=42)
    flow = simulator.create_udp_flow("H1", "H3")
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_udp_data(flow.flow_id, 2)
    simulator.run_until_empty()
    assert (flow.packets_lost, flow.packets_delivered) == (2, 0)


def test_stage9_04_udp_does_not_retransmit():
    simulator = NetworkSimulator(seed=42)
    flow = simulator.create_udp_flow("H1", "H3")
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_udp_data(flow.flow_id)
    simulator.run_until_empty()
    simulator.tick(1.0)
    assert simulator.get_transport_statistics()["UDP"]["retransmissions"] == 0
    assert len(flow.packets) == 1


def test_stage9_05_tcp_syn_packet():
    simulator = NetworkSimulator(seed=42)
    connection = simulator.create_tcp_connection("H1", "H3")
    assert connection.packets[0].flags == ["SYN"]
    assert connection.packets[0].sequence_number == 0


def test_stage9_06_tcp_syn_ack_packet():
    _, connection = tcp_ready()
    packet = next(item for item in connection.packets if item.kind == "CONTROL" and item.flags == ["SYN", "ACK"])
    assert packet.acknowledgement_number == 1


def test_stage9_07_tcp_final_handshake_ack():
    _, connection = tcp_ready()
    assert any(
        item.kind == "CONTROL" and item.flags == ["ACK"] and item.sequence_number == 1
        for item in connection.packets
    )


def test_stage9_08_tcp_handshake_completion():
    simulator, connection = tcp_ready()
    assert connection.state == TCPState.ESTABLISHED
    assert connection.setup_completed is not None
    assert simulator.get_tcp_state(connection.flow_id) == "ESTABLISHED"


def test_stage9_09_tcp_sequence_numbers():
    simulator, connection = tcp_ready(initial_cwnd=4)
    simulator.send_tcp_data(connection.flow_id, 4)
    assert [item.sequence_number for item in connection.packets if item.kind == "DATA"] == [1, 2, 3, 4]


def test_stage9_10_tcp_cumulative_ack_processing():
    simulator, connection = tcp_ready(initial_cwnd=2)
    simulator.send_tcp_data(connection.flow_id, 2)
    simulator.run_until_empty()
    assert connection.cumulative_ack == 3
    assert connection.highest_ack == 3


def test_stage9_11_tcp_sliding_window():
    simulator, connection = tcp_ready(initial_cwnd=2, receiver_window=4)
    assert len(simulator.send_tcp_data(connection.flow_id, 5)) == 2
    assert connection.packets_in_flight == 2
    assert connection.to_dict()["send_window"] == 2


def test_stage9_12_tcp_receiver_window_flow_control():
    simulator, connection = tcp_ready(initial_cwnd=8, receiver_window=2)
    assert connection.effective_window == 2
    assert len(simulator.send_tcp_data(connection.flow_id, 8)) == 2


def test_stage9_13_tcp_rtt_uses_simulation_clock():
    simulator, connection = tcp_ready()
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert len(connection.rtt_samples) == 1
    assert connection.rtt_samples[0] > 0
    assert connection.to_dict()["average_rtt_ms"] > 0


def test_stage9_14_tcp_timeout_occurs():
    simulator, connection = tcp_ready(timeout=0.1)
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    simulator.set_packet_loss("H1", "R1", 0.0)
    simulator.tick(0.2)
    assert connection.timeout_count == 1


def test_stage9_15_tcp_retransmission_after_loss():
    simulator, connection = tcp_ready(timeout=0.1)
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    simulator.set_packet_loss("H1", "R1", 0.0)
    simulator.tick(0.2)
    simulator.run_until_empty()
    assert connection.retransmission_count == 1
    assert connection.cumulative_ack == 2


def test_stage9_16_tcp_slow_start():
    simulator, connection = tcp_ready(initial_cwnd=1, ssthresh=8)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert connection.cwnd == 2
    assert connection.congestion_phase == "SLOW_START"


def test_stage9_17_tcp_congestion_avoidance():
    simulator, connection = tcp_ready(initial_cwnd=2, ssthresh=1)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert connection.cwnd == 2.5
    assert connection.congestion_phase == "CONGESTION_AVOIDANCE"


def test_stage9_18_tcp_cwnd_reduces_after_timeout():
    simulator, connection = tcp_ready(initial_cwnd=4, ssthresh=16)
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    simulator.set_packet_loss("H1", "R1", 0.0)
    simulator.tick(0.2)
    assert (connection.cwnd, connection.ssthresh, connection.congestion_phase) == (
        1.0,
        2.0,
        "SLOW_START",
    )


def test_stage9_19_tcp_fin_packet():
    simulator, connection = tcp_ready()
    packet = simulator.close_tcp_connection(connection.flow_id)
    assert packet.kind == "FIN" and "FIN" in packet.flags


def test_stage9_20_tcp_connection_closes():
    simulator, connection = tcp_ready()
    simulator.close_tcp_connection(connection.flow_id)
    simulator.run_until_empty()
    assert connection.state == TCPState.CLOSED
    assert any(
        item.kind == "FIN" and item.source == connection.destination
        for item in connection.packets
    )


def test_stage9_21_tcp_metrics():
    simulator, connection = tcp_ready()
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    metrics = simulator.get_transport_statistics()["TCP"]
    assert metrics["packets_sent"] >= 5
    assert metrics["ack_count"] == 1
    assert metrics["packets_in_flight"] == 0


def test_stage9_22_udp_metrics():
    _, flow = udp_ready(3)
    metrics = flow.to_dict()
    assert metrics["packets_sent"] == 3
    assert metrics["packets_delivered"] == 3
    assert metrics["throughput"] > 0
    assert metrics["average_latency"] > 0
    assert metrics["average_rtt_ms"] is None


def test_stage9_23_packet_inspector_matches_simulator():
    session = LabSession()
    session.create_transport("TCP", "PC1", "PC2", source_port=5000, destination_port=8080)
    state = session.state()
    packet = next(item for item in state["packets"] if item["protocol"] == "TCP")
    assert packet["source_port"] == 8080 or packet["source_port"] == 5000
    assert packet["flags"]
    assert packet["flow_id"].startswith("TCP-")
    assert packet["queue_wait_time"] is not None


def test_stage9_24_tcp_with_packet_loss():
    simulator, connection = tcp_ready()
    simulator.set_packet_loss("H1", "R1", 1.0)
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert connection.data_packets_lost == 1
    assert connection.outstanding


def test_stage9_25_tcp_with_link_failure():
    simulator, connection = tcp_ready()
    original = list(connection.route)
    simulator.fail_link(original[1], original[2])
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert connection.state == TCPState.ESTABLISHED
    assert connection.route != original


def test_stage9_26_tcp_route_recalculation_is_logged():
    simulator, connection = tcp_ready()
    original = list(connection.route)
    simulator.fail_link(original[1], original[2])
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert any(
        event.event_type.value == "ROUTE_RECALCULATED" and event.flow_id == connection.flow_id
        for event in simulator.event_logger.events
    )


def test_stage9_27_udp_route_recalculation():
    simulator, flow = udp_ready()
    original = list(flow.route)
    simulator.fail_link(original[1], original[2])
    simulator.send_udp_data(flow.flow_id)
    simulator.run_until_empty()
    assert flow.route != original
    assert flow.packets[-1].route == flow.route


def test_stage9_28_qos_integration_with_tcp():
    simulator, connection = tcp_ready(scheduler="priority")
    simulator.send_tcp_data(connection.flow_id)
    simulator.run_until_empty()
    assert simulator.scheduler.queue_statistics()["packets_served"] >= 4
    assert connection.packets[-1].traffic_class == "HTTP"


def test_stage9_29_qos_integration_with_udp():
    simulator, flow = udp_ready(scheduler="wfq")
    assert flow.packets[-1].traffic_class == "VoIP"
    assert simulator.scheduler.queue_statistics()["packets_served"] >= 1


def test_stage9_30_deterministic_tcp_udp_comparison():
    first = compare_transport(NetworkSimulator(seed=17), "H1", "H3", 4, 1000, seed=17)
    second = compare_transport(NetworkSimulator(seed=17), "H1", "H3", 4, 1000, seed=17)
    assert first == second
    assert {row["protocol"] for row in first["results"]} == {"TCP", "UDP"}
    assert all(row["average_latency"] > 0 for row in first["results"])


def test_stage9_31_cli_transport_information():
    session = LabSession()
    session.create_transport("TCP", "PC1", "PC2")
    result = session.console("PC1", "netstat")
    assert "TCP-001" in result["output"]
    assert result["flows"][0]["state"] == "ESTABLISHED"


def test_stage9_32_destination_unreachable_does_not_fake_delivery():
    simulator = NetworkSimulator(seed=42)
    simulator.fail_link("H3", "R5")
    connection = simulator.create_tcp_connection("H1", "H3")
    simulator.run_until_empty()
    assert connection.state != TCPState.ESTABLISHED
    assert all(packet.status != "DELIVERED" for packet in connection.packets)


def test_stage9_33_four_way_fin_exchange():
    simulator, connection = tcp_ready()
    simulator.close_tcp_connection(connection.flow_id)
    simulator.run_until_empty()
    states = [entry["state"] for entry in connection.state_history]
    assert "FIN_WAIT" in states and "TIME_WAIT" in states
    assert states[-1] == "CLOSED"
    assert len([packet for packet in connection.packets if packet.kind == "FIN"]) == 2
