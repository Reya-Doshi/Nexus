"""Deterministic UDP and TCP transport services for NetAdapt.

Transport packets are regular :class:`qos.Packet` instances.  They therefore use
NetAdapt's existing routing, failure handling, QoS scheduler, packet loss model,
event logger, animation state, and simulation clock.  This module only adds the
transport state machine and its protocol-specific reliability semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from events import EventType
from qos import Packet


class TCPState(str, Enum):
    CLOSED = "CLOSED"
    SYN_SENT = "SYN_SENT"
    SYN_RECEIVED = "SYN_RECEIVED"
    ESTABLISHED = "ESTABLISHED"
    FIN_WAIT = "FIN_WAIT"
    CLOSE_WAIT = "CLOSE_WAIT"
    LAST_ACK = "LAST_ACK"
    TIME_WAIT = "TIME_WAIT"


@dataclass
class TransportPacket:
    """Transport metadata attached to an existing simulated network packet."""

    protocol: str
    source: str
    destination: str
    source_port: int
    destination_port: int
    payload_size: int = 0
    flow_id: str = ""
    sequence_number: Optional[int] = None
    acknowledgement_number: Optional[int] = None
    flags: List[str] = field(default_factory=list)
    window_size: int = 0
    kind: str = "DATA"
    traffic_class: str = "HTTP"
    packet_id: Optional[int] = None
    network_packet_id: Optional[int] = None
    sent_time: float = 0.0
    delivered_time: Optional[float] = None
    status: str = "ACTIVE"
    route: List[str] = field(default_factory=list)
    retransmission: int = 0
    latency: Optional[float] = None
    queue_wait_time: Optional[float] = None
    drop_reason: Optional[str] = None

    @property
    def label(self) -> str:
        if self.kind == "CONTROL":
            return "+".join(self.flags)
        if self.kind == "ACK":
            return "ACK"
        if self.kind == "FIN":
            return "FIN"
        return "DATA"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.packet_id,
            "packet_id": self.packet_id,
            "network_packet_id": self.network_packet_id,
            "protocol": self.protocol,
            "source": self.source,
            "destination": self.destination,
            "source_endpoint": f"{self.source}:{self.source_port}",
            "destination_endpoint": f"{self.destination}:{self.destination_port}",
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "payload_size": self.payload_size,
            "flow_id": self.flow_id,
            "sequence_number": self.sequence_number,
            "acknowledgement_number": self.acknowledgement_number,
            "flags": list(self.flags),
            "window_size": self.window_size,
            "kind": self.kind,
            "traffic_class": self.traffic_class,
            "sent_time": self.sent_time,
            "delivered_time": self.delivered_time,
            "status": self.status,
            "route": list(self.route),
            "retransmission": self.retransmission,
            "latency": self.latency,
            "queue_wait_time": self.queue_wait_time,
            "drop_reason": self.drop_reason,
        }


@dataclass
class OutstandingSegment:
    sequence_number: int
    packet: TransportPacket
    original_send_time: float
    last_send_time: float
    retransmissions: int = 0
    timeout_started: bool = False


@dataclass
class UDPFlow:
    flow_id: str
    source: str
    destination: str
    source_port: int
    destination_port: int
    traffic_class: str = "VoIP"
    payload_size: int = 1000
    started_at: float = 0.0
    packets: List[TransportPacket] = field(default_factory=list)
    bytes_sent: int = 0
    bytes_delivered: int = 0
    packets_delivered: int = 0
    packets_lost: int = 0
    elapsed_time: float = 0.0
    route: List[str] = field(default_factory=list)
    route_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        sent = len(self.packets)
        duration = max(self.elapsed_time, 0.001)
        latencies = [
            packet.latency
            for packet in self.packets
            if packet.status == "DELIVERED" and packet.latency is not None
        ]
        return {
            "protocol": "UDP",
            "flow_id": self.flow_id,
            "source": self.source,
            "destination": self.destination,
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "state": "CONNECTIONLESS",
            "traffic_class": self.traffic_class,
            "payload_size": self.payload_size,
            "packets_sent": sent,
            "packets_delivered": self.packets_delivered,
            "packets_lost": self.packets_lost,
            "packet_delivery_ratio": (self.packets_delivered / sent * 100.0) if sent else 100.0,
            "bytes_sent": self.bytes_sent,
            "bytes_transferred": self.bytes_delivered,
            "retransmissions": 0,
            "average_latency": sum(latencies) / len(latencies) if latencies else 0.0,
            "minimum_latency": min(latencies) if latencies else 0.0,
            "maximum_latency": max(latencies) if latencies else 0.0,
            "current_rtt_ms": None,
            "average_rtt_ms": None,
            "min_rtt_ms": None,
            "max_rtt_ms": None,
            "throughput": self.bytes_delivered / duration,
            "route": list(self.route),
            "route_history": list(self.route_history),
            "packets": [packet.to_dict() for packet in self.packets],
        }


@dataclass
class TCPConnection:
    flow_id: str
    source: str
    destination: str
    source_port: int
    destination_port: int
    traffic_class: str = "HTTP"
    state: TCPState = TCPState.CLOSED
    initial_cwnd: int = 1
    cwnd: float = 1.0
    ssthresh: float = 16.0
    receiver_window: int = 8
    sender_window: int = 8
    timeout: float = 0.1
    rto: float = 0.1
    srtt: Optional[float] = None
    rttvar: float = 0.0
    rtt_samples: List[float] = field(default_factory=list)
    next_sequence: int = 1
    cumulative_ack: int = 0
    highest_ack: int = 0
    receiver_ack: int = 0
    received_sequences: List[int] = field(default_factory=list)
    outstanding: Dict[int, OutstandingSegment] = field(default_factory=dict)
    packets: List[TransportPacket] = field(default_factory=list)
    route: List[str] = field(default_factory=list)
    route_history: List[Dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    elapsed_time: float = 0.0
    setup_completed: Optional[float] = None
    ack_count: int = 0
    duplicate_ack_count: int = 0
    timeout_count: int = 0
    retransmission_count: int = 0
    data_packets_sent: int = 0
    data_packets_delivered: int = 0
    response_packets_sent: int = 0
    data_packets_lost: int = 0
    bytes_sent: int = 0
    bytes_transferred: int = 0
    max_cwnd: float = 1.0
    cwnd_samples: List[float] = field(default_factory=list)
    congestion_phase: str = "SLOW_START"
    last_timeout_time: Optional[float] = None
    state_history: List[Dict[str, Any]] = field(default_factory=list)
    termination_phase: str = ""

    @property
    def effective_window(self) -> int:
        return max(0, int(min(self.cwnd, float(self.receiver_window))))

    @property
    def packets_in_flight(self) -> int:
        return len(self.outstanding)

    @property
    def bytes_in_flight(self) -> int:
        return sum(segment.packet.payload_size for segment in self.outstanding.values())

    def transition(self, state: TCPState, time: float, reason: str) -> None:
        self.state = state
        self.state_history.append(
            {"time": time, "state": state.value, "reason": reason}
        )

    def to_dict(self) -> Dict[str, Any]:
        samples = self.rtt_samples
        duration = max(self.elapsed_time, 0.001)
        all_sent = len(self.packets)
        all_delivered = sum(packet.status == "DELIVERED" for packet in self.packets)
        all_lost = sum(packet.status == "DROPPED" for packet in self.packets)
        data_latencies = [
            packet.latency
            for packet in self.packets
            if packet.kind == "DATA"
            and packet.status == "DELIVERED"
            and packet.latency is not None
        ]
        return {
            "protocol": "TCP",
            "flow_id": self.flow_id,
            "source": self.source,
            "destination": self.destination,
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "state": self.state.value,
            "state_history": list(self.state_history),
            "initial_cwnd": self.initial_cwnd,
            "cwnd": self.cwnd,
            "max_cwnd": self.max_cwnd,
            "average_cwnd": (
                sum(self.cwnd_samples) / len(self.cwnd_samples)
                if self.cwnd_samples
                else self.cwnd
            ),
            "ssthresh": self.ssthresh,
            "congestion_phase": self.congestion_phase,
            "receiver_window": self.receiver_window,
            "sender_window": max(0, self.receiver_window - self.packets_in_flight),
            "send_window": self.effective_window,
            "effective_window": self.effective_window,
            "packets_in_flight": self.packets_in_flight,
            "bytes_in_flight": self.bytes_in_flight,
            "outstanding_sequences": sorted(self.outstanding),
            "next_sequence": self.next_sequence,
            "cumulative_ack": self.cumulative_ack,
            "highest_ack": self.highest_ack,
            "receiver_ack": self.receiver_ack,
            "received_sequences": sorted(set(self.received_sequences)),
            "ack_count": self.ack_count,
            "duplicate_ack_count": self.duplicate_ack_count,
            "timeout_count": self.timeout_count,
            "retransmission_count": self.retransmission_count,
            "packets_sent": all_sent,
            "packets_delivered": all_delivered,
            "packets_lost": all_lost,
            "packet_delivery_ratio": (all_delivered / all_sent * 100.0) if all_sent else 100.0,
            "data_packets_sent": self.data_packets_sent,
            "data_packets_delivered": self.data_packets_delivered,
            "data_packets_lost": self.data_packets_lost,
            "data_packet_delivery_ratio": (
                self.data_packets_delivered / self.data_packets_sent * 100.0
                if self.data_packets_sent
                else 100.0
            ),
            "bytes_sent": self.bytes_sent,
            "bytes_transferred": self.bytes_transferred,
            "response_packets_sent": self.response_packets_sent,
            "average_latency": (
                sum(data_latencies) / len(data_latencies)
                if data_latencies
                else 0.0
            ),
            "minimum_latency": min(data_latencies) if data_latencies else 0.0,
            "maximum_latency": max(data_latencies) if data_latencies else 0.0,
            "current_rtt_ms": samples[-1] * 1000 if samples else None,
            "average_rtt_ms": sum(samples) / len(samples) * 1000 if samples else None,
            "min_rtt_ms": min(samples) * 1000 if samples else None,
            "max_rtt_ms": max(samples) * 1000 if samples else None,
            "throughput": self.bytes_transferred / duration,
            "timeout": self.timeout,
            "rto": self.rto,
            "setup_time_ms": (
                (self.setup_completed - self.started_at) * 1000
                if self.setup_completed is not None
                else None
            ),
            "route": list(self.route),
            "route_history": list(self.route_history),
            "packets": [packet.to_dict() for packet in self.packets],
        }


class TransportLayer:
    """Protocol state layered onto NetAdapt's single packet simulator."""

    def __init__(self, simulator: Any):
        self.simulator = simulator
        self.connections: Dict[str, TCPConnection] = {}
        self.udp_flows: Dict[str, UDPFlow] = {}
        self.packet_map: Dict[int, TransportPacket] = {}
        self._transport_packet_map: Dict[int, TransportPacket] = {}
        self._tcp = 1
        self._udp = 1
        self._packet = 1

    def _log(
        self,
        event: EventType,
        message: str,
        connection: Optional[TCPConnection] = None,
        **details: Any,
    ) -> None:
        flow_id = connection.flow_id if connection else details.pop("flow_id", None)
        self.simulator._log_event(event, message, flow_id=flow_id, **details)

    def _validate_endpoints(self, source: str, destination: str) -> None:
        if (
            source not in self.simulator.topology.devices
            or destination not in self.simulator.topology.devices
        ):
            raise ValueError("Transport source and destination must be existing devices")
        if source == destination:
            raise ValueError("Transport source and destination must differ")

    def _enqueue(self, packet: TransportPacket, creation_time: Optional[float] = None) -> TransportPacket:
        network_packet = self.simulator.generate_packet(
            packet.source,
            packet.destination,
            packet.traffic_class,
            max(1, packet.payload_size),
            packet.flow_id,
            creation_time=creation_time,
        )
        packet.packet_id = self._packet
        self._packet += 1
        packet.network_packet_id = network_packet.packet_id
        packet.sent_time = self.simulator.time
        network_packet.transport = packet
        network_packet.transport_protocol = packet.protocol
        network_packet.transport_kind = packet.kind
        network_packet.transport_flags = list(packet.flags)
        network_packet.transport_flow_id = packet.flow_id
        network_packet.transport_sequence = packet.sequence_number
        network_packet.transport_ack = packet.acknowledgement_number
        self.packet_map[network_packet.packet_id] = packet
        self._transport_packet_map[packet.packet_id] = packet
        return packet

    def _control(
        self,
        connection: TCPConnection,
        flags: List[str],
        kind: str,
        reverse: bool = False,
        seq: Optional[int] = None,
        ack: Optional[int] = None,
    ) -> TransportPacket:
        if reverse:
            source, destination = connection.destination, connection.source
            source_port, destination_port = (
                connection.destination_port,
                connection.source_port,
            )
        else:
            source, destination = connection.source, connection.destination
            source_port, destination_port = (
                connection.source_port,
                connection.destination_port,
            )
        packet = TransportPacket(
            "TCP",
            source,
            destination,
            source_port,
            destination_port,
            0,
            connection.flow_id,
            seq,
            ack,
            list(flags),
            connection.receiver_window,
            kind,
            connection.traffic_class,
        )
        connection.packets.append(packet)
        return self._enqueue(packet)

    def create_tcp_connection(
        self,
        source: str,
        destination: str,
        source_port: int = 5000,
        destination_port: int = 8080,
        initial_cwnd: int = 1,
        receiver_window: int = 8,
        ssthresh: float = 16.0,
        timeout: float = 0.1,
        traffic_class: str = "HTTP",
    ) -> TCPConnection:
        self._validate_endpoints(source, destination)
        flow_id = f"TCP-{self._tcp:03d}"
        self._tcp += 1
        connection = TCPConnection(
            flow_id=flow_id,
            source=source,
            destination=destination,
            source_port=int(source_port),
            destination_port=int(destination_port),
            traffic_class=traffic_class,
            state=TCPState.CLOSED,
            initial_cwnd=max(1, int(initial_cwnd)),
            cwnd=max(1.0, float(initial_cwnd)),
            ssthresh=max(1.0, float(ssthresh)),
            receiver_window=max(1, int(receiver_window)),
            sender_window=max(1, int(receiver_window)),
            timeout=max(0.001, float(timeout)),
            rto=max(0.001, float(timeout)),
            started_at=self.simulator.time,
        )
        self.connections[flow_id] = connection
        connection.transition(TCPState.CLOSED, self.simulator.time, "created")
        self._log(
            EventType.TCP_CONNECTION_STARTED,
            f"TCP connection started {source}:{source_port} → {destination}:{destination_port}",
            connection,
        )
        connection.transition(TCPState.SYN_SENT, self.simulator.time, "SYN queued")
        syn = self._control(connection, ["SYN"], "CONTROL", seq=0)
        self._log(
            EventType.TCP_SYN_SENT,
            "TCP SYN sent",
            connection,
            sequence_number=0,
            packet_id=syn.packet_id,
        )
        return connection

    def create_udp_flow(
        self,
        source: str,
        destination: str,
        source_port: int = 5001,
        destination_port: int = 8080,
        payload_size: int = 1000,
        traffic_class: str = "VoIP",
    ) -> UDPFlow:
        self._validate_endpoints(source, destination)
        flow_id = f"UDP-{self._udp:03d}"
        self._udp += 1
        flow = UDPFlow(
            flow_id,
            source,
            destination,
            int(source_port),
            int(destination_port),
            traffic_class,
            max(1, int(payload_size)),
            self.simulator.time,
        )
        self.udp_flows[flow_id] = flow
        self._log(
            EventType.UDP_FLOW_STARTED,
            f"UDP flow started {source}:{source_port} → {destination}:{destination_port}",
            flow_id=flow_id,
        )
        return flow

    def send_udp_data(
        self,
        flow_id: str,
        packet_count: int = 1,
        payload_size: Optional[int] = None,
    ) -> List[TransportPacket]:
        flow = self.udp_flows[flow_id]
        sent: List[TransportPacket] = []
        for _ in range(max(1, int(packet_count))):
            packet = TransportPacket(
                "UDP",
                flow.source,
                flow.destination,
                flow.source_port,
                flow.destination_port,
                int(payload_size or flow.payload_size),
                flow.flow_id,
                kind="DATA",
                traffic_class=flow.traffic_class,
            )
            flow.packets.append(packet)
            flow.bytes_sent += packet.payload_size
            sent.append(self._enqueue(packet))
            self._log(
                EventType.UDP_DATA_SENT,
                "UDP data sent",
                flow_id=flow.flow_id,
                packet_id=packet.packet_id,
                payload_size=packet.payload_size,
            )
        return sent

    def enqueue_raw(
        self,
        protocol: str,
        source: str,
        destination: str,
        source_port: int,
        destination_port: int,
        payload_size: int = 512,
        *,
        flags: Optional[List[str]] = None,
        kind: str = "DATA",
        traffic_class: str = "HTTP",
        flow_id: Optional[str] = None,
        sequence_number: Optional[int] = None,
        acknowledgement_number: Optional[int] = None,
        creation_time: Optional[float] = None,
    ) -> TransportPacket:
        """Enqueue one service/security transport packet without a flow object.

        This is used by controlled attack scenarios and protocol simulations;
        it deliberately uses the same network packet path as every other
        transport packet and therefore does not bypass routing or QoS.
        """
        self._validate_endpoints(source, destination)
        protocol = str(protocol).upper()
        if protocol not in {"TCP", "UDP"}:
            raise ValueError("Raw transport protocol must be TCP or UDP")
        packet = TransportPacket(
            protocol,
            source,
            destination,
            int(source_port),
            int(destination_port),
            max(1, int(payload_size)),
            flow_id or f"RAW-{protocol}-{self._packet:03d}",
            sequence_number,
            acknowledgement_number,
            list(flags or []),
            0,
            kind,
            traffic_class,
        )
        return self._enqueue(packet, creation_time=creation_time)

    def _ack(
        self,
        connection: TCPConnection,
        acknowledgement: int,
    ) -> TransportPacket:
        return self._control(
            connection,
            ["ACK"],
            "ACK",
            reverse=True,
            ack=acknowledgement,
        )

    def send_tcp_data(
        self,
        flow_id: str,
        packet_count: int = 1,
        payload_size: int = 1000,
    ) -> List[TransportPacket]:
        connection = self.connections[flow_id]
        if connection.state != TCPState.ESTABLISHED:
            raise ValueError("TCP connection must be ESTABLISHED before sending data")
        available = max(0, connection.effective_window - connection.packets_in_flight)
        sent: List[TransportPacket] = []
        for _ in range(min(max(0, int(packet_count)), available)):
            sequence = connection.next_sequence
            packet = TransportPacket(
                "TCP",
                connection.source,
                connection.destination,
                connection.source_port,
                connection.destination_port,
                max(1, int(payload_size)),
                connection.flow_id,
                sequence,
                connection.cumulative_ack,
                ["ACK"],
                connection.receiver_window,
                "DATA",
                connection.traffic_class,
            )
            connection.packets.append(packet)
            connection.outstanding[sequence] = OutstandingSegment(
                sequence,
                packet,
                self.simulator.time,
                self.simulator.time,
            )
            connection.next_sequence += 1
            connection.data_packets_sent += 1
            connection.bytes_sent += packet.payload_size
            sent.append(self._enqueue(packet))
            self._log(
                EventType.TCP_DATA_SENT,
                f"TCP data sent seq={sequence}",
                connection,
                sequence_number=sequence,
                payload_size=packet.payload_size,
                packet_id=packet.packet_id,
            )
        return sent

    def send_tcp_response(
        self,
        flow_id: str,
        payload_size: int = 512,
    ) -> TransportPacket:
        """Send a server -> client data segment on an established connection.

        Stage 10 application services (HTTP, FTP, SMTP) use this so their
        responses travel back over the same simulated TCP connection, the same
        scheduler queue, and the same routing/QoS path as the request.
        """
        connection = self.connections[flow_id]
        if connection.state != TCPState.ESTABLISHED:
            raise ValueError(
                "TCP connection must be ESTABLISHED before sending a response"
            )
        # Response segments keep their own sequence counter so they never punch
        # a hole in the client's contiguous sequence space (which would stall
        # cumulative acknowledgement of later requests).
        sequence = connection.response_packets_sent
        packet = TransportPacket(
            "TCP",
            connection.destination,
            connection.source,
            connection.destination_port,
            connection.source_port,
            max(1, int(payload_size)),
            connection.flow_id,
            sequence,
            connection.cumulative_ack,
            ["ACK"],
            connection.receiver_window,
            "RESPONSE",
            connection.traffic_class,
        )
        connection.packets.append(packet)
        connection.response_packets_sent += 1
        connection.bytes_sent += packet.payload_size
        sent = self._enqueue(packet)
        self._log(
            EventType.TCP_DATA_SENT,
            f"TCP response segment sent seq={sequence}",
            connection,
            sequence_number=sequence,
            payload_size=packet.payload_size,
            packet_id=packet.packet_id,
        )
        return sent

    def set_receiver_window(self, flow_id: str, window_size: int) -> TCPConnection:
        connection = self.connections[flow_id]
        connection.receiver_window = max(0, int(window_size))
        return connection

    def close_tcp_connection(self, flow_id: str) -> TransportPacket:
        connection = self.connections[flow_id]
        if connection.state not in {TCPState.ESTABLISHED, TCPState.CLOSE_WAIT}:
            raise ValueError("TCP connection cannot close from its current state")
        connection.termination_phase = "ACTIVE_FIN_SENT"
        connection.transition(TCPState.FIN_WAIT, self.simulator.time, "active FIN queued")
        packet = self._control(
            connection,
            ["FIN", "ACK"],
            "FIN",
            seq=connection.next_sequence,
        )
        self._log(
            EventType.TCP_FIN_SENT,
            "TCP active FIN sent",
            connection,
            sequence_number=packet.sequence_number,
            packet_id=packet.packet_id,
        )
        return packet

    def _update_route(
        self,
        flow: TCPConnection | UDPFlow,
        packet: TransportPacket,
    ) -> None:
        if packet.source != flow.source or not packet.route:
            return
        if packet.route != flow.route:
            old_route = list(flow.route)
            flow.route = list(packet.route)
            flow.route_history.append(
                {
                    "time": self.simulator.time,
                    "route": list(flow.route),
                    "old_route": old_route,
                    "reason": "ROUTING_UPDATE",
                }
            )
            if old_route:
                self._log(
                    EventType.ROUTE_RECALCULATED,
                    f"{packet.protocol} route recalculated {flow.source}→{flow.destination}",
                    flow_id=flow.flow_id,
                    old_route=old_route,
                    new_route=list(flow.route),
                )

    def _rtt(self, connection: TCPConnection, rtt: float) -> None:
        rtt = max(0.0, rtt)
        connection.rtt_samples.append(rtt)
        if connection.srtt is None:
            connection.srtt = rtt
            connection.rttvar = rtt / 2
        else:
            connection.rttvar = (
                3 * connection.rttvar + abs(connection.srtt - rtt)
            ) / 4
            connection.srtt = (7 * connection.srtt + rtt) / 8
        connection.rto = max(
            connection.timeout,
            connection.srtt + 4 * connection.rttvar,
        )

    def _process_ack(self, connection: TCPConnection, packet: TransportPacket) -> None:
        acknowledgement = int(packet.acknowledgement_number or 0)
        previous_ack = connection.cumulative_ack
        connection.ack_count += 1
        connection.highest_ack = max(connection.highest_ack, acknowledgement)
        duplicate = acknowledgement <= previous_ack
        if duplicate:
            connection.duplicate_ack_count += 1
        acknowledged = sorted(
            sequence
            for sequence in connection.outstanding
            if sequence < acknowledgement
        )
        if acknowledged:
            newest = max(acknowledged)
            newest_segment = connection.outstanding.get(newest)
            if newest_segment is not None:
                self._rtt(
                    connection,
                    self.simulator.time - newest_segment.original_send_time,
                )
            for sequence in acknowledged:
                connection.outstanding.pop(sequence, None)
            connection.cumulative_ack = max(
                connection.cumulative_ack,
                acknowledgement,
            )
            if connection.cwnd < connection.ssthresh:
                connection.cwnd += 1
                connection.congestion_phase = "SLOW_START"
                self._log(
                    EventType.TCP_SLOW_START,
                    "TCP slow start increased cwnd",
                    connection,
                    cwnd=connection.cwnd,
                )
            else:
                connection.cwnd += 1 / max(1.0, connection.cwnd)
                connection.congestion_phase = "CONGESTION_AVOIDANCE"
                self._log(
                    EventType.TCP_CONGESTION_AVOIDANCE,
                    "TCP congestion avoidance increased cwnd",
                    connection,
                    cwnd=connection.cwnd,
                )
            connection.max_cwnd = max(connection.max_cwnd, connection.cwnd)
            connection.cwnd_samples.append(connection.cwnd)
            self._log(
                EventType.TCP_CONGESTION_WINDOW_CHANGED,
                f"TCP cwnd={connection.cwnd:g}",
                connection,
                cwnd=connection.cwnd,
                phase=connection.congestion_phase,
            )
        self._log(
            EventType.TCP_ACK_RECEIVED,
            f"TCP ACK received ack={acknowledgement}",
            connection,
            acknowledgement_number=acknowledgement,
            duplicate=duplicate,
        )

    def _retransmit(
        self,
        connection: TCPConnection,
        segment: OutstandingSegment,
    ) -> TransportPacket:
        original = segment.packet
        segment.retransmissions += 1
        packet = TransportPacket(
            "TCP",
            original.source,
            original.destination,
            original.source_port,
            original.destination_port,
            original.payload_size,
            original.flow_id,
            original.sequence_number,
            original.acknowledgement_number,
            list(original.flags),
            connection.receiver_window,
            original.kind,
            connection.traffic_class,
            retransmission=segment.retransmissions,
        )
        connection.packets.append(packet)
        self._enqueue(packet)
        segment.packet = packet
        segment.last_send_time = self.simulator.time
        segment.timeout_started = False
        connection.retransmission_count += 1
        self._log(
            EventType.TCP_RETRANSMISSION,
            f"TCP retransmission seq={packet.sequence_number}",
            connection,
            sequence_number=packet.sequence_number,
            retransmission=packet.retransmission,
            packet_id=packet.packet_id,
        )
        return packet

    def process_transport_tick(
        self,
        now: Optional[float] = None,
    ) -> List[TransportPacket]:
        current = self.simulator.time if now is None else float(now)
        retransmissions: List[TransportPacket] = []
        for connection in self.connections.values():
            for segment in list(connection.outstanding.values()):
                if current - segment.last_send_time < connection.rto:
                    continue
                can_start_timeout = (
                    connection.last_timeout_time is None
                    or current - connection.last_timeout_time >= connection.rto
                )
                if not segment.timeout_started and can_start_timeout:
                    segment.timeout_started = True
                    connection.last_timeout_time = current
                    connection.timeout_count += 1
                    connection.ssthresh = max(2.0, connection.cwnd / 2)
                    connection.cwnd = 1.0
                    connection.congestion_phase = "SLOW_START"
                    connection.cwnd_samples.append(connection.cwnd)
                    self._log(
                        EventType.TCP_TIMEOUT,
                        f"TCP timeout seq={segment.sequence_number}",
                        connection,
                        sequence_number=segment.sequence_number,
                        rto=connection.rto,
                        packet_id=segment.packet.packet_id,
                    )
                    self._log(
                        EventType.TCP_CONGESTION_DETECTED,
                        "TCP congestion detected; reducing cwnd",
                        connection,
                        cwnd=connection.cwnd,
                        ssthresh=connection.ssthresh,
                    )
                    self._log(
                        EventType.TCP_CONGESTION_WINDOW_CHANGED,
                        f"TCP cwnd reduced to {connection.cwnd:g}",
                        connection,
                        cwnd=connection.cwnd,
                        ssthresh=connection.ssthresh,
                        phase=connection.congestion_phase,
                    )
                retransmissions.append(self._retransmit(connection, segment))
        return retransmissions

    def _record_outcome(
        self,
        connection: Optional[TCPConnection],
        udp_flow: Optional[UDPFlow],
        packet: TransportPacket,
        network_packet: Packet,
    ) -> None:
        delivered = network_packet.delivery_status == "DELIVERED"
        if udp_flow is not None:
            udp_flow.elapsed_time = max(
                udp_flow.elapsed_time,
                self.simulator.time - udp_flow.started_at,
            )
            if delivered:
                udp_flow.packets_delivered += 1
                udp_flow.bytes_delivered += packet.payload_size
            elif network_packet.delivery_status == "DROPPED":
                udp_flow.packets_lost += 1
        if connection is not None:
            connection.elapsed_time = max(
                connection.elapsed_time,
                self.simulator.time - connection.started_at,
            )
            if packet.kind in {"DATA", "RESPONSE"}:
                if delivered:
                    connection.data_packets_delivered += 1
                    connection.bytes_transferred += packet.payload_size
                elif network_packet.delivery_status == "DROPPED":
                    connection.data_packets_lost += 1

    def on_packet_processed(self, network_packet: Packet) -> None:
        transport_packet = getattr(network_packet, "transport", None)
        if transport_packet is None:
            return
        transport_packet.status = network_packet.delivery_status
        transport_packet.route = list(network_packet.route)
        transport_packet.latency = network_packet.latency
        transport_packet.delivered_time = network_packet.delivery_time
        transport_packet.queue_wait_time = network_packet.queue_wait_time
        if network_packet.delivery_status == "DROPPED":
            drop_event = next(
                (
                    event
                    for event in reversed(self.simulator.event_logger.events)
                    if event.details.get("packet_id") == network_packet.packet_id
                    and event.event_type == EventType.PACKET_DROPPED
                ),
                None,
            )
            transport_packet.drop_reason = (
                drop_event.details.get("reason") if drop_event else None
            ) or "DESTINATION_UNREACHABLE"
            if transport_packet.drop_reason == "PACKET_LOSS" and any(
                float(
                    self.simulator.topology.graph[left][right].get(
                        "congestion", 0
                    )
                )
                > 0
                for left, right in zip(
                    transport_packet.route,
                    transport_packet.route[1:],
                )
                if self.simulator.topology.graph.has_edge(left, right)
            ):
                transport_packet.drop_reason = "CONGESTION"

        connection = self.connections.get(transport_packet.flow_id)
        udp_flow = self.udp_flows.get(transport_packet.flow_id)
        if connection is not None:
            self._update_route(connection, transport_packet)
        elif udp_flow is not None:
            self._update_route(udp_flow, transport_packet)
        self._record_outcome(
            connection,
            udp_flow,
            transport_packet,
            network_packet,
        )
        self.simulator.transport_metrics.record(
            transport_packet.protocol,
            network_packet.packet_id,
            transport_packet.sent_time,
            network_packet.delivery_time,
            network_packet.latency,
            network_packet.size,
            network_packet.delivery_status,
            transport_packet.flow_id,
            transport_packet.route,
            transport_packet.traffic_class,
        )

        if transport_packet.protocol == "UDP":
            return
        if connection is None or network_packet.delivery_status == "DROPPED":
            if connection is not None and transport_packet.kind == "DATA":
                self._log(
                    EventType.TCP_PACKET_LOST,
                    f"TCP packet lost seq={transport_packet.sequence_number}",
                    connection,
                    sequence_number=transport_packet.sequence_number,
                    reason=transport_packet.drop_reason,
                    packet_id=transport_packet.packet_id,
                )
            return

        flags = transport_packet.flags
        if transport_packet.kind == "CONTROL" and flags == ["SYN"]:
            connection.transition(
                TCPState.SYN_RECEIVED,
                self.simulator.time,
                "SYN received",
            )
            syn_ack = self._control(
                connection,
                ["SYN", "ACK"],
                "CONTROL",
                reverse=True,
                seq=0,
                ack=1,
            )
            self._log(
                EventType.TCP_SYN_ACK_SENT,
                "TCP SYN-ACK sent",
                connection,
                acknowledgement_number=1,
                packet_id=syn_ack.packet_id,
            )
        elif transport_packet.kind == "CONTROL" and flags == ["SYN", "ACK"]:
            self._log(
                EventType.TCP_SYN_ACK_RECEIVED,
                "TCP SYN-ACK received",
                connection,
                packet_id=transport_packet.packet_id,
            )
            connection.transition(
                TCPState.SYN_RECEIVED,
                self.simulator.time,
                "SYN-ACK received",
            )
            self._control(
                connection,
                ["ACK"],
                "CONTROL",
                seq=1,
                ack=1,
            )
        elif (
            transport_packet.kind == "CONTROL"
            and flags == ["ACK"]
            and connection.state == TCPState.SYN_RECEIVED
        ):
            connection.setup_completed = self.simulator.time
            connection.transition(
                TCPState.ESTABLISHED,
                self.simulator.time,
                "handshake complete",
            )
            self._log(
                EventType.TCP_ACK_RECEIVED,
                "TCP handshake ACK received",
                connection,
                acknowledgement_number=transport_packet.acknowledgement_number,
                packet_id=transport_packet.packet_id,
            )
            self._log(
                EventType.TCP_HANDSHAKE_COMPLETE,
                "TCP handshake complete",
                connection,
                setup_time_ms=(
                    connection.setup_completed - connection.started_at
                )
                * 1000,
                packet_id=transport_packet.packet_id,
            )
        elif transport_packet.kind == "RESPONSE" and transport_packet.payload_size:
            self._log(
                EventType.TCP_DATA_SENT,
                f"TCP response segment delivered seq={transport_packet.sequence_number}",
                connection,
                sequence_number=transport_packet.sequence_number,
                payload_size=transport_packet.payload_size,
                packet_id=transport_packet.packet_id,
            )
        elif transport_packet.kind == "DATA" and transport_packet.payload_size:
            sequence = int(transport_packet.sequence_number or 0)
            if sequence not in connection.received_sequences:
                connection.received_sequences.append(sequence)
            # ACK numbers identify the next expected segment.  Only advance
            # across a contiguous received prefix so an out-of-order segment
            # cannot acknowledge a missing predecessor.
            contiguous = 0
            while contiguous + 1 in connection.received_sequences:
                contiguous += 1
            connection.receiver_ack = contiguous + 1
            ack = self._ack(connection, connection.receiver_ack)
            self._log(
                EventType.TCP_ACK_RECEIVED,
                f"TCP cumulative ACK queued ack={connection.receiver_ack}",
                connection,
                acknowledgement_number=connection.receiver_ack,
                packet_id=transport_packet.packet_id,
                ack_packet_id=ack.packet_id,
            )
        elif transport_packet.kind == "ACK":
            self._process_ack(connection, transport_packet)
        elif transport_packet.kind == "FIN":
            if transport_packet.source == connection.source:
                connection.termination_phase = "ACTIVE_FIN_ACK_QUEUED"
                connection.transition(
                    TCPState.CLOSE_WAIT,
                    self.simulator.time,
                    "active FIN received",
                )
                self._log(
                    EventType.TCP_FIN_RECEIVED,
                    "TCP active FIN received",
                    connection,
                    sequence_number=transport_packet.sequence_number,
                    packet_id=transport_packet.packet_id,
                )
                self._control(
                    connection,
                    ["ACK"],
                    "CONTROL",
                    reverse=True,
                    ack=connection.receiver_ack,
                )
            else:
                connection.termination_phase = "PASSIVE_FIN_ACK_QUEUED"
                connection.transition(
                    TCPState.TIME_WAIT,
                    self.simulator.time,
                    "passive FIN received",
                )
                self._log(
                    EventType.TCP_FIN_RECEIVED,
                    "TCP passive FIN received",
                    connection,
                    sequence_number=transport_packet.sequence_number,
                    packet_id=transport_packet.packet_id,
                )
                self._control(
                    connection,
                    ["ACK"],
                    "CONTROL",
                    reverse=True,
                    ack=connection.receiver_ack,
                )
        elif (
            transport_packet.kind == "CONTROL"
            and "ACK" in flags
            and connection.state == TCPState.CLOSE_WAIT
        ):
            connection.termination_phase = "PASSIVE_FIN_SENT"
            connection.transition(
                TCPState.LAST_ACK,
                self.simulator.time,
                "active FIN acknowledged; passive FIN queued",
            )
            passive_fin = self._control(
                connection,
                ["FIN", "ACK"],
                "FIN",
                reverse=True,
                seq=connection.next_sequence,
                ack=connection.receiver_ack,
            )
            self._log(
                EventType.TCP_FIN_SENT,
                "TCP passive FIN sent",
                connection,
                sequence_number=passive_fin.sequence_number,
                packet_id=passive_fin.packet_id,
            )
        elif (
            transport_packet.kind == "CONTROL"
            and "ACK" in flags
            and connection.state == TCPState.TIME_WAIT
        ):
            connection.termination_phase = "CLOSED"
            connection.transition(
                TCPState.CLOSED,
                self.simulator.time,
                "four-way FIN exchange complete",
            )
            self._log(
                EventType.TCP_CONNECTION_CLOSED,
                "TCP connection closed",
                connection,
                packet_id=transport_packet.packet_id,
            )

    def get_connection(self, flow_id: str) -> Optional[TCPConnection]:
        return self.connections.get(flow_id)

    def get_tcp_state(self, flow_id: str) -> str:
        connection = self.connections.get(flow_id)
        return connection.state.value if connection else "CLOSED"

    def get_transport_flows(self) -> List[Dict[str, Any]]:
        return [connection.to_dict() for connection in self.connections.values()] + [
            flow.to_dict() for flow in self.udp_flows.values()
        ]

    def get_tcp_packet_info(self, packet_id: int) -> Optional[Dict[str, Any]]:
        packet = self.packet_map.get(int(packet_id)) or self._transport_packet_map.get(
            int(packet_id)
        )
        return packet.to_dict() if packet else None

    def get_transport_statistics(self) -> Dict[str, Any]:
        tcp = self.simulator.transport_metrics.calculate(
            "TCP",
            self.simulator.time,
            sum(
                connection.retransmission_count
                for connection in self.connections.values()
            ),
            sum(connection.timeout_count for connection in self.connections.values()),
            sum(
                connection.setup_completed - connection.started_at
                for connection in self.connections.values()
                if connection.setup_completed is not None
            ),
        )
        udp = self.simulator.transport_metrics.calculate("UDP", self.simulator.time)
        connections = [
            connection.to_dict() for connection in self.connections.values()
        ]
        if connections:
            tcp.update(
                {
                    "current_cwnd": connections[-1]["cwnd"],
                    "maximum_cwnd": max(
                        connection["max_cwnd"] for connection in connections
                    ),
                    "average_cwnd": sum(
                        connection["average_cwnd"] for connection in connections
                    )
                    / len(connections),
                    "ssthresh": connections[-1]["ssthresh"],
                    "ack_count": sum(
                        connection["ack_count"] for connection in connections
                    ),
                    "duplicate_ack_count": sum(
                        connection["duplicate_ack_count"]
                        for connection in connections
                    ),
                    "packets_in_flight": sum(
                        connection["packets_in_flight"]
                        for connection in connections
                    ),
                    "bytes_in_flight": sum(
                        connection["bytes_in_flight"]
                        for connection in connections
                    ),
                }
            )
        return {
            "TCP": tcp,
            "UDP": udp,
            "connections": connections,
            "udp_flows": [
                flow.to_dict() for flow in self.udp_flows.values()
            ],
        }

    def reset(self) -> None:
        self.connections.clear()
        self.udp_flows.clear()
        self.packet_map.clear()
        self._transport_packet_map.clear()
        self._tcp = 1
        self._udp = 1
        self._packet = 1
        self.simulator.transport_metrics.reset()


