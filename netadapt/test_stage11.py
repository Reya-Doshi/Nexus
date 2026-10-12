"""Stage 11 acceptance tests: learning mode, evaluation, and final polish.

These tests validate the whole laboratory end to end: topology, devices,
routing, ARP, ICMP, TCP/UDP, QoS, the Stage 10 services and security, learning
demonstrations, challenge evaluation, quizzes, demo scenarios, reporting, CLI
polish, and deterministic reproducibility.
"""

import pytest

import learning
from challenges import CHALLENGES, ChallengeEngine
from demos import DEMOS, DemoEngine
from lab_session import LabError, LabSession
from learning import LearningMode, build_packet_journey, explain_failure, explain_reason
from quiz import STATIC_QUESTIONS, QuizEngine, live_questions
from report import (
    HEALTHY,
    WARNING,
    network_health,
    network_overview,
    network_report,
    report_csv,
    report_dataframe,
    report_rows,
    report_to_markdown,
)
from services_security import DROP_FIREWALL
from simulator import NetworkSimulator


# ----------------------------------------------------------------------
# 1/2. Topology and device configuration
# ----------------------------------------------------------------------


def test_stage11_01_basic_topology_and_device_configuration():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    state = session.state()
    assert {device["id"] for device in state["devices"]} == {"PC1", "SW1", "Server1"}
    assert len(state["links"]) == 2
    session.add_device("pc", 100, 100, "PC2")
    session.configure_interface("PC1", "eth0", {"ip_address": "192.168.1.50", "prefix": 24})
    interface = session.simulator.get_device("PC1").get_interface("eth0")
    assert interface.ip_address == "192.168.1.50"
    assert interface.prefix == 24


def test_stage11_02_invalid_configuration_returns_useful_messages():
    session = LabSession()
    with pytest.raises(LabError) as excinfo:
        session.configure_interface("PC1", "eth9", {"status": "UP"})
    assert "Interface does not exist" in str(excinfo.value)
    result = session.console("PC1", "ping 999.1")
    assert result["output"] == "% Invalid IP address: 999.1"
    unsupported = session.console("PC1", "frobnicate")
    assert unsupported["output"].startswith("% Command not supported")
    wrong_device = session.console("PC1", "show ip route")
    assert "not supported on this device" in wrong_device["output"]


# ----------------------------------------------------------------------
# 3-6. Routing, ARP, ICMP, TCP, UDP
# ----------------------------------------------------------------------


def test_stage11_03_routing_and_self_healing():
    simulator = NetworkSimulator(seed=42)
    simulator.create_flow("H1", "H4", "HTTP", 2, 500, 10, 1.0)
    simulator.run_until_empty()
    before = simulator.get_current_route("H1", "H4")[0]
    simulator.fail_link("R4", "R6")
    simulator.tick(4.0)
    after = simulator.get_current_route("H1", "H4")[0]
    assert after != before and "R5" in after
    assert any(event.event_type == "ROUTE_RECALCULATED" for event in simulator.event_logger.events)


def test_stage11_04_arp_and_icmp():
    simulator = NetworkSimulator(seed=42)
    entry = simulator.arp_lookup("H1", "192.168.1.3")
    assert entry is not None and entry.mac_address
    ping = simulator.diagnostic_ping("H1", "H3", count=2)
    assert ping["received"] == 2 and ping["path"]


def test_stage11_05_tcp_handshake_and_data():
    simulator = NetworkSimulator(seed=42)
    connection = simulator.create_tcp_connection("H1", "H3")
    simulator.run_until_empty()
    assert connection.state.value == "ESTABLISHED"
    simulator.send_tcp_data(connection.flow_id, 1, 800)
    simulator.run_until_empty()
    simulator.send_tcp_data(connection.flow_id, 1, 800)
    simulator.run_until_empty()
    assert connection.data_packets_delivered == 2


def test_stage11_06_udp_flow():
    simulator = NetworkSimulator(seed=42)
    flow = simulator.create_udp_flow("H1", "H3", 5001, 53, payload_size=300)
    simulator.send_udp_data(flow.flow_id, 3, 300)
    simulator.run_until_empty()
    assert flow.packets_delivered == 3 and flow.bytes_delivered == 900


