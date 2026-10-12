"""Comprehensive Stage 8 transport-layer simulation tests."""
import pytest
from simulator import NetworkSimulator
from transport import TCPState, compare_transport


def tcp_ready(**kwargs):
    sim = NetworkSimulator(seed=42)
    connection = sim.create_tcp_connection("H1", "H3", **kwargs)
    sim.run_until_empty()
    return sim, connection


def udp_ready(count=1, **kwargs):
    sim = NetworkSimulator(seed=42)
    flow = sim.create_udp_flow("H1", "H3", **kwargs)
    sim.send_udp_data(flow.flow_id, count, 1000)
    sim.run_until_empty()
    return sim, flow


def test_udp_flow_creation():
    sim = NetworkSimulator(); flow = sim.create_udp_flow("H1", "H3")
    assert flow.flow_id == "UDP-001" and flow.source_port == 5001


def test_udp_delivery():
    sim, flow = udp_ready(); assert flow.bytes_delivered == 1000


def test_udp_packet_loss():
    sim = NetworkSimulator(); flow = sim.create_udp_flow("H1", "H3"); sim.set_packet_loss("H1", "R1", 1.0); sim.send_udp_data(flow.flow_id); sim.run_until_empty(); assert sim.get_transport_statistics()["UDP"]["packets_lost"] == 1


def test_udp_has_no_retransmission():
    sim, flow = udp_ready(); assert sim.get_transport_statistics()["UDP"]["retransmissions"] == 0


def test_tcp_syn_generation():
    sim = NetworkSimulator(); connection = sim.create_tcp_connection("H1", "H3"); assert connection.packets[0].flags == ["SYN"]


def test_tcp_syn_ack_generation():
    sim, connection = tcp_ready(); assert any(p.flags == ["SYN", "ACK"] for p in connection.packets)


def test_tcp_final_ack_generation():
    sim, connection = tcp_ready(); assert any(p.kind == "CONTROL" and p.flags == ["ACK"] for p in connection.packets)


def test_tcp_handshake_completion():
    sim, connection = tcp_ready(); assert connection.state == TCPState.ESTABLISHED and connection.setup_completed is not None


def test_tcp_sequence_numbers():
    sim, connection = tcp_ready(); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert [p.sequence_number for p in connection.packets if p.kind == "DATA"] == [1]


def test_tcp_ack_processing():
    sim, connection = tcp_ready(); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.cumulative_ack == 2 and connection.ack_count == 1


def test_tcp_sliding_window_limits_sender():
    sim, connection = tcp_ready(initial_cwnd=2, receiver_window=4); assert len(sim.send_tcp_data(connection.flow_id, 8)) == 2


def test_tcp_receiver_window_limits_effective_window():
    sim, connection = tcp_ready(initial_cwnd=8, receiver_window=1); assert connection.effective_window == 1 and len(sim.send_tcp_data(connection.flow_id, 4)) == 1


def test_tcp_timeout_retransmits_lost_data():
    sim, connection = tcp_ready(timeout=.1); sim.set_packet_loss("H1", "R1", 1.0); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); sim.set_packet_loss("H1", "R1", 0.0); sim.tick(.2); assert connection.timeout_count == 1 and connection.retransmission_count == 1


def test_tcp_retransmission_uses_same_sequence_number():
    sim, connection = tcp_ready(); sim.set_packet_loss("H1", "R1", 1.0); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); original = connection.outstanding[1].packet.sequence_number; sim.set_packet_loss("H1", "R1", 0.0); sim.tick(.2); assert connection.outstanding[1].packet.sequence_number == original


def test_tcp_rtt_uses_simulation_time():
    sim, connection = tcp_ready(); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.rtt_samples and connection.rtt_samples[0] > 0


def test_tcp_slow_start_increases_cwnd():
    sim, connection = tcp_ready(initial_cwnd=1, ssthresh=16); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.cwnd == 2 and connection.congestion_phase == "SLOW_START"