def compare_transport(
    simulator: Any,
    source: str,
    destination: str,
    packet_count: int = 6,
    payload_size: int = 1000,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Run TCP and UDP on cloned topology/conditions with one random seed."""

    from simulator import NetworkSimulator

    selected_seed = int(simulator.seed if seed is None else seed)
    template = deepcopy(simulator.topology)
    results: List[Dict[str, Any]] = []
    for protocol in ("TCP", "UDP"):
        run = NetworkSimulator(
            topology=deepcopy(template),
            seed=selected_seed,
            scheduler=simulator.scheduler_name,
            scheduler_params=deepcopy(simulator.scheduler_params),
        )
        # The handshake is setup overhead, not part of the compared data loss
        # stream.  Reset the workload RNG so both protocols consume the same
        # topology loss/congestion realisation for their data packets.
        run.rng.seed(selected_seed + 1000)
        if protocol == "TCP":
            connection = run.create_tcp_connection(
                source,
                destination,
                traffic_class="HTTP",
            )
            run.run_until_empty()
            run.rng.seed(selected_seed + 1000)
            sent = 0
            attempts = 0
            while sent < packet_count and attempts < packet_count * 8:
                attempts += 1
                sent += len(
                    run.send_tcp_data(
                        connection.flow_id,
                        packet_count - sent,
                        payload_size,
                    )
                )
                run.run_until_empty()
                if connection.outstanding and attempts < packet_count * 8:
                    run.tick(max(connection.rto * 1.1, 0.001))
                    run.run_until_empty()
            row = connection.to_dict()
            row.update(
                {
                    "protocol": "TCP",
                    "flow_id": connection.flow_id,
                    "state": connection.state.value,
                    "connection_setup_time": row["setup_time_ms"] / 1000
                    if row["setup_time_ms"] is not None
                    else None,
                }
            )
        else:
            flow = run.create_udp_flow(
                source,
                destination,
                payload_size=payload_size,
                traffic_class="VoIP",
            )
            run.send_udp_data(flow.flow_id, packet_count, payload_size)
            run.run_until_empty()
            row = flow.to_dict()
            row.update(
                {
                    "protocol": "UDP",
                    "flow_id": flow.flow_id,
                    "state": "CONNECTIONLESS",
                    "connection_setup_time": 0.0,
                }
            )
        results.append(row)
    return {
        "type": "transport_comparison",
        "source": source,
        "destination": destination,
        "seed": selected_seed,
        "packet_count": packet_count,
        "payload_size": payload_size,
        "results": results,
    }