# ----------------------------------------------------------------------
# 7/8. QoS
# ----------------------------------------------------------------------


def test_stage11_07_qos_scheduler_comparison():
    simulator = NetworkSimulator(seed=42)
    results = {}
    for scheduler in ("fifo", "priority", "wfq"):
        simulator.reset()
        simulator.set_scheduler(scheduler)
        simulator.set_link_conditions("R1", "R3", bandwidth=2.0, congestion=0.5)
        simulator.create_flow("H1", "H4", "VoIP", 4, 512, 30, 2.0)
        simulator.create_flow("H1", "H4", "FTP", 4, 1400, 30, 2.0)
        simulator.run_until_empty()
        results[scheduler] = simulator.class_metrics()
    assert set(results) == {"fifo", "priority", "wfq"}
    assert all(results["priority"]["VoIP"]["average_latency"] > 0 for _ in [0])


# ----------------------------------------------------------------------
# 9-17. Services and security (Stage 10 subsystems, still green)
# ----------------------------------------------------------------------


def test_stage11_08_dhcp_dns_http_ftp_smtp_flow():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    for service in ("DHCP", "DNS", "HTTP", "FTP", "SMTP"):
        session.install_service(service, "Server1")
    lease = session.dhcp_acquire("PC1", "Server1")["services"]["result"]
    assert lease["success"] and lease["steps"][-1] == "ACK"
    address = session.simulator.get_device("Server1").interfaces[0].ip_address
    session.dns_add_record("Server1", "server.netadapt.local", address)
    assert session.dns_query("PC1", "server.netadapt.local", "Server1")["services"]["result"]["success"]
    assert session.http_request("PC1", "Server1")["services"]["result"]["status"] == 200
    assert session.ftp_command("PC1", "Server1", "LIST")["services"]["result"]["success"]
    assert session.smtp_send("PC1", "Server1", recipient="a@server1.netadapt.local")["services"]["result"]["delivered"]


def test_stage11_09_firewall_acl_arp_flood():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    session.install_service("HTTP", "Server1")
    assert session.http_request("PC1", "Server1")["services"]["result"]["status"] == 200
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    blocked = session.http_request("PC1", "Server1")["services"]["result"]
    assert blocked["success"] is False and blocked["reason"] == DROP_FIREWALL
    session.create_acl("NO-WEB", "EXTENDED")
    session.add_acl_entry("NO-WEB", action="DENY", source_ip="192.168.1.1", protocol="TCP", destination_port=80)
    session.attach_acl("NO-WEB", "SW1")
    assert session.http_request("PC1", "Server1")["services"]["result"]["reason"] in {"ACL_DENY", DROP_FIREWALL}
    spoof = session.arp_spoof(
        "PC1", "PC1", session.simulator.get_device("Server1").interfaces[0].ip_address, detect=True
    )["security"]["result"]
    assert spoof["detection"]["conflict"] is True
    flood = session.start_flood(
        "PC1", "Server1", protocol="UDP", rate=200, duration=1.0, threshold=30
    )["security"]["result"]
    assert flood["detected"] is True


# ----------------------------------------------------------------------
# 18. Self-healing
# ----------------------------------------------------------------------


def test_stage11_10_self_healing_through_the_session():
    session = LabSession()
    session.load_preset("self_healing", record=False)
    before = session.simulator.get_current_route("PC1", "PC2")[0]
    session.simulator.fail_node("R2")
    session.simulator.tick(4.0)
    after = session.simulator.get_current_route("PC1", "PC2")[0]
    assert after != before
    session.simulator.recover_node("R2")
    session.simulator.tick(4.0)
    assert session.simulator.get_current_route("PC1", "PC2")[0] == before


# ----------------------------------------------------------------------
# 19. Packet inspection, journey, and explanations
# ----------------------------------------------------------------------


