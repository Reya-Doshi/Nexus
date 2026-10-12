"""Stage 11 - one-click demonstration scenarios.

Every demo runs on the live :class:`~lab_session.LabSession`, so it uses the
same topology, adaptive router, QoS scheduler, transport layer, services, and
security layer as the rest of NetAdapt.  There is no separate demo engine:
:func:`DemoEngine.run` loads a topology preset and then calls ordinary session
and simulator methods.  :func:`DemoEngine.reset` reloads the same preset, which
rebuilds the simulator and therefore clears packets, events, metrics, queues,
services, security state, and transport flows back to the starting point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Demo:
    demo_id: str
    title: str
    category: str
    description: str
    preset: str
    run: Callable[[Any], Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.demo_id,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "preset": self.preset,
        }


DEMOS: List[Demo] = []


def _demo(demo_id, title, category, description, preset, run) -> Demo:
    DEMOS.append(Demo(demo_id, title, category, description, preset, run))
    return demo_id


def _metrics(session: Any) -> Dict[str, Any]:
    simulator = session.simulator
    values = simulator.metrics.calculate(
        simulator.time, simulator.average_congestion(), len(simulator.active_flows)
    )
    return {
        "packets_sent": values["packets_sent"],
        "packets_delivered": values["packets_delivered"],
        "packets_dropped": values["packets_dropped"],
        "packet_delivery_ratio": values["packet_delivery_ratio"],
        "average_latency_ms": round(values["average_latency"] * 1000, 3),
        "throughput": round(values["throughput"], 2),
    }


def _events(session: Any, limit: int = 12) -> List[Dict[str, Any]]:
    return [event.to_dict() for event in session.simulator.event_logger.get_events(limit=limit)]


def _summary(steps: List[str], explanation: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"steps": steps, "explanation": explanation, "detail": extra or {}}


# --------------------------------------------------------------------------
# Demonstrations
# --------------------------------------------------------------------------


def _basic_ping(session: Any) -> Dict[str, Any]:
    result = session.simulator.diagnostic_ping("PC1", "Server1", count=4)
    session.record_diagnostic_packets(result)
    return _summary(
        [
            f"PC1 sent {result['sent']} ICMP echo request(s) to Server1",
            f"Server1 answered {result['received']} of them",
            f"Average RTT {result['rtt_ms']['average']:.2f} ms over {' → '.join(result['path'])}",
        ],
        "ICMP echo request/reply proves reachability and measures the round-trip time of the "
        "real simulated path.",
        {"ping": result},
    )


def _tcp_handshake(session: Any) -> Dict[str, Any]:
    connection = session.simulator.create_tcp_connection("PC1", "Server1", 5000, 80, traffic_class="HTTP")
    session.simulator.run_until_empty()
    states = [entry["state"] for entry in connection.to_dict()["state_history"]]
    return _summary(
        [
            "PC1 sent SYN to Server1:80",
            "Server1 replied SYN-ACK",
            "PC1 sent ACK - the connection is ESTABLISHED",
            f"State history: {' → '.join(states)}",
        ],
        "The three-way handshake completes before any data is sent; every segment is a real "
        "routed packet.",
        {"connection": connection.flow_id, "state": connection.state.value, "packets": connection.to_dict()["packets"]},
    )


def _udp_traffic(session: Any) -> Dict[str, Any]:
    flow = session.simulator.create_udp_flow("PC1", "Server1", 5001, 53, payload_size=400, traffic_class="VoIP")
    session.simulator.send_udp_data(flow.flow_id, 6, 400)
    session.simulator.run_until_empty()
    return _summary(
        [
            f"PC1 opened a connectionless UDP flow to Server1:53",
            f"{len(flow.packets)} datagrams sent, {flow.packets_delivered} delivered",
            f"{flow.bytes_delivered} bytes transferred with no handshake and no retransmission",
        ],
        "UDP sends datagrams directly: lower overhead, no reliability, which is why DNS uses it.",
        {"flow": flow.flow_id},
    )


def _tcp_loss(session: Any) -> Dict[str, Any]:
    connection = session.simulator.create_tcp_connection("PC1", "Server1", 5000, 80, timeout=0.05)
    session.simulator.run_until_empty()
    # Loss is injected after the handshake so the failure is isolated to data.
    session.configure_link("SW1", "Server1", {"packet_loss": 0.6})
    session.simulator.send_tcp_data(connection.flow_id, 3, 1000)
    session.simulator.run_until_empty()
    session.simulator.process_transport_tick(session.simulator.time + 0.2)
    session.simulator.run_until_empty()
    return _summary(
        [
            "The connection is established on a healthy link",
            "The SW1-Server1 link then drops 60% of packets",
            f"{connection.data_packets_lost} data packet(s) were lost",
            f"{connection.timeout_count} timeout(s) triggered {connection.retransmission_count} retransmission(s)",
            f"cwnd reacted to the loss and is now {connection.cwnd:.2f}",
        ],
        "TCP repairs loss with a timeout and retransmission, and the congestion window reacts "
        "to the loss instead of the data being lost silently.",
        {"connection": connection.flow_id},
    )


def _congestion(session: Any) -> Dict[str, Any]:
    session.configure_link("SW1", "Server1", {"bandwidth": 2.0, "congestion": 0.6, "latency": 30.0})
    session.simulator.create_flow("PC1", "Server1", "Video", 8, 1200, 40, 2.0)
    session.simulator.run_until_empty()
    queue = session.simulator.scheduler_statistics()
    return _summary(
        [
            "The access link bandwidth dropped to 2 Mbps with 60% congestion",
            f"The queue peaked at {queue.get('max_queue_length', 0)} packets",
            f"Average queue wait rose to {queue.get('average_waiting_time', 0) * 1000:.2f} ms",
            f"Packet delivery ratio is {_metrics(session)['packet_delivery_ratio']:.1f}%",
        ],
        "Congestion builds queues, raises latency, and increases loss - the symptoms students "
        "must learn to recognise.",
    )


def _link_failure(session: Any) -> Dict[str, Any]:
    before = session.simulator.get_current_route("PC1", "PC2")
    session.simulator.create_flow("PC1", "PC2", "HTTP", 4, 800, 10, 2.0)
    session.simulator.run_until_empty()
    session.simulator.fail_link("R1", "R2")
    session.simulator.tick(4.0)
    after = session.simulator.get_current_route("PC1", "PC2")
    session.simulator.create_flow("PC1", "PC2", "HTTP", 4, 800, 10, 2.0)
    session.simulator.run_until_empty()
    return _summary(
        [
            f"Route before failure: {' → '.join(before[0])}",
            "Link R1-R2 was failed",
            f"Route after recalculation: {' → '.join(after[0])} (cost {after[1]:.2f})",
            "Traffic continued on the redundant path",
        ],
        "This is the self-healing behaviour: the failure is detected, the route is recomputed, "
        "and traffic keeps flowing over the alternative path.",
        {"before": before[0], "after": after[0]},
    )


def _router_failure(session: Any) -> Dict[str, Any]:
    session.simulator.create_flow("PC1", "PC2", "HTTP", 2, 500, 10, 1.0)
    session.simulator.run_until_empty()
    session.simulator.fail_node("R2")
    session.simulator.tick(4.0)
    during = session.simulator.diagnostic_ping("PC1", "PC2", count=1)
    session.simulator.recover_node("R2")
    session.simulator.tick(4.0)
    after = session.simulator.diagnostic_ping("PC1", "PC2", count=1)
    return _summary(
        [
            "Router R2 was shut down",
            f"Ping during the failure: {'answered' if during['success'] else 'unreachable'}"
            f" ({' → '.join(during['path']) or 'no path'})",
            "R2 was recovered and heartbeats cleared the failure",
            f"Ping after recovery: {'answered' if after['success'] else 'still unreachable'}",
        ],
        "Node failures remove a router from every route; recovery restores it through the same "
        "heartbeat and recalculation machinery.",
    )


def _qos_congestion(session: Any) -> Dict[str, Any]:
    session.set_scheduler("priority")
    session.set_qos_config({"VoIP": 5.0, "Video": 3.0, "HTTP": 2.0, "FTP": 1.0, "Emergency": 4.0})
    session.configure_link("SW1", "Server1", {"bandwidth": 2.0, "congestion": 0.5, "latency": 25.0})
    session.simulator.create_flow("PC1", "Server1", "VoIP", 4, 256, 40, 2.0)
    session.simulator.create_flow("PC1", "Server1", "FTP", 4, 1400, 40, 2.0)
    session.simulator.run_until_empty()
    classes = session.simulator.class_metrics()
    return _summary(
        [
            "The priority scheduler now ranks VoIP above every other class",
            "A congested 2 Mbps link forces the two classes to compete",
            f"VoIP average latency {classes.get('VoIP', {}).get('average_latency', 0) * 1000:.2f} ms",
            f"FTP average latency {classes.get('FTP', {}).get('average_latency', 0) * 1000:.2f} ms",
        ],
        "Scheduling decides who waits. The same congestion hurts the prioritised class far less.",
    )


def _dhcp_demo(session: Any) -> Dict[str, Any]:
    session.install_service("DHCP", "Server1")
    result = session.simulator.dhcp_acquire("PC1", "Server1")
    interface = session.simulator.topology.get_device("PC1").interfaces[0].to_dict()
    return _summary(
        [
            "PC1 broadcast DHCP DISCOVER",
            f"Server1 offered {result.get('address')}",
            "PC1 requested that address and Server1 replied DHCP ACK",
            f"PC1 is now {interface['ip_address']}/{interface['prefix']} with gateway {session.simulator.topology.get_device('PC1').default_gateway}",
        ],
        "The lease is not cosmetic: the real NetworkInterface of PC1 is reconfigured, which "
        "changes every later routing and service decision.",
        {"result": result, "interface": interface},
    )


def _dns_demo(session: Any) -> Dict[str, Any]:
    session.install_service("DNS", "Server1")
    address = session.simulator.topology.get_device("Server1").interfaces[0].ip_address
    session.dns_add_record("Server1", "server.netadapt.local", address, ttl=120)
    first = session.simulator.dns_query("PC1", "server.netadapt.local", "Server1")
    second = session.simulator.dns_query("PC1", "server.netadapt.local", "Server1")
    return _summary(
        [
            "PC1 sent a UDP DNS query for server.netadapt.local to Server1:53",
            f"Server1 answered {first.get('address')}",
            "The answer is cached on PC1 with its TTL",
            f"The second lookup was answered from the cache ({second.get('source')})",
        ],
        "DNS separates names from addresses and the cache removes the round trip for repeats.",
        {"first": first, "second": second},
    )


def _http_demo(session: Any) -> Dict[str, Any]:
    session.install_service("HTTP", "Server1")
    ok = session.simulator.http_request("PC1", "Server1")
    missing = session.simulator.http_request("PC1", "Server1", path="/missing")
    return _summary(
        [
            f"GET / returned {ok.get('status')} in {ok.get('latency', 0) * 1000:.2f} ms",
            f"GET /missing returned {missing.get('status')} ({missing.get('reason')})",
            f"HTTP service metrics: {session.simulator.service_state()['metrics']['HTTP']}",
        ],
        "The status code is produced by the simulated server logic, and the exchange travels over "
        "the existing TCP connection.",
        {"ok": ok, "missing": missing},
    )


def _ftp_demo(session: Any) -> Dict[str, Any]:
    session.install_service("FTP", "Server1")
    listing = session.simulator.ftp_command("PC1", "Server1", "LIST")
    put = session.simulator.ftp_command("PC1", "Server1", "PUT", "lab.txt", "uploaded inside the simulator")
    get = session.simulator.ftp_command("PC1", "Server1", "GET", "lab.txt")
    return _summary(
        [
            f"FTP control connection to Server1:21 established, store lists {listing.get('listing')}",
            f"PUT stored {put.get('bytes')} bytes in the in-memory store",
            f"GET read the file back ({get.get('bytes')} bytes)",
        ],
        "FTP uses a separate bulk traffic class, and the file store is in memory: the real "
        "filesystem is never touched.",
    )


def _smtp_demo(session: Any) -> Dict[str, Any]:
    session.install_service("SMTP", "Server1")
    result = session.simulator.smtp_send(
        "PC1", "Server1", sender="student@netadapt.local",
        recipient="server@server1.netadapt.local",
    )
    return _summary(
        [
            "PC1 connected to Server1:25 and sent HELO, MAIL FROM, RCPT TO, DATA, QUIT",
            f"Message delivered: {result.get('delivered')}",
            f"Simulated mailbox now holds {len(session.simulator.services.smtp_mailboxes.get('Server1', []))} message(s)",
        ],
        "The whole conversation is simulated; no mail ever leaves the process.",
        {"transcript": result.get("transcript_text")},
    )


def _firewall_demo(session: Any) -> Dict[str, Any]:
    session.install_service("HTTP", "Server1")
    before = session.simulator.http_request("PC1", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    after = session.simulator.http_request("PC1", "Server1")
    blocked = next(
        (
            event.to_dict()
            for event in reversed(session.simulator.event_logger.events)
            if event.event_type == "PACKET_BLOCKED"
        ),
        None,
    )
    return _summary(
        [
            f"Before the rule: HTTP {before.get('status')}",
            "A DENY rule for TCP destination port 80 was added",
            f"After the rule: the request failed with {after.get('reason')}",
            f"Security metrics: {session.simulator.security_state()['metrics']}",
        ],
        "The firewall blocks the real packet inside the simulator, so the service honestly reports "
        "a failure instead of pretending to answer.",
        {"blocked_event": blocked},
    )


def _acl_demo(session: Any) -> Dict[str, Any]:
    session.install_service("HTTP", "Server1")
    session.create_acl("NO-PC1-WEB", "EXTENDED")
    session.add_acl_entry(
        "NO-PC1-WEB", action="DENY", source_ip="192.168.1.1",
        destination_ip="192.168.10.1", protocol="TCP", destination_port=80,
    )
    session.attach_acl("NO-PC1-WEB", "R1")
    denied = session.simulator.http_request("PC1", "Server1")
    other = session.simulator.http_request("PC2", "Server1")
    return _summary(
        [
            "An extended ACL denying 192.168.1.1 to Server1:80 was attached to R1",
            f"PC1 request failed with {denied.get('reason')}",
            f"PC2 request still returned HTTP {other.get('status')}",
        ],
        "An ACL is evaluated while the packet traverses the attached interface, so only the "
        "matching flow is affected.",
    )


def _arp_demo(session: Any) -> Dict[str, Any]:
    gateway = session.simulator.topology.get_device("Server1").interfaces[0].ip_address
    session.arp_spoof("PC2", "PC1", gateway, detect=True)
    state = session.simulator.services.arp_state()
    conflict = state["conflicts"][-1] if state["conflicts"] else {}
    return _summary(
        [
            f"PC2 sent a forged ARP reply claiming to be {gateway}",
            f"PC1 now trusts {conflict.get('new_mac')} for {gateway}",
            f"The detector reported a conflict: {conflict.get('known_mac')} vs {conflict.get('new_mac')}",
            f"Spoof attempts: {state['spoof_attempts']}, protection: "
            f"{'enabled' if state['protection_enabled'] else 'disabled'}",
        ],
        "This is a fully simulated attack: no real ARP packet is emitted and no real cache is "
        "touched. The detector is an educational one-IP-many-MACs check.",
        {"conflict": conflict},
    )


def _flood_demo(session: Any) -> Dict[str, Any]:
    scenario = session.simulator.start_flood(
        "PC1", "Server1", protocol="UDP", rate=400, duration=1.0, threshold=50, protect=True
    )
    return _summary(
        [
            f"PC1 generated {scenario['packets_generated']} UDP attack datagrams at {scenario['rate']:.0f} pps",
            f"Peak observed rate {scenario['peak_rate']:.0f} pps exceeded the {scenario['threshold']:.0f} pps threshold",
            f"{scenario['packets_dropped']} attack datagrams were dropped by flood protection",
            f"Queue growth {scenario['queue_growth']:.0f}, bandwidth {scenario['bandwidth']:.0f} B/s",
        ],
        "The flood stays inside the simulator. Detection compares one-second arrival windows "
        "with the configured threshold and reports TRAFFIC_SPIKE and FLOOD_DETECTED.",
        {"scenario": scenario},
    )


def _combined_demo(session: Any) -> Dict[str, Any]:
    session.install_service("HTTP", "Server1")
    baseline = session.simulator.http_request("PC1", "Server1")
    session.simulator.fail_link("R1", "R3")
    session.simulator.tick(4.0)
    rerouted = session.simulator.http_request("PC1", "Server1")
    session.add_firewall_rule(action="DENY", protocol="TCP", destination_port=80)
    blocked = session.simulator.http_request("PC1", "Server1")
    session.stop_service("HTTP", "Server1")
    stopped = session.simulator.http_request("PC1", "Server1")
    return _summary(
        [
            f"Baseline: HTTP {baseline.get('status')} on {' → '.join(baseline['packets'][0]['route'])}",
            "R1-R3 failed and the route was recalculated automatically",
            f"After rerouting: HTTP {rerouted.get('status')}",
            f"After a firewall DENY rule: {blocked.get('reason')}",
            f"After stopping the service: {stopped.get('reason')}",
        ],
        "One scenario exercises failure handling, routing, policy, and service state together - "
        "exactly the combination a real incident looks like.",
        {"metrics": _metrics(session)},
    )


# -- catalog ------------------------------------------------------------

_demo("basic_ping", "Basic Ping", "Diagnostics",
      "ICMP reachability and round-trip time between two devices.", "simple_lan", _basic_ping)
_demo("tcp_handshake", "TCP Handshake", "Transport",
      "The three-way TCP handshake between a client and port 80.", "simple_lan", _tcp_handshake)
_demo("udp_traffic", "UDP Traffic", "Transport",
      "A connectionless UDP datagram burst with no handshake.", "simple_lan", _udp_traffic)
_demo("tcp_loss", "TCP Packet Loss + Retransmission", "Transport",
      "Loss on a link causing a timeout and a retransmission.", "simple_lan", _tcp_loss)
_demo("congestion", "Network Congestion", "QoS",
      "A narrow congested link producing queue growth and loss.", "simple_lan", _congestion)
_demo("link_failure", "Link Failure + Self-Healing", "Resilience",
      "A link failure detected by heartbeats and rerouted automatically.", "self_healing", _link_failure)
_demo("router_failure", "Router Failure + Recovery", "Resilience",
      "A router failure, its effect on reachability, and recovery.", "self_healing", _router_failure)
_demo("qos_congestion", "QoS Under Congestion", "QoS",
      "Priority scheduling protecting voice traffic on a congested link.", "qos_demo", _qos_congestion)
_demo("dhcp", "DHCP Assignment", "Services",
      "A full DORA exchange that configures a real interface.", "simple_lan", _dhcp_demo)
_demo("dns", "DNS Resolution", "Services",
      "A DNS query, response, and cache hit over UDP.", "simple_lan", _dns_demo)
_demo("http", "HTTP Request", "Services",
      "An HTTP GET returning 200 and 404 over TCP.", "simple_lan", _http_demo)
_demo("ftp", "FTP Transfer", "Services",
      "FTP LIST, PUT, and GET against the in-memory file store.", "simple_lan", _ftp_demo)
_demo("smtp", "SMTP Message", "Services",
      "A complete simulated SMTP conversation and delivery.", "simple_lan", _smtp_demo)
_demo("firewall", "Firewall Blocking", "Security",
      "A firewall rule that blocks a real HTTP request.", "simple_lan", _firewall_demo)
_demo("acl", "ACL Blocking", "Security",
      "An extended ACL on a router interface blocking one client.", "self_healing", _acl_demo)
_demo("arp_spoof", "ARP Spoofing Detection", "Security",
      "A simulated ARP poisoning attack and its detection.", "self_healing", _arp_demo)
_demo("flood", "Flood Detection", "Security",
      "A controlled UDP flood with rate detection and protection.", "simple_lan", _flood_demo)
_demo("combined", "Combined Network Failure", "Resilience",
      "Failure, rerouting, policy, and service state in one scenario.", "self_healing", _combined_demo)


class DemoEngine:
    """Runs and resets the demonstration scenarios on a live lab session."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.active: Optional[str] = None

    @staticmethod
    def catalog() -> List[Dict[str, Any]]:
        return [demo.to_dict() for demo in DEMOS]

    @staticmethod
    def require(demo_id: str) -> Demo:
        for demo in DEMOS:
            if demo.demo_id == demo_id:
                return demo
        raise ValueError(f"Unknown demonstration: {demo_id}")

    def run(self, demo_id: str) -> Dict[str, Any]:
        demo = self.require(demo_id)
        # Loading the preset rebuilds the simulator: no state leaks between demos.
        self.session.load_preset(demo.preset, record=False)
        self.active = demo_id
        summary = demo.run(self.session) or {}
        self.session.running = True
        result = {
            "id": demo.demo_id,
            "title": demo.title,
            "category": demo.category,
            "description": demo.description,
            "preset": demo.preset,
            "steps": summary.get("steps", []),
            "explanation": summary.get("explanation", ""),
            "detail": summary.get("detail", {}),
            "metrics": _metrics(self.session),
            "events": _events(self.session),
            "service_metrics": self.session.simulator.service_state()["metrics"],
            "security_metrics": self.session.simulator.security_state()["metrics"],
        }
        self.session.demo_result = result
        self.session._update_document()
        return result

    def reset(self, demo_id: Optional[str] = None) -> Dict[str, Any]:
        target = demo_id or self.active
        if target is None:
            raise ValueError("No demonstration is active")
        demo = self.require(target)
        self.session.load_preset(demo.preset, record=False)
        self.session.running = False
        self.session.demo_result = None
        self.active = None
        return {
            "reset": True,
            "id": demo.demo_id,
            "title": demo.title,
            "preset": demo.preset,
            "state": {
                "time": self.session.simulator.time,
                "packets": len(self.session.simulator.packets),
                "events": len(self.session.simulator.event_logger.events),
                "services": len(self.session.simulator.services.registry.list()),
                "firewall_rules": len(self.session.simulator.services.firewall.rules),
                "acls": len(self.session.simulator.services.acls.acls),
            },
        }