def test_tcp_congestion_avoidance_phase():
    sim, connection = tcp_ready(initial_cwnd=2, ssthresh=1); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.cwnd == 2.5 and connection.congestion_phase == "CONGESTION_AVOIDANCE"


def test_tcp_congestion_window_reduces_after_timeout():
    sim, connection = tcp_ready(initial_cwnd=4, ssthresh=16); sim.set_packet_loss("H1", "R1", 1.0); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); sim.set_packet_loss("H1", "R1", 0.0); sim.tick(.2); assert connection.cwnd == 1 and connection.ssthresh == 2


def test_tcp_fin_packet_is_generated():
    sim, connection = tcp_ready(); packet = sim.close_tcp_connection(connection.flow_id); assert "FIN" in packet.flags


def test_tcp_connection_closes_after_fin_ack():
    sim, connection = tcp_ready(); sim.close_tcp_connection(connection.flow_id); sim.run_until_empty(); assert connection.state == TCPState.CLOSED


def test_tcp_connection_state_history_is_real():
    sim, connection = tcp_ready(); assert [item["state"] for item in connection.state_history][-1] == "ESTABLISHED"


def test_tcp_metrics_are_separate_from_udp():
    sim, connection = tcp_ready(); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); flow = sim.create_udp_flow("H1", "H3"); sim.send_udp_data(flow.flow_id); sim.run_until_empty(); stats = sim.get_transport_statistics(); assert stats["TCP"]["packets_sent"] > 0 and stats["UDP"]["packets_sent"] == 1


def test_udp_metrics_report_throughput_and_latency():
    sim, flow = udp_ready(2); stats = sim.get_transport_statistics()["UDP"]; assert stats["throughput"] > 0 and stats["average_latency"] > 0


def test_tcp_packet_inspector_info():
    sim, connection = tcp_ready(); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); packet = next(item.to_dict() for item in connection.packets if item.kind == "DATA"); assert packet["protocol"] == "TCP" and packet["source_port"] == 5000 and packet["acknowledgement_number"] == 0


def test_udp_packet_inspector_info():
    sim, flow = udp_ready(); packet = flow.packets[0].to_dict(); assert packet["protocol"] == "UDP" and packet["payload_size"] == 1000


def test_tcp_packet_loss_is_not_forced_delivered():
    sim, connection = tcp_ready(); sim.set_packet_loss("H1", "R1", 1.0); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.outstanding and sim.get_transport_statistics()["TCP"]["packets_lost"] == 1


def test_tcp_link_failure_reroutes_when_alternative_exists():
    sim, connection = tcp_ready(); original = list(connection.route); sim.fail_link("R1", "R3"); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.route != original and any(e.event_type.value == "ROUTE_RECALCULATED" for e in sim.event_logger.events)


def test_udp_route_recalculation_uses_live_router():
    sim, flow = udp_ready(); sim.fail_link("R1", "R3"); sim.send_udp_data(flow.flow_id); sim.run_until_empty(); assert flow.packets[-1].route != ["H1", "R1", "R3", "R5", "H3"]


def test_transport_uses_existing_qos_scheduler():
    sim, connection = tcp_ready(scheduler="fifo") if False else tcp_ready(); flow = sim.create_udp_flow("H1", "H3", traffic_class="VoIP"); sim.send_udp_data(flow.flow_id); sim.run_until_empty(); assert sim.scheduler.queue_statistics()["packets_served"] > 0


def test_transport_flow_uses_traffic_class_and_route():
    sim, connection = tcp_ready(traffic_class="HTTP"); sim.send_tcp_data(connection.flow_id); sim.run_until_empty(); assert connection.packets[-1].traffic_class == "HTTP" and connection.packets[-1].route


def test_transport_comparison_is_deterministic():
    first = compare_transport(NetworkSimulator(seed=9), "H1", "H3", 3, 1000, seed=9)
    second = compare_transport(NetworkSimulator(seed=9), "H1", "H3", 3, 1000, seed=9)
    assert first == second and {row["protocol"] for row in first["results"]} == {"TCP", "UDP"}