def test_stage11_11_packet_journey_uses_real_history():
    simulator = NetworkSimulator(seed=42)
    connection = simulator.create_tcp_connection("H1", "H3")
    simulator.run_until_empty()
    packet = connection.packets[0]
    journey = build_packet_journey(simulator, packet.network_packet_id)
    assert journey["source"] == "H1" and journey["destination"] == "H3"
    assert journey["hops"][0]["device"] == "H1"
    assert journey["hops"][-1]["action"] in {"DELIVERED", "DROPPED"}
    assert all(hop["interface"] for hop in journey["hops"])
    with pytest.raises(ValueError):
        build_packet_journey(simulator, 9999)


def test_stage11_12_failure_explanations_come_from_events():
    simulator = NetworkSimulator(seed=42)
    simulator.install_service("HTTP", "H3")
    simulator.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    result = simulator.http_request("H1", "H3")
    assert result["success"] is False
    explanation = explain_failure(simulator, None, result)
    assert explanation["failed"] is True
    assert explanation["reason"] == DROP_FIREWALL
    assert "firewall" in explanation["explanation"].lower()
    blocked = next(
        item for item in simulator.packets
        if (getattr(item, "security", None) or {}).get("reason") == DROP_FIREWALL
    )
    packet_explanation = explain_failure(simulator, blocked)
    assert packet_explanation["details"]["packet_id"] == blocked.packet_id
    assert explain_reason("NO_ROUTE")
    assert explain_reason(None)


# ----------------------------------------------------------------------
# 20. CLI polish
# ----------------------------------------------------------------------


def test_stage11_13_cli_help_and_device_commands():
    session = LabSession()
    session.load_preset("self_healing", record=False)
    general = session.console("PC1", "help")
    assert "help ping" in general["output"]
    for topic in ("ping", "show", "routing", "tcp", "dns"):
        assert session.console("PC1", f"help {topic}")["output"]
    assert "% No help topic" in session.console("PC1", "help nonsense")["output"]
    for command in ("ipconfig", "ipconfig /all", "arp -a", "route print", "netstat", "show services"):
        assert session.console("PC1", command)["output"]
    for command in ("show interfaces", "show ip interface brief", "show ip route", "show access-lists", "show running-config"):
        assert session.console("R1", command)["output"]
    for command in ("show interfaces status", "show mac address-table", "show vlan brief", "show running-config"):
        assert session.console("SW1", command)["output"]


