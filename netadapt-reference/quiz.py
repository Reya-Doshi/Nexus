"""Stage 11 - Quiz Mode.

Two question sources:

* :data:`STATIC_QUESTIONS` - concept questions about NetAdapt and networking,
  each with an explanation that is shown after submission.
* :func:`live_questions` - questions generated from the *current* simulated
  topology.  The correct answer is re-derived from live simulator state at
  grading time, so a live question can never drift out of date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Question:
    question_id: str
    kind: str
    prompt: str
    options: List[str]
    answer: int
    explanation: str
    source: str = "concept"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.question_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": list(self.options),
            "source": self.source,
        }


def _q(question_id, kind, prompt, options, answer, explanation, source: str = "concept") -> Question:
    return Question(question_id, kind, prompt, options, answer, explanation, source)


STATIC_QUESTIONS: List[Question] = [
    _q("q_osi_layer", "mcq",
       "Which OSI layer does a router operate on?",
       ["Data link", "Network", "Transport", "Session"], 1,
       "A router forwards IPv4 packets, which is the network layer (layer 3)."),
    _q("q_tcp_next", "tcp_next",
       "A TCP connection is in SYN_SENT. What does the client receive next?",
       ["FIN", "SYN-ACK", "RST", "DATA"], 1,
       "The server answers the SYN with SYN-ACK; the client then replies with ACK and the "
       "connection becomes ESTABLISHED."),
    _q("q_tcp_retransmit", "failure_reason",
       "Why did NetAdapt retransmit a TCP segment?",
       ["The server was stopped",
        "The original segment was lost on its route",
        "The port was filtered",
        "The subnet was full"], 1,
       "A retransmission is only produced when an expected segment never arrived, which in "
       "NetAdapt means the packet was dropped by the simulated loss/congestion model."),
    _q("q_arp", "identify_protocol",
       "Which protocol resolves an IPv4 address to a MAC address?",
       ["ICMP", "ARP", "DHCP", "DNS"], 1,
       "ARP maps IPv4 to MAC on the local link; ICMP reports errors, DHCP assigns addresses, "
       "DNS resolves names."),
    _q("q_dns_port", "mcq",
       "Which transport protocol and port does DNS use in NetAdapt?",
       ["TCP/53", "UDP/53", "UDP/80", "TCP/25"], 1,
       "DNS queries and responses are sent over UDP port 53 in the simulation."),
    _q("q_dhcp_dora", "identify_packet",
       "Which DHCP message assigns the address to the client?",
       ["DISCOVER", "OFFER", "REQUEST", "ACK"], 3,
       "The ACK carries the final lease (address, mask, gateway, DNS) and configures the client interface."),
    _q("q_http_404", "mcq",
       "An HTTP GET for a path that is not in the simulated resource table returns:",
       ["200 OK", "301 Moved", "404 Not Found", "500 Server Error"], 2,
       "A known server with an unknown path answers 404; 500 is only produced by an injected fault."),
    _q("q_acl_standard", "mcq",
       "What does a standard ACL match on?",
       ["Source IP only", "Destination port", "Protocol and port", "MAC address"], 0,
       "A standard ACL filters by source address; destination, protocol, and ports need an extended ACL."),
    _q("q_ttl", "mcq",
       "What happens when a packet's TTL reaches zero?",
       ["It is delivered",
        "The router drops it and returns ICMP TTL exceeded",
        "It is retransmitted",
        "The firewall blocks it"], 1,
       "TTL bounds the number of hops; the returning router sends TTL exceeded, which is what "
       "traceroute observes."),
    _q("q_pdr", "true_false",
       "A packet delivery ratio of 100% means every generated packet was delivered.",
       ["True", "False"], 0,
       "PDR is delivered/sent, so 100% means no packet was lost or dropped."),
    _q("q_wfq", "mcq",
       "What does WFQ use to share bandwidth between traffic classes?",
       ["A per-class weight", "A single global FIFO", "Random selection", "Priority bits"], 0,
       "Weighted fair queueing gives each class service credits proportional to its weight."),
    _q("q_priority_queue", "mcq",
       "Under congestion, which class does the Priority Queue serve first?",
       ["The lowest priority class", "The highest priority class", "Whichever arrived first", "Randomly"], 1,
       "The priority scheduler orders classes by the configured priority, highest first."),
    _q("q_firewall_stateful", "mcq",
       "What makes the simulated firewall stateful?",
       ["It remembers the decision for a 5-tuple and reuses it",
        "It stores packets on disk",
        "It only allows ARP",
        "It tracks device boot order"], 0,
       "The first packet of a flow decides, and later packets of the same 5-tuple reuse that decision."),
    _q("q_drop_reason", "failure_reason",
       "An ACL attached to a router denies a packet. Which drop reason does NetAdapt report?",
       ["PORT_BLOCKED", "ACL_DENY", "ARP_CONFLICT", "NO_ROUTE"], 1,
       "Interface ACLs report ACL_DENY; port filters report PORT_BLOCKED and the firewall reports FIREWALL_BLOCK."),
    _q("q_bellman_ford", "mcq",
       "Why choose Bellman-Ford over Dijkstra?",
       ["It is always faster",
        "It tolerates negative weights",
        "It needs no graph",
        "It works without a topology"], 1,
       "Dijkstra requires non-negative weights; Bellman-Ford handles negative edges."),
    _q("q_smtp", "mcq",
       "Which SMTP command introduces the message body?",
       ["HELO", "MAIL FROM", "RCPT TO", "DATA"], 3,
       "DATA starts the body; MAIL FROM and RCPT TO declare the envelope."),
    _q("q_ftp_store", "mcq",
       "Where does the simulated FTP service store files?",
       ["In the real filesystem", "In an in-memory file store", "In the ARP cache", "In a routing table"], 1,
       "FTP uses an in-memory store so the simulation never touches the real disk."),
    _q("q_flood_threshold", "mcq",
       "When does NetAdapt report a traffic spike?",
       ["When the packet queue is empty",
        "When the arrival rate exceeds the configured threshold",
        "When a link is congested",
        "When a route changes"], 1,
       "Arrival rates are counted in one-second windows and compared with the flood threshold."),
    _q("q_route_table", "routing_table",
       "In a routing table, what does the 'next hop' column hold?",
       ["The destination host name", "The router or address traffic is forwarded to",
        "The queue length", "The MAC address of the sender"], 1,
       "A routing entry maps a destination network to the next hop and outgoing interface."),
    _q("q_capture_http", "packet_capture",
       "A capture shows TCP 49152 -> 80 with payload 'GET /'. Which service is this?",
       ["FTP", "SMTP", "HTTP", "DNS"], 2,
       "Destination port 80 identifies HTTP; the payload confirms a GET request."),
    _q("q_capture_drop", "packet_capture",
       "A capture shows a UDP packet to port 67 with no response and a drop reason PORT_BLOCKED. "
       "What happened?",
       ["The DHCP pool was exhausted",
        "A port filter denied UDP/67",
        "The client had no address",
        "The server was down"], 1,
       "PORT_BLOCKED is produced by a port filter, not by the DHCP server logic."),
    _q("q_tcp_window", "mcq",
       "What limits how many unacknowledged segments TCP may have in flight?",
       ["The congestion window and the receiver window", "The TTL", "The ARP cache", "The DNS TTL"], 0,
       "The effective window is the smaller of cwnd and the receiver window."),
    _q("q_self_heal", "mcq",
       "What makes NetAdapt traffic self-healing?",
       ["Traffic is duplicated on every link",
        "The router recalculates routes when a component fails",
        "Packets ignore failed links",
        "The firewall reroutes traffic"], 1,
       "Heartbeat detection marks a component failed and the adaptive router picks a new path."),
]


# --------------------------------------------------------------------------
# Live questions derived from the current simulation
# --------------------------------------------------------------------------


def _options_with_answer(
    values: List[str], correct: str, rng_state: int = 0
) -> tuple[List[str], int]:
    """Place ``correct`` among distractors deterministically."""

    options = [correct]
    for value in values:
        if len(options) >= 4:
            break
        if value != correct and value not in options:
            options.append(value)
    while len(options) < 4:
        options.append(f"none of these ({len(options)})")
    # Deterministic rotation so option order is stable for a given question.
    shift = len(correct) % len(options)
    options = options[shift:] + options[:shift]
    return options, options.index(correct)


def live_questions(session: Any, limit: int = 6) -> List[Question]:
    """Build questions from the current topology, devices, and traffic."""

    simulator = session.simulator
    topology = simulator.topology
    hosts = [name for name, device in topology.devices.items() if not device.is_router and not device.is_switch]
    routers = [name for name, device in topology.devices.items() if device.is_router]
    if not hosts or len(hosts) < 2:
        return []

    source, destination = hosts[0], hosts[1]
    questions: List[Question] = []

    path, cost, status = simulator.get_current_route(source, destination)
    if path:
        options, answer = _options_with_answer(
            [f"{' → '.join(path)} (cost {cost:.0f})", "no route", "direct link only", "via a switch only"],
            f"{' → '.join(path)} (cost {cost:.0f})",
        )
        questions.append(
            _q("live_route", "routing_table",
               f"What is the current route from {source} to {destination}?",
               options, answer,
               f"The adaptive router ({simulator.get_router_algorithm()}) selected "
               f"{' → '.join(path)} with cost {cost:.2f} and status {status}.",
               source="live")
        )

    links = list(topology.graph.edges(data=True))
    if links:
        bandwidths = sorted({float(edge.get("bandwidth", 100.0)) for _, _, edge in links})
        lowest = bandwidths[0]
        slowest = [f"{u}-{v}" for u, v, edge in links if float(edge.get("bandwidth", 100.0)) == lowest]
        options, answer = _options_with_answer(
            [f"{', '.join(slowest)} ({lowest:g} Mbps)", "none", "all links", "only router links"],
            f"{', '.join(slowest)} ({lowest:g} Mbps)",
        )
        questions.append(
            _q("live_bottleneck", "mcq",
               "Which link is the bandwidth bottleneck right now?",
               options, answer,
               f"The lowest configured link bandwidth is {lowest:g} Mbps on {', '.join(slowest)}.",
               source="live")
        )

    down = [
        f"{name}.{interface.interface_id}"
        for name, device in topology.devices.items()
        for interface in device.interfaces
        if interface.status == "DOWN"
    ]
    options, answer = _options_with_answer(
        [", ".join(down) if down else "no interface is down", "all interfaces", "eth0 everywhere", "loopback"],
        ", ".join(down) if down else "no interface is down",
    )
    questions.append(
        _q("live_down", "failure_reason",
           "Which interface is down right now?",
           options, answer,
           "Read directly from the simulated NetworkInterface status values.",
           source="live")
    )

    rules = simulator.services.firewall.rules
    if rules:
        worst = max(rules, key=lambda rule: rule.packets_matched)
        options, answer = _options_with_answer(
            [f"rule {worst.rule_id} ({worst.action} {worst.protocol or 'any'} dport {worst.destination_port or 'any'})",
             "no rule matched", "an ACL entry", "the flood threshold"],
            f"rule {worst.rule_id} ({worst.action} {worst.protocol or 'any'} dport {worst.destination_port or 'any'})",
        )
        questions.append(
            _q("live_rule", "failure_reason",
               "Which firewall rule has matched the most packets?",
               options, answer,
               f"Rule {worst.rule_id} matched {worst.packets_matched} packet(s) and reports {worst.reason}.",
               source="live")
        )

    connections = simulator.get_transport_flows()
    tcp = [flow for flow in connections if flow.get("protocol") == "TCP"]
    if tcp:
        latest = tcp[-1]
        cwnd = latest.get("cwnd")
        options, answer = _options_with_answer(
            [f"{cwnd}", f"{float(cwnd or 1) / 2:.2f}", f"{float(cwnd or 1) * 2:.2f}", "1.00"],
            f"{cwnd}",
        )
        questions.append(
            _q("live_cwnd", "mcq",
               "What is the current TCP congestion window of the newest connection?",
               options, answer,
               f"Connection {latest['flow_id']} is {latest['state']} with cwnd {cwnd}.",
               source="live")
        )

    arp_rows = [row for name in hosts for row in simulator.arp_table(name)]
    if arp_rows:
        row = arp_rows[0]
        options, answer = _options_with_answer(
            [f"{row['ip_address']} -> {row['mac_address']}", f"{row['ip_address']} -> 00:00:00:00:00:00",
             "no ARP entry exists", f"{row['ip_address']} -> ff:ff:ff:ff:ff:ff"],
            f"{row['ip_address']} -> {row['mac_address']}",
        )
        questions.append(
            _q("live_arp", "identify_protocol",
               f"Which MAC address is currently associated with {row['ip_address']}?",
               options, answer,
               f"The simulated ARP cache of {hosts[0]} holds that binding.",
               source="live")
        )

    running = [row["name"] for row in simulator.services.service_status_all() if row["state"] == "RUNNING"]
    options, answer = _options_with_answer(
        [", ".join(running) if running else "no service is running", "all five services",
         "only DHCP", "only HTTP"],
        ", ".join(running) if running else "no service is running",
    )
    questions.append(
        _q("live_services", "mcq",
           "Which services are running on this network?",
           options, answer,
           "Read from the live service registry state (RUNNING).",
           source="live")
    )

    return questions[:limit]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class QuizEngine:
    """Tracks quiz attempts, scores, and explanations."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.index = 0
        self.active: List[Question] = []
        self.answers: Dict[str, Any] = dict(session.quiz_answers)
        self.finished = False

    def start(self, include_live: bool = True) -> Dict[str, Any]:
        questions = list(STATIC_QUESTIONS[:6])
        if include_live:
            questions.extend(live_questions(self.session, limit=4))
        self.active = questions
        self.index = 0
        self.finished = False
        self.answers = {}
        return self.state()

    def current(self) -> Optional[Question]:
        if self.index < len(self.active):
            return self.active[self.index]
        return None

    def submit(self, answer: Any) -> Dict[str, Any]:
        question = self.current()
        if question is None:
            raise ValueError("The quiz is already finished")
        correct = str(answer) == str(question.answer)
        self.answers[question.question_id] = {
            "answer": answer,
            "correct": correct,
        }
        self.index += 1
        self.finished = self.index >= len(self.active)
        self.session.quiz_answers = dict(self.answers)
        state = self.state()
        state["explanation"] = question.explanation
        state["correct_answer"] = question.options[question.answer]
        return state

    def next_question(self) -> Dict[str, Any]:
        self.index = min(self.index + 1, len(self.active))
        return self.state()

    def score(self) -> Dict[str, Any]:
        total = len(self.answers)
        correct = sum(1 for row in self.answers.values() if row.get("correct"))
        return {
            "answered": total,
            "correct": correct,
            "wrong": total - correct,
            "score_percent": round(correct / total * 100.0, 1) if total else 0.0,
        }

    def state(self) -> Dict[str, Any]:
        question = self.current()
        return {
            "questions": [item.to_dict() for item in self.active],
            "current": question.to_dict() if question else None,
            "position": self.index,
            "total": len(self.active),
            "finished": self.finished,
            "answers": dict(self.answers),
            "score": self.score(),
            "catalog_size": len(STATIC_QUESTIONS),
        }
