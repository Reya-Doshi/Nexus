"""Stage 11 - Challenge Mode with real, rule-based evaluation.

A challenge is a deliberately broken network plus a list of success criteria.
Every criterion is a predicate over the *live* simulator state, so a challenge
is only reported as PASS when the simulated network really satisfies it.  No
criterion can be satisfied by pressing a button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class Criterion:
    """One measurable success condition."""

    criterion_id: str
    description: str
    check: Callable[[Any], Tuple[bool, str]]

    def evaluate(self, session: Any) -> Dict[str, Any]:
        try:
            passed, detail = self.check(session)
        except Exception as exc:  # a broken state must never crash the lab
            passed, detail = False, f"check failed: {exc}"
        return {
            "id": self.criterion_id,
            "description": self.description,
            "passed": bool(passed),
            "detail": detail,
        }


@dataclass
class Challenge:
    challenge_id: str
    title: str
    brief: str
    symptoms: List[str]
    hints: List[str]
    preset: str
    setup: Callable[[Any], None]
    criteria: List[Criterion]
    requires_answer: bool = False
    solution: str = ""


# --------------------------------------------------------------------------
# Helpers used by the criteria (all read live simulator state)
# --------------------------------------------------------------------------


def _device(session: Any, name: str):
    return session.simulator.topology.get_device(name)


def _interface(session: Any, name: str, interface_id: str = "eth0"):
    return _device(session, name).get_interface(interface_id)


def _ip(session: Any, name: str) -> Optional[str]:
    try:
        return _interface(session, name).ip_address
    except Exception:
        return None


def _criterion_interface_up(session: Any, name: str) -> Tuple[bool, str]:
    interface = _interface(session, name)
    return interface.status == "UP", f"{name}.{interface.interface_id} is {interface.status}"


def _criterion_valid_ip(session: Any, name: str) -> Tuple[bool, str]:
    address = _ip(session, name)
    if not address:
        return False, f"{name} has no IPv4 address configured"
    if address.startswith("169.254") or address == "0.0.0.0":
        return False, f"{name} has an unusable address ({address})"
    return True, f"{name} is configured with {address}"


def _criterion_gateway(session: Any, name: str) -> Tuple[bool, str]:
    gateway = _device(session, name).default_gateway
    if not gateway:
        return False, f"{name} has no default gateway"
    return True, f"{name} default gateway is {gateway}"


def _criterion_ping(session: Any, source: str, destination: str) -> Tuple[bool, str]:
    result = session.simulator.diagnostic_ping(source, destination, count=3)
    ok = result["received"] > 0
    return ok, (
        f"ping {source} -> {destination}: {result['received']}/{result['sent']} answered"
        + (f" ({result['reason']})" if result.get("reason") else "")
    )


def _criterion_pdr(session: Any, source: str, destination: str) -> Tuple[bool, str]:
    result = session.simulator.diagnostic_ping(source, destination, count=3)
    ratio = 100.0 if not result["sent"] else (result["received"] / result["sent"] * 100.0)
    return ratio == 100.0, f"packet delivery ratio {ratio:.0f}% ({result['received']}/{result['sent']})"


def _criterion_route(session: Any, source: str, destination: str) -> Tuple[bool, str]:
    path, cost, status = session.simulator.get_current_route(source, destination)
    return bool(path), f"route {source} -> {destination}: {' → '.join(path) or 'none'} (cost {cost:.2f})"


def _criterion_service_running(session: Any, name: str, device: str) -> Tuple[bool, str]:
    instance = session.simulator.services.registry.get(name, device)
    if instance is None:
        return False, f"no {name} service on {device}"
    return instance.state == "RUNNING", f"{name} on {device} is {instance.state}"


# --------------------------------------------------------------------------
# Setups
# --------------------------------------------------------------------------


def _setup_connectivity(session: Any) -> None:
    session.load_preset("simple_lan", record=False)
    session.install_service("DHCP", "Server1")
    device = _device(session, "PC1")
    device.get_interface("eth0").ip_address = None
    device.get_interface("eth0").prefix = 24
    device.default_gateway = None
    session.simulator.fail_interface("PC1", "eth0")
    session._update_document()


def _setup_http_scope(session: Any) -> None:
    session.load_preset("self_healing", record=False)
    session.install_service("HTTP", "Server1")
    session.add_firewall_rule(
        action="DENY",
        source_ip=_ip(session, "PC2"),
        destination_ip=_ip(session, "Server1"),
        protocol="TCP",
        destination_port=80,
    )
    session._update_document()


def _setup_self_healing(session: Any) -> None:
    session.load_preset("redundant", record=False)
    session.simulator.fail_link("R1", "R2")
    session.simulator.tick(4.0)
    # Everything the student still has to do happens after this mark.
    session.challenge_marks["self_healing"] = session.simulator.time
    session._update_document()


def _setup_qos(session: Any) -> None:
    session.load_preset("simple_lan", record=False)
    session.set_scheduler("fifo")
    session.set_qos_config({"VoIP": 1.0, "FTP": 5.0, "HTTP": 3.0, "Video": 2.0, "Emergency": 0.5})
    session.configure_link("SW1", "Server1", {"bandwidth": 2.0, "congestion": 0.8, "latency": 40.0})
    session._update_document()


def _setup_dns(session: Any) -> None:
    session.load_preset("simple_lan", record=False)
    session.install_service("DNS", "Server1")
    session.dns_add_record("Server1", "server.netadapt.local", "10.99.99.99", ttl=60)
    session.stop_service("DNS", "Server1")
    session._update_document()


def _setup_arp_spoof(session: Any) -> None:
    session.load_preset("self_healing", record=False)
    session.configure_security({"arp_detection": False})
    session.arp_spoof("PC2", "PC1", _ip(session, "Server1"), detect=False)
    session._update_document()


# --------------------------------------------------------------------------
# Challenges

def _check_failed_link(session: Any) -> Tuple[bool, str]:
    failed = session.simulator.get_failed_links()
    ok = any(set(pair) == {"R1", "R2"} for pair in failed)
    return ok, f"failed links: {failed or 'none'}"


def _check_alternate_route(session: Any) -> Tuple[bool, str]:
    path = session.simulator.get_current_route("PC1", "PC2")[0]
    uses_alternate = "R3" in path and "R2" in path
    return uses_alternate, f"current route PC1 -> PC2: {' → '.join(path) or 'none'}"


def _check_traffic_after_failure(session: Any) -> Tuple[bool, str]:
    mark = session.challenge_marks.get("self_healing")
    flows = [
        flow
        for flow in session.simulator.active_flows.values()
        if mark is None or flow.start_time >= mark
    ]
    routes = [flow.current_route for flow in flows if flow.current_route]
    return bool(routes), f"{len(flows)} flow(s) started after the failure, {len(routes)} with a route"


def _check_recalculated(session: Any) -> Tuple[bool, str]:
    count = sum(
        1
        for event in session.simulator.event_logger.events
        if event.event_type == "ROUTE_RECALCULATED"
    )
    return count > 0, f"{count} route recalculation event(s) recorded"


def _check_scheduler(session: Any) -> Tuple[bool, str]:
    name = session.simulator.scheduler_name
    return name in {"priority", "wfq"}, f"scheduler is {name}"


def _check_voip_priority(session: Any) -> Tuple[bool, str]:
    priorities = session.simulator.get_priority_config()
    best = max(priorities.items(), key=lambda item: item[1])
    return priorities.get("VoIP", 0) >= best[1], (
        f"priorities {priorities} (highest: {best[0]})"
    )


def _check_voip_flow(session: Any) -> Tuple[bool, str]:
    flows = [
        flow
        for flow in session.simulator.get_transport_flows()
        if flow.get("traffic_class") == "VoIP"
    ]
    delivered = sum(flow.get("packets_delivered", flow.get("packets_sent", 0)) for flow in flows)
    return delivered > 0, f"{len(flows)} VoIP flow(s), {delivered} datagram(s) delivered"


def _check_dns_record(session: Any) -> Tuple[bool, str]:
    expected = _ip(session, "Server1")
    records = session.simulator.services.dns_zones.get("Server1", {})
    record = records.get("server.netadapt.local")
    if record is None:
        return False, "no A record for server.netadapt.local on Server1"
    return record.address == expected, f"record points to {record.address}, Server1 is {expected}"


def _check_dns_resolves(session: Any) -> Tuple[bool, str]:
    expected = _ip(session, "Server1")
    result = session.simulator.dns_query("PC1", "server.netadapt.local", "Server1", use_cache=False)
    if not result.get("success"):
        return False, f"resolution failed: {result.get('reason')}"
    return result.get("address") == expected, f"PC1 resolved {result.get('address')} (expected {expected})"


def _check_arp_detection(session: Any) -> Tuple[bool, str]:
    enabled = session.simulator.services.arp_detection
    return enabled, f"ARP detection is {'enabled' if enabled else 'disabled'}"


def _check_arp_conflict(session: Any) -> Tuple[bool, str]:
    conflicts = session.simulator.services.arp_conflicts
    if not conflicts:
        return False, "no ARP conflict has been detected yet"
    last = conflicts[-1]
    return True, (
        f"conflict on {last['device']}: {last['ip_address']} {last['known_mac']} -> {last['new_mac']}"
    )


def _check_identified_attacker(session: Any) -> Tuple[bool, str]:
    answer = session.challenge_answers.get("arp_spoof", {}).get("attacker")
    conflicts = session.simulator.services.arp_conflicts
    if not answer:
        return False, "no attacker submitted yet"
    expected = None
    if conflicts:
        new_mac = conflicts[-1]["new_mac"]
        expected = next(
            (
                name
                for name, device in session.simulator.topology.devices.items()
                if any(interface.mac_address == new_mac for interface in device.interfaces)
            ),
            None,
        )
    ok = expected is not None and str(answer) == expected
    return ok, f"submitted {answer!r}" + (f", the conflicting MAC belongs to {expected}" if expected else "")


# --------------------------------------------------------------------------


def _http_status(session: Any, client: str) -> Tuple[bool, str]:
    result = session.simulator.http_request(client, "Server1")
    status = result.get("status")
    if status == 200:
        return True, f"{client} received HTTP {status}"
    return False, f"{client} request failed: {result.get('reason') or status}"


def _no_deny_rule(session: Any, source: str, destination: str) -> Tuple[bool, str]:
    from services_security import match_ip

    layer = session.simulator.services
    source_ip = _ip(session, source)
    destination_ip = _ip(session, destination)
    for rule in layer.firewall.rules:
        if rule.action != "DENY":
            continue
        if rule.destination_port and int(rule.destination_port) != 80:
            continue
        if not match_ip(rule.source_ip, source_ip):
            continue
        if not match_ip(rule.destination_ip, destination_ip):
            continue
        return False, f"firewall rule {rule.rule_id} still denies {source} -> {destination}:80"
    return True, f"no firewall rule denies {source} -> {destination}:80"


CHALLENGES: List[Challenge] = [
    Challenge(
        challenge_id="connectivity",
        title="PC1 cannot communicate with Server1",
        brief=(
            "PC1 has no usable network configuration. Diagnose the interface, address, "
            "and gateway, then restore connectivity to Server1."
        ),
        symptoms=[
            "PC1 interface may be administratively down",
            "PC1 has no IP address or an address from the wrong subnet",
            "no default gateway is configured",
            "a route or the path may still be broken",
        ],
        hints=[
            "Check whether PC1's interface is UP.",
            "Run 'ipconfig' or 'ipconfig /all' on PC1 to see the current configuration.",
            "Server1 is running a DHCP server - 'ipconfig /renew' on PC1 can restore the address, mask, and gateway.",
            "Verify with 'ping <Server1 ip>' from PC1 once the address is back.",
        ],
        preset="simple_lan",
        setup=_setup_connectivity,
        criteria=[
            Criterion("interface", "PC1's interface is UP", lambda s: _criterion_interface_up(s, "PC1")),
            Criterion("address", "PC1 has a valid IPv4 address", lambda s: _criterion_valid_ip(s, "PC1")),
            Criterion("gateway", "PC1 has a default gateway", lambda s: _criterion_gateway(s, "PC1")),
            Criterion("route", "A route to Server1 exists", lambda s: _criterion_route(s, "PC1", "Server1")),
            Criterion("ping", "PC1 can ping Server1", lambda s: _criterion_ping(s, "PC1", "Server1")),
            Criterion("pdr", "Packet delivery ratio is 100%", lambda s: _criterion_pdr(s, "PC1", "Server1")),
        ],
        solution="Bring eth0 up and run 'ipconfig /renew' so DHCP supplies address, mask, and gateway.",
    ),
    Challenge(
        challenge_id="http_scope",
        title="HTTP works from PC1 but not from PC2",
        brief=(
            "The HTTP service is healthy, but one client is refused. Find the policy that "
            "blocks it and correct the configuration."
        ),
        symptoms=[
            "a firewall rule may deny the client",
            "an ACL attached to a device on the path may deny it",
            "the route from that client may be broken",
            "the HTTP service may be stopped",
        ],
        hints=[
            "Compare a successful PC1 request with the failing one - the difference is the source.",
            "Run 'show firewall' on Server1 to list the active rules.",
            "Remove or narrow the rule that denies the client to TCP port 80.",
        ],
        preset="self_healing",
        setup=_setup_http_scope,
        criteria=[
            Criterion("service", "HTTP on Server1 is RUNNING", lambda s: _criterion_service_running(s, "HTTP", "Server1")),
            Criterion("pc1", "PC1 can fetch / with HTTP 200", lambda s: _http_status(s, "PC1")),
            Criterion("pc2", "PC2 can fetch / with HTTP 200", lambda s: _http_status(s, "PC2")),
            Criterion("policy", "No rule denies the client to port 80", lambda s: _no_deny_rule(s, "PC2", "Server1")),
        ],
        solution="Remove the DENY rule that matches the blocked client's source address for TCP/80.",
    ),
    Challenge(
        challenge_id="self_healing",
        title="Traffic must survive an R1-R2 link failure",
        brief=(
            "The R1-R2 link carrying PC1-PC2 traffic has failed. Verify that the adaptive "
            "router recalculated a path through the redundant topology and that PC1 can "
            "still reach PC2."
        ),
        symptoms=[
            "the direct R1-R2 path is gone",
            "the alternate path must use R1-R3 and R3-R2",
            "route recalculation happens on the existing topology",
        ],
        hints=[
            "Run 'show ip route' on R1 to see which path is now used.",
            "Start traffic from PC1 to PC2 (Play/Step) so the reroute happens in the event log.",
            "The topology is redundant: R1-R3 and R3-R2 still exist, so PC1 to PC2 should keep working.",
        ],
        preset="redundant",
        setup=_setup_self_healing,
        criteria=[
            Criterion("failed", "The R1-R2 link is still failed", _check_failed_link),
            Criterion("route", "A route from PC1 to PC2 exists", lambda s: _criterion_route(s, "PC1", "PC2")),
            Criterion("alternate", "The current path uses the R1-R3-R2 alternate route", _check_alternate_route),
            Criterion("traffic", "Traffic was started after the failure", _check_traffic_after_failure),
            Criterion("ping", "PC1 can still ping PC2", lambda s: _criterion_ping(s, "PC1", "PC2")),
        ],
        solution="No repair is needed: the redundant topology already provides the alternate path. Verify it.",
    ),
    Challenge(
        challenge_id="qos_priority",
        title="VoIP must get priority during congestion",
        brief=(
            "The congested link serves VoIP last because FIFO and the current priorities favour "
            "bulk traffic. Reconfigure QoS and then send VoIP traffic."
        ),
        symptoms=[
            "FIFO serves classes in arrival order",
            "the configured priorities currently rank FTP above VoIP",
            "a scheduler that understands classes is required",
        ],
        hints=[
            "Open Lab policies and look at the class priorities - VoIP is currently the lowest.",
            "Switch the scheduler to Priority Queue or WFQ, then raise the VoIP priority above FTP.",
            "Start a VoIP flow from PC1 to Server1 and check the per-class latency table.",
        ],
        preset="simple_lan",
        setup=_setup_qos,
        criteria=[
            Criterion("scheduler", "The scheduler understands classes", _check_scheduler),
            Criterion("priority", "VoIP has the highest class priority", _check_voip_priority),
            Criterion("flow", "A VoIP flow has been delivered", _check_voip_flow),
        ],
        solution="Use the Priority Queue or WFQ scheduler and give VoIP the highest priority, then send VoIP traffic.",
    ),
    Challenge(
        challenge_id="dns_repair",
        title="DNS resolution fails for server.netadapt.local",
        brief=(
            "The DNS service is not answering and its zone holds a wrong address. Repair the "
            "service and the record so PC1 resolves the name to Server1."
        ),
        symptoms=[
            "the DNS service may be stopped",
            "the A record may point to the wrong address",
            "resolution needs UDP/53 to reach the DNS server",
        ],
        hints=[
            "Run 'show services' on Server1 and look at the DNS state.",
            "Start the DNS service from the Services & Security panel.",
            "Correct the A record so server.netadapt.local maps to Server1's interface address.",
        ],
        preset="simple_lan",
        setup=_setup_dns,
        criteria=[
            Criterion("service", "DNS on Server1 is RUNNING", lambda s: _criterion_service_running(s, "DNS", "Server1")),
            Criterion("record", "The A record matches Server1's address", _check_dns_record),
            Criterion("resolve", "PC1 resolves the hostname", _check_dns_resolves),
        ],
        solution="Start the DNS service and set server.netadapt.local to Server1's interface address.",
    ),
    Challenge(
        challenge_id="arp_spoof",
        title="An ARP spoofing attack hit the LAN",
        brief=(
            "PC1's simulated ARP cache was poisoned by PC2. Turn detection on, inspect the "
            "conflict, and identify the attacker."
        ),
        symptoms=[
            "one IP address is now mapped to two MAC addresses",
            "the legitimate entry came from Server1",
            "the attacker is one of the hosts on the LAN",
        ],
        hints=[
            "Run 'arp -a' on PC1 to see the entries it currently trusts.",
            "Enable ARP detection in the Services & Security panel and re-run the spoofing scenario.",
            "Compare the known MAC with the new MAC: the device holding the new MAC is the attacker.",
        ],
        preset="self_healing",
        setup=_setup_arp_spoof,
        criteria=[
            Criterion("detection", "ARP detection is enabled", _check_arp_detection),
            Criterion("conflict", "A conflict was detected for the gateway IP", _check_arp_conflict),
            Criterion("attacker", "The attacker was correctly identified", _check_identified_attacker),
        ],
        requires_answer=True,
        solution="Enable detection, re-run the scenario, then report PC2 as the attacker.",
    ),
]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class ChallengeEngine:
    """Runs challenges against the live lab session and evaluates real state."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.active: Optional[str] = None
        self.attempts = 0
        self.hints_used = 0
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.history: List[Dict[str, Any]] = []

    # -- catalog ------------------------------------------------------
    @staticmethod
    def catalog() -> List[Dict[str, Any]]:
        return [
            {
                "id": item.challenge_id,
                "title": item.title,
                "brief": item.brief,
                "symptoms": list(item.symptoms),
                "hint_count": len(item.hints),
                "criteria": [
                    {"id": criterion.criterion_id, "description": criterion.description}
                    for criterion in item.criteria
                ],
                "requires_answer": item.requires_answer,
            }
            for item in CHALLENGES
        ]

    @staticmethod
    def require(challenge_id: str) -> Challenge:
        for item in CHALLENGES:
            if item.challenge_id == challenge_id:
                return item
        raise ValueError(f"Unknown challenge: {challenge_id}")

    # -- lifecycle ----------------------------------------------------
    def start(self, challenge_id: str) -> Dict[str, Any]:
        item = self.require(challenge_id)
        self.active = challenge_id
        self.attempts = 0
        self.hints_used = 0
        self.completed_at = None
        item.setup(self.session)
        self.started_at = time()
        return self.state()

    def reset(self) -> Dict[str, Any]:
        if self.active is None:
            return self.state()
        return self.start(self.active)

    def hint(self) -> Dict[str, Any]:
        if self.active is None:
            raise ValueError("No challenge is active")
        item = self.require(self.active)
        index = min(self.hints_used, len(item.hints) - 1)
        self.hints_used += 1
        return {
            "hint": index + 1,
            "total": len(item.hints),
            "text": item.hints[index],
            "hints_used": self.hints_used,
        }

    def submit_answer(self, key: str, value: str) -> Dict[str, Any]:
        if self.active is None:
            raise ValueError("No challenge is active")
        self.session.challenge_answers.setdefault(self.active, {})[key] = value
        return self.evaluate()

    def evaluate(self) -> Dict[str, Any]:
        if self.active is None:
            return {
                "active": None,
                "challenges": self.catalog(),
                "history": self.history[-10:],
            }
        item = self.require(self.active)
        self.attempts += 1
        results = [criterion.evaluate(self.session) for criterion in item.criteria]
        failed = [row for row in results if not row["passed"]]
        passed = not failed
        if passed and self.completed_at is None:
            self.completed_at = time()
            self.history.append(
                {
                    "challenge": item.challenge_id,
                    "title": item.title,
                    "attempts": self.attempts,
                    "hints_used": self.hints_used,
                    "seconds": round(self.completed_at - (self.started_at or self.completed_at), 2),
                }
            )
        return {
            "active": item.challenge_id,
            "title": item.title,
            "brief": item.brief,
            "symptoms": list(item.symptoms),
            "status": "PASS" if passed else "NOT COMPLETE",
            "passed": passed,
            "criteria": results,
            "failing": [row["description"] for row in failed],
            "attempts": self.attempts,
            "hints_used": self.hints_used,
            "hints_available": len(item.hints),
            "elapsed_seconds": round(
                (self.completed_at or time()) - (self.started_at or time()), 2
            ),
            "requires_answer": item.requires_answer,
            "answers": self.session.challenge_answers.get(item.challenge_id, {}),
            "solution_available": passed,
            "history": self.history[-10:],
        }

    def state(self) -> Dict[str, Any]:
        state = self.evaluate()
        state["challenges"] = self.catalog()
        return state