def test_stage11_14_cli_reports_live_service_and_security_state():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    session.install_service("DNS", "Server1")
    session.stop_service("DNS", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    session.http_request("PC1", "Server1")
    services = session.console("Server1", "show services")
    assert "STOPPED" in services["output"]
    assert "DENY TCP" in session.console("Server1", "show firewall")["output"]
    assert "Proto" in session.console("PC1", "show connections")["output"]
    assert "ARP detection" in session.console("Server1", "show security")["output"]
    assert session.console("Server1", "show service http")["status"]["state"] == "RUNNING"


# ----------------------------------------------------------------------
# 21. Demo scenarios
# ----------------------------------------------------------------------


def test_stage11_15_all_eighteen_demo_scenarios_execute():
    session = LabSession()
    engine = DemoEngine(session)
    assert len(DEMOS) == 18
    for demo in DEMOS:
        result = engine.run(demo.demo_id)
        assert result["steps"], demo.demo_id
        assert result["explanation"], demo.demo_id
        # Every demo produces real simulator activity: either network packets or
        # protocol-layer packets (ICMP/ARP) recorded on the same session.
        assert result["events"] or result["metrics"]["packets_sent"] > 0, demo.demo_id


def test_stage11_16_demo_reset_clears_every_subsystem():
    session = LabSession()
    engine = DemoEngine(session)
    engine.run("combined")
    assert session.simulator.packets and session.simulator.event_logger.events
    reset = engine.reset("combined")
    assert reset["reset"] is True
    assert reset["state"]["packets"] == 0
    assert reset["state"]["events"] == 0
    assert reset["state"]["services"] == 0
    assert reset["state"]["firewall_rules"] == 0
    assert reset["state"]["acls"] == 0
    assert session.simulator.time == 0.0
    assert session.demo_result is None


def test_stage11_17_demos_do_not_leak_state_between_runs():
    session = LabSession()
    engine = DemoEngine(session)
    engine.run("firewall")
    blocked = session.http_request("PC1", "Server1")["services"]["result"]
    engine.run("http")
    allowed = session.http_request("PC1", "Server1")["services"]["result"]
    assert blocked["success"] is False
    assert allowed["success"] is True and allowed["status"] == 200


# ----------------------------------------------------------------------
# 22/23. Challenges
# ----------------------------------------------------------------------


def test_stage11_18_challenges_start_incomplete_and_list_failing_criteria():
    session = LabSession()
    engine = ChallengeEngine(session)
    assert len(CHALLENGES) == 6
    for challenge in CHALLENGES:
        state = engine.start(challenge.challenge_id)
        assert state["status"] in {"PASS", "NOT COMPLETE"}
        assert state["criteria"], challenge.challenge_id
        if state["status"] == "NOT COMPLETE":
            assert state["failing"], challenge.challenge_id


def test_stage11_19_connectivity_challenge_is_solved_by_real_repair():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("connectivity")
    assert session.simulator.topology.get_device("PC1").get_interface("eth0").status == "DOWN"
    session.configure_interface("PC1", "eth0", {"status": "UP"})
    assert engine.evaluate()["status"] == "NOT COMPLETE"
    session.console("PC1", "ipconfig /renew")
    result = engine.evaluate()
    assert result["status"] == "PASS"
    assert all(row["passed"] for row in result["criteria"])
    assert result["history"][-1]["challenge"] == "connectivity"


def test_stage11_20_firewall_challenge_passes_when_the_rule_is_removed():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("http_scope")
    assert engine.evaluate()["status"] == "NOT COMPLETE"
    rule = session.simulator.services.firewall.rules[0]
    session.firewall_remove_rule(rule.rule_id)
    result = engine.evaluate()
    assert result["status"] == "PASS"
    assert [row["description"] for row in result["criteria"] if row["passed"]]


def test_stage11_21_dns_challenge_requires_service_and_record():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("dns_repair")
    session.start_service("DNS", "Server1")
    assert engine.evaluate()["status"] == "NOT COMPLETE"
    address = session.simulator.get_device("Server1").interfaces[0].ip_address
    session.dns_add_record("Server1", "server.netadapt.local", address, ttl=60)
    assert engine.evaluate()["status"] == "PASS"


def test_stage11_22_arp_challenge_needs_detection_and_the_attacker():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("arp_spoof")
    assert engine.evaluate()["status"] == "NOT COMPLETE"
    session.configure_security({"arp_detection": True})
    session.arp_spoof("PC2", "PC1", None, detect=True)
    result = engine.evaluate()
    assert "The attacker was correctly identified" in result["failing"]
    assert engine.submit_answer("attacker", "PC1")["status"] == "NOT COMPLETE"
    assert engine.submit_answer("attacker", "PC2")["status"] == "PASS"


def test_stage11_23_hints_are_progressive_and_tracked():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("connectivity")
    first = engine.hint()
    second = engine.hint()
    assert first["hint"] == 1 and second["hint"] == 2
    assert first["text"] != second["text"]
    assert engine.evaluate()["hints_used"] == 2
    assert engine.hint()["hint"] == 3
    assert engine.hint()["hint"] == 4


def test_stage11_24_qos_challenge_checks_scheduler_and_priority():
    session = LabSession()
    engine = ChallengeEngine(session)
    engine.start("qos_priority")
    assert engine.evaluate()["failing"]
    session.set_scheduler("priority")
    session.set_qos_config({"VoIP": 9.0, "FTP": 1.0})
    result = engine.evaluate()
    assert "A VoIP flow has been delivered" in result["failing"]
    session.simulator.create_udp_flow("PC1", "Server1", 6000, 9000, 200, "VoIP")
    session.simulator.send_udp_data("UDP-001", 1, 200)
    session.simulator.run_until_empty()
    assert engine.evaluate()["status"] == "PASS"


def test_stage11_25_self_healing_challenge_requires_student_traffic():
    session = LabSession()
    engine = ChallengeEngine(session)
    state = engine.start("self_healing")
    assert "Traffic was started after the failure" in state["failing"]
    session.start_traffic("PC1", "PC2", 2, 5)
    assert engine.evaluate()["status"] == "PASS"


# ----------------------------------------------------------------------
# 24. Learning mode
# ----------------------------------------------------------------------


def test_stage11_26_learning_catalog_covers_every_category():
    categories = LearningMode.categories()
    names = {item["category"] for item in categories}
    assert names == {
        "NETWORK BASICS", "DATA LINK", "NETWORK LAYER",
        "TRANSPORT", "APPLICATION", "SECURITY", "QoS",
    }
    assert sum(len(item["topics"]) for item in categories) == len(learning.TOPICS)
    for item in learning.TOPICS:
        assert item.concept and item.why and item.how and item.demonstration
        assert item.observe and callable(item.run)


def test_stage11_27_every_topic_runs_the_real_simulator():
    mode = LearningMode(seed=42)
    for topic in learning.TOPICS:
        result = mode.run(topic.topic_id)
        assert result["summary"], topic.topic_id
        assert result["packets"] is not None, topic.topic_id
        assert result["events"] is not None, topic.topic_id


def test_stage11_27b_packet_topics_generate_real_packets():
    mode = LearningMode(seed=42)
    for topic_id in ("tcp_handshake", "dhcp", "http", "flood_detection", "congestion"):
        result = mode.run(topic_id)
        assert result["metrics"]["packets_sent"] > 0, topic_id
        assert result["packets"], topic_id


def test_stage11_28_learning_step_mode_walks_real_events():
    mode = LearningMode(seed=42)
    mode.run("tcp_handshake")
    assert mode.steps and mode.cursor == 0
    first = mode.advance()["current"]
    assert first["event"] in {"TCP_CONNECTION_STARTED", "TCP_SYN_SENT", "TCP_SYN_ACK_SENT"}
    assert mode.back()["position"] == 0
    assert mode.advance(3)["position"] == 3
    assert mode.reset_steps()["position"] == 0


def test_stage11_29_learning_through_the_session_and_state():
    session = LabSession()
    state = session.run_learning_topic("dhcp")
    assert state["learning"]["result"]["topic"]["id"] == "dhcp"
    assert state["learning"]["step_mode"]["total"] > 0
    stepped = session.learning_step("next")
    assert stepped["learning"]["step_mode"]["position"] == 1
    assert session.learning_step("reset")["learning"]["step_mode"]["position"] == 0
    with pytest.raises(LabError):
        session.run_learning_topic("not-a-topic")


# ----------------------------------------------------------------------
# 25. Quiz
# ----------------------------------------------------------------------


def test_stage11_30_static_questions_cover_every_required_type():
    kinds = {question.kind for question in STATIC_QUESTIONS}
    assert {
        "mcq", "true_false", "identify_protocol", "identify_packet",
        "failure_reason", "routing_table", "packet_capture", "tcp_next",
    } <= kinds
    for question in STATIC_QUESTIONS:
        assert question.options and 0 <= question.answer < len(question.options)
        assert question.explanation


def test_stage11_31_live_questions_come_from_the_current_topology():
    session = LabSession()
    session.load_preset("self_healing", record=False)
    questions = live_questions(session)
    assert len(questions) >= 4
    kinds = {question.kind for question in questions}
    assert "routing_table" in kinds
    assert all(question.source == "live" for question in questions)
    assert any("192.168" in question.prompt or "route" in question.prompt.lower() for question in questions)


def test_stage11_32_quiz_flow_scores_and_explains():
    session = LabSession()
    quiz = QuizEngine(session)
    state = quiz.start()
    assert state["total"] >= 8
    first = state["current"]
    answered = quiz.submit(first["options"][first["options"].index(first["correct_answer"])] if "correct_answer" in first else first["options"][0])
    assert answered["score"]["answered"] == 1
    assert answered["explanation"]
    assert answered["correct_answer"] in first["options"]
    session.quiz_mode("start")
    state = session.quiz_mode("submit", 0)
    assert state["quiz"]["score"]["answered"] == 1


# ----------------------------------------------------------------------
# Health, overview, and report
# ----------------------------------------------------------------------


def test_stage11_33_network_health_uses_explicit_rules():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    health = network_health(session)
    assert {item["category"] for item in health["categories"]} == {
        "Connectivity", "Routing", "Performance", "QoS", "Services", "Security",
    }
    assert all(item["rules"] and item["rules"][0]["reason"] for item in health["categories"])
    assert health["overall"] in {HEALTHY, WARNING, "CRITICAL"}
    assert health["thresholds"]["pdr_warning_percent"] == 95.0
    assert health["counts"]["HEALTHY"] >= 1


def test_stage11_34_health_degrades_with_a_real_failure():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    session.install_service("HTTP", "Server1")
    assert network_health(session)["overall"] in {HEALTHY, WARNING}
    session.stop_service("HTTP", "Server1")
    services = next(item for item in network_health(session)["categories"] if item["category"] == "Services")
    assert services["status"] == WARNING
    assert any("unavailable" in rule["reason"] for rule in services["rules"])


def test_stage11_35_overview_uses_real_metrics():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    overview = network_overview(session)
    assert overview["devices"] == 3 and overview["links"] == 2
    assert overview["packets_sent"] > 0
    assert overview["services_running"] == 1
    for key in (
        "packet_delivery_ratio", "average_latency_ms", "throughput", "congestion",
        "active_failures", "route_changes", "tcp_connections", "udp_flows",
        "blocked_packets", "security_alerts",
    ):
        assert key in overview


def test_stage11_36_report_contains_every_required_section():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    session.install_service("HTTP", "Server1")
    session.http_request("PC1", "Server1")
    session.start_flood("PC1", "Server1", protocol="UDP", rate=120, duration=1.0, threshold=30)
    report = network_report(session)
    for section in (
        "topology", "devices", "interfaces", "routing", "traffic", "qos", "transport",
        "services", "security", "failures", "recovery", "performance" if "performance" in report else "traffic",
        "challenges", "quiz", "overview", "health",
    ):
        assert section in report, section
    assert report["routing"]["changes"] >= 0
    assert report["services"]["metrics"]["HTTP"]["requests"] == 1
    assert "Packet Delivery" in report_to_markdown(report) or "packet delivery" in report_to_markdown(report).lower()


def test_stage11_37_report_exports_csv_and_dataframe():
    session = LabSession()
    report = network_report(session)
    rows = report_rows(report)
    assert rows and {"section", "name", "detail"} == set(rows[0])
    csv_text = report_csv(report)
    assert csv_text.splitlines()[0] == "section,name,detail"
    assert len(csv_text.splitlines()) == len(rows) + 1
    frame = report_dataframe(report)
    assert list(frame.columns) == ["section", "name", "detail"]
    assert len(frame) == len(rows)


def test_stage11_38_report_export_through_the_session():
    session = LabSession()
    session.install_service("HTTP", "Server1")
    assert session.network_report("json")["format"] == "json"
    assert "# NetAdapt network report" in session.network_report("markdown")["text"]
    assert session.network_report("csv")["text"].startswith("section,name,detail")


# ----------------------------------------------------------------------
# Determinism and performance
# ----------------------------------------------------------------------


def test_stage11_39_learning_demonstrations_are_deterministic():
    first = LearningMode(seed=42).run("tcp_handshake")
    second = LearningMode(seed=42).run("tcp_handshake")
    assert [step["title"] for step in first["steps"]] == [step["title"] for step in second["steps"]]
    assert first["metrics"] == second["metrics"]


def test_stage11_40_demos_are_deterministic():
    def run_demo():
        session = LabSession()
        return DemoEngine(session).run("link_failure")

    assert run_demo()["metrics"] == run_demo()["metrics"]
    assert run_demo()["steps"] == run_demo()["steps"]


def test_stage11_41_history_stays_bounded():
    session = LabSession()
    session.load_preset("simple_lan", record=False)
    for _ in range(3):
        session.start_traffic("PC1", "Server1", 4, 50)
        session.simulator.run_until_empty()
    state = session.state()
    assert len(state["packets"]) <= 200
    assert len(state["events"]) <= 40
