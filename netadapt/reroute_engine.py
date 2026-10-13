"""
ReRoute (Vanguard) - NC-04 AI-Assisted Self-Healing Network Simulator Engine.

Provides:
- 6-Router Redundant Topology Preset
- Rolling Window Telemetry Collector (ground truth segregated)
- Explainable Threshold-Based Anomaly Detector with Incident Lifecycle
- Qualcomm Cloud AI 100 / Cirrascale Diagnostic Client (non-blocking, timeout, stale-epoch drop, rule-based fallback)
- Validated Healing Manager (deterministic rerouting, capacity validation, measured verification window, flap prevention)
- A/B Deterministic Comparison Runner (Auto-Heal ON vs OFF)
"""

from __future__ import annotations

import copy
import math
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Set

try:
    from telegram_notifier import TelegramNotifier, IncidentAlertPayload
except ModuleNotFoundError:
    from netadapt.telegram_notifier import TelegramNotifier, IncidentAlertPayload


# -----------------------------------------------------------------------------
# 1. Fixed Six-Router Redundant Topology Preset
# -----------------------------------------------------------------------------

def get_six_router_preset() -> Dict[str, Any]:
    """
    Fixed 6-router topology with redundant paths between Client and Server.
    
    Layout:
      Client (80, 320) -> R1 [Ingress] (240, 320)
      Upper Tier: R2 (460, 180) -> R4 (680, 180)
      Lower Tier: R3 (460, 460) -> R5 (680, 460)
      R6 [Egress] (880, 320) -> Server (1040, 320)
      Cross Links: R2-R3, R4-R5, R2-R5, R3-R4
    
    Primary path: Client -> R1 -> R2 -> R4 -> R6 -> Server (low latency ~19ms)
    Backup path:  Client -> R1 -> R3 -> R5 -> R6 -> Server (higher latency ~40ms)
    """
    def _dev(kind: str, name: str, x: float, y: float) -> Dict[str, Any]:
        return {"id": name, "type": kind, "x": x, "y": y, "interfaces": []}

    def _lk(src: str, dst: str, lat: float = 5.0, bw: float = 100.0) -> Dict[str, Any]:
        return {
            "id": f"{src}--{dst}",
            "source": src,
            "target": dst,
            "status": "UP",
            "latency": float(lat),
            "bandwidth": float(bw),
            "packet_loss": 0.0,
            "congestion": 0.0,
            "duplex": "Full",
            "interface_a": "eth0",
            "interface_b": "eth0",
        }

    return {
        "version": 1,
        "name": "ReRoute 6-Router Redundant Mesh",
        "settings": {
            "seed": 42,
            "scheduler": "priority",
            "routing_algorithm": "dijkstra",
        },
        "devices": [
            _dev("host", "Client", 80, 320),
            _dev("router", "R1", 240, 320),
            _dev("router", "R2", 460, 180),
            _dev("router", "R3", 460, 460),
            _dev("router", "R4", 680, 180),
            _dev("router", "R5", 680, 460),
            _dev("router", "R6", 880, 320),
            _dev("server", "Server", 1040, 320),
        ],
        "links": [
            _lk("Client", "R1", lat=2.0, bw=100.0),
            _lk("R1", "R2", lat=5.0, bw=100.0),
            _lk("R1", "R3", lat=12.0, bw=100.0),
            _lk("R2", "R4", lat=5.0, bw=100.0),
            _lk("R3", "R5", lat=12.0, bw=100.0),
            _lk("R4", "R6", lat=5.0, bw=100.0),
            _lk("R5", "R6", lat=12.0, bw=100.0),
            _lk("R6", "Server", lat=2.0, bw=100.0),
            # Inter-tier cross links
            _lk("R2", "R3", lat=8.0, bw=100.0),
            _lk("R4", "R5", lat=8.0, bw=100.0),
            _lk("R2", "R5", lat=15.0, bw=100.0),
            _lk("R3", "R4", lat=15.0, bw=100.0),
        ],
    }


# -----------------------------------------------------------------------------
# 2. Incident Lifecycle State Machine
# -----------------------------------------------------------------------------

class IncidentState(str, Enum):
    HEALTHY = "Healthy"
    WARNING = "Warning"
    DETECTED = "Detected"
    DIAGNOSING = "Diagnosing"
    ACTION_SELECTED = "Action selected"
    REROUTING = "Rerouting"
    VERIFYING = "Verifying"
    RECOVERED = "Recovered"
    UNRESOLVED = "Unresolved"


# -----------------------------------------------------------------------------
# 3. Telemetry Collector (Ground-Truth Segregated)
# -----------------------------------------------------------------------------

@dataclass
class PacketObservation:
    packet_id: int
    timestamp: float
    source: str
    destination: str
    status: str  # DELIVERED or DROPPED
    latency: Optional[float]
    route: List[str]


@dataclass
class LinkTelemetry:
    link_key: str  # e.g. "R2-R4"
    observed_latency: Optional[float] = None
    observed_loss_rate: Optional[float] = None
    observed_utilization: Optional[float] = None
    heartbeat_missed: int = 0  # consecutive missed heartbeats


class TelemetryCollector:
    """
    Maintains rolling observations of telemetry.
    Strictly segregated from ground-truth fault injection state.
    """
    def __init__(self, window_size: int = 15) -> None:
        self.window_size = window_size
        self.packet_history: deque[PacketObservation] = deque(maxlen=window_size)
        self.link_telemetry: Dict[str, LinkTelemetry] = {}
        self.baseline_latency: float = 19.0  # expected baseline for Client->R1->R2->R4->R6->Server
        self._observed_packet_ids: set[int] = set()
        self.total_packets_observed: int = 0
        self.total_delivered: int = 0
        self.total_dropped: int = 0

    def reset(self) -> None:
        self.baseline_latency = 19.0
        self._observed_packet_ids.clear()
        self.packet_history.clear()
        self.link_telemetry.clear()
        self.total_packets_observed = 0
        self.total_delivered = 0
        self.total_dropped = 0

    def record_packet(self, pkt_id: int, timestamp: float, src: str, dst: str,
                      status: str, latency: Optional[float], route: List[str]) -> None:
        if pkt_id in self._observed_packet_ids:
            return
        self._observed_packet_ids.add(pkt_id)
        obs = PacketObservation(pkt_id, timestamp, src, dst, status, latency, list(route))
        self.packet_history.append(obs)
        self.total_packets_observed += 1
        if status == "DELIVERED":
            self.total_delivered += 1
        elif status == "DROPPED":
            self.total_dropped += 1

    def update_link_telemetry(self, link_key: str, latency: Optional[float],
                              loss_rate: Optional[float], util: Optional[float], missed_hb: int) -> None:
        self.link_telemetry[link_key] = LinkTelemetry(
            link_key=link_key,
            observed_latency=latency,
            observed_loss_rate=loss_rate,
            observed_utilization=util,
            heartbeat_missed=missed_hb,
        )

    def get_recent_delivery_rate(self) -> float:
        if not self.packet_history:
            return 100.0
        delivered = sum(1 for p in self.packet_history if p.status == "DELIVERED")
        return round((delivered / len(self.packet_history)) * 100.0, 1)

    def get_recent_avg_latency(self) -> float:
        delivered_latencies = [p.latency for p in self.packet_history if p.status == "DELIVERED" and p.latency is not None]
        if not delivered_latencies:
            return 0.0
        return round(sum(delivered_latencies) / len(delivered_latencies), 2)

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "window_size": len(self.packet_history),
            "recent_delivery_rate_pct": self.get_recent_delivery_rate(),
            "recent_avg_latency_ms": self.get_recent_avg_latency() if any(p.status == "DELIVERED" and p.latency is not None for p in self.packet_history) else None,
            "recent_delivered_packets": sum(p.status == "DELIVERED" for p in self.packet_history),
            "total_observed": self.total_packets_observed,
            "total_delivered": self.total_delivered,
            "total_dropped": self.total_dropped,
            "link_stats": {
                k: {
                    "latency_ms": v.observed_latency,
                    "loss_rate": v.observed_loss_rate,
                    "utilization": v.observed_utilization,
                    "consecutive_missed_hb": v.heartbeat_missed,
                }
                for k, v in self.link_telemetry.items()
            },
        }


# -----------------------------------------------------------------------------
# 4. Explainable Detection Engine
# -----------------------------------------------------------------------------

@dataclass
class DetectionRuleConfig:
    warning_latency_mult: float = 1.4      # 1.4x baseline latency
    warning_loss_threshold: float = 0.05    # 5% loss in rolling window
    detected_loss_threshold: float = 0.20   # 20% loss
    detected_latency_mult: float = 2.2     # 2.2x baseline latency
    detected_missed_hb: int = 2            # 2 consecutive missed heartbeats
    consecutive_samples_required: int = 2  # consecutive degraded samples to trigger Warning/Detected
    cooldown_steps: int = 4                # cooldown after state change to prevent flapping
    predictive_latency_threshold: float = 23.5   # Pre-breach latency threshold (< 30ms SLO target)
    predictive_congestion_threshold: float = 0.20 # Congestion building on link along route


class ExplainableDetector:
    """
    Evaluates only observable telemetry using explicit, documented rules.
    Does not have access to ground-truth fault configuration.
    """
    def __init__(self, config: Optional[DetectionRuleConfig] = None) -> None:
        self.config = config or DetectionRuleConfig()
        self.consecutive_warning_samples: int = 0
        self.consecutive_detected_samples: int = 0
        self.cooldown_remaining: int = 0
        self.last_detection_evidence: List[str] = []
        self.suspected_component: Optional[str] = None

    def reset(self) -> None:
        self.consecutive_warning_samples = 0
        self.consecutive_detected_samples = 0
        self.cooldown_remaining = 0
        self.last_detection_evidence.clear()
        self.suspected_component = None

    def evaluate(self, telemetry: TelemetryCollector, current_route: List[str]) -> Tuple[str, List[str], Optional[str]]:
        """
        Returns (evaluation_level, evidence_list, suspected_component).
        evaluation_level: "HEALTHY", "WARNING", or "DETECTED"
        """
        evidence: List[str] = []
        suspected: Optional[str] = None

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

        recent_lat = telemetry.get_recent_avg_latency()
        recent_pdr = telemetry.get_recent_delivery_rate()
        recent_loss_pct = 100.0 - recent_pdr

        # 1. Check Heartbeats on links along the active route
        for i in range(len(current_route) - 1):
            u, v = current_route[i], current_route[i + 1]
            key = f"{u}-{v}"
            alt_key = f"{v}-{u}"
            lt = telemetry.link_telemetry.get(key) or telemetry.link_telemetry.get(alt_key)
            if lt and lt.heartbeat_missed >= self.config.detected_missed_hb:
                evidence.append(f"Heartbeat failure: link {key} missed {lt.heartbeat_missed} consecutive probes")
                suspected = key
                return "DETECTED", evidence, suspected

        # 2. Check for sudden packet drop
        if len(telemetry.packet_history) >= 2:
            last_packets = list(telemetry.packet_history)[-3:]
            drops = [p for p in last_packets if p.status == "DROPPED"]
            if len(drops) >= 2:
                evidence.append(f"Consecutive packet drop: {len(drops)} of last {len(last_packets)} packets dropped")
                # Suspect link on active path
                if len(current_route) >= 2:
                    suspected = f"{current_route[1]}-{current_route[2]}" if len(current_route) > 2 else f"{current_route[0]}-{current_route[1]}"
                return "DETECTED", evidence, suspected

        # 3. Check Degraded Latency / Loss
        is_detected_level = False
        is_warning_level = False

        if recent_lat >= (telemetry.baseline_latency * self.config.detected_latency_mult):
            evidence.append(f"Severe latency spike: {recent_lat:.1f}ms (threshold >= {telemetry.baseline_latency * self.config.detected_latency_mult:.1f}ms)")
            is_detected_level = True
        elif recent_lat >= (telemetry.baseline_latency * self.config.warning_latency_mult):
            evidence.append(f"Elevated latency: {recent_lat:.1f}ms (threshold >= {telemetry.baseline_latency * self.config.warning_latency_mult:.1f}ms)")
            is_warning_level = True

        if recent_loss_pct >= (self.config.detected_loss_threshold * 100.0):
            evidence.append(f"High packet loss: {recent_loss_pct:.1f}% (threshold >= {self.config.detected_loss_threshold * 100.0:.1f}%)")
            is_detected_level = True
        elif recent_loss_pct >= (self.config.warning_loss_threshold * 100.0):
            evidence.append(f"Minor packet loss: {recent_loss_pct:.1f}% (threshold >= {self.config.warning_loss_threshold * 100.0:.1f}%)")
            is_warning_level = True

        # Check per-link metrics along route
        for i in range(len(current_route) - 1):
            u, v = current_route[i], current_route[i + 1]
            key = f"{u}-{v}"
            lt = telemetry.link_telemetry.get(key) or telemetry.link_telemetry.get(f"{v}-{u}")
            if lt:
                if lt.observed_loss_rate is not None and lt.observed_loss_rate >= self.config.detected_loss_threshold:
                    evidence.append(f"Link {key} loss rate {lt.observed_loss_rate * 100:.1f}%")
                    suspected = key
                    is_detected_level = True
                elif lt.observed_utilization is not None and lt.observed_utilization >= self.config.predictive_congestion_threshold and lt.observed_latency is not None and lt.observed_latency >= 9.0:
                    evidence.append(f"Early rising congestion on {key}: latency {lt.observed_latency:.1f}ms / congestion {lt.observed_utilization * 100:.0f}%")
                    if not suspected:
                        suspected = key
                    is_detected_level = True
                elif lt.observed_latency is not None and lt.observed_latency > 30.0:
                    evidence.append(f"Link {key} latency {lt.observed_latency:.1f}ms")
                    if not suspected:
                        suspected = key
                    is_warning_level = True

        if is_detected_level:
            self.consecutive_detected_samples += 1
            if self.consecutive_detected_samples >= self.config.consecutive_samples_required:
                return "DETECTED", evidence, suspected
            return "WARNING", evidence, suspected
        elif is_warning_level:
            self.consecutive_warning_samples += 1
            self.consecutive_detected_samples = 0
            if self.consecutive_warning_samples >= self.config.consecutive_samples_required:
                return "WARNING", evidence, suspected
            return "HEALTHY", [], None
        else:
            self.consecutive_warning_samples = 0
            self.consecutive_detected_samples = 0
            return "HEALTHY", [], None


# -----------------------------------------------------------------------------
# 5. Qualcomm Cloud AI 100 / Cirrascale Diagnostic Client
# -----------------------------------------------------------------------------

@dataclass
class AIDiagnosisResult:
    likely_cause: str
    affected_component: str
    evidence: List[str]
    recommended_action: str
    explanation: str
    model_name: str
    wall_clock_ms: float
    is_fallback: bool = False
    fallback_reason: Optional[str] = None


class QualcommAIClient:
    """
    Non-blocking Qualcomm Cloud AI 100 inference client.
    - Connects to Cirrascale OpenAI-compatible endpoint.
    - Validates returned schema, rejects hallucinated components/actions.
    - Drops stale responses if incident was replaced or reset.
    - Employs clearly labeled deterministic rule fallback if timed out or offline.
    """
    ALLOWED_ACTIONS = {"REROUTE_TRAFFIC", "BYPASS_COMPONENT", "TRAFFIC_SHAPING", "ISOLATE_NODE"}

    def __init__(self, endpoint: Optional[str] = None, model: Optional[str] = None,
                 api_key: Optional[str] = None, timeout_seconds: float = 8.0) -> None:
        # Load root .env if environment variables are not yet present
        if "QUALCOMM_AI_KEY" not in os.environ or "QUALCOMM_AI_ENDPOINT" not in os.environ:
            for env_candidate in [Path(__file__).resolve().parent.parent / ".env", Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"]:
                if env_candidate.exists():
                    try:
                        with open(env_candidate, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, v = line.split("=", 1)
                                    k, v = k.strip(), v.strip()
                                    if k not in os.environ:
                                        os.environ[k] = v
                    except Exception:
                        pass

        self.endpoint = (endpoint if endpoint is not None else (os.environ.get("QUALCOMM_AI_ENDPOINT") or "https://aisuite.cirrascale.com/apis/v2")).rstrip("/")
        self.model = model if model is not None else (os.environ.get("QUALCOMM_AI_MODEL") or "Llama-3.1-8B")
        self.api_key = api_key if api_key is not None else (os.environ.get("QUALCOMM_AI_KEY") or "")
        self.timeout = timeout_seconds
        self.current_epoch: int = 0
        self._lock = threading.Lock()
        self.active_diagnosis: Optional[AIDiagnosisResult] = None
        self.is_busy: bool = False

    def new_epoch(self) -> int:
        with self._lock:
            self.current_epoch += 1
            self.active_diagnosis = None
            self.is_busy = False
            return self.current_epoch

    def diagnose_async(
        self,
        epoch: int,
        telemetry_summary: Dict[str, Any],
        topology_components: List[str],
        current_route: List[str],
        detector_evidence: List[str],
        suspected_component: Optional[str],
        on_complete: Callable[[AIDiagnosisResult], None],
    ) -> None:
        """Launches diagnosis in a background thread so the simulation never freezes."""
        thread = threading.Thread(
            target=self._run_diagnosis,
            args=(epoch, telemetry_summary, topology_components, current_route,
                  detector_evidence, suspected_component, on_complete),
            daemon=True,
        )
        with self._lock:
            self.is_busy = True
        thread.start()
        return thread


    def _run_diagnosis(
        self,
        epoch: int,
        telemetry_summary: Dict[str, Any],
        topology_components: List[str],
        current_route: List[str],
        detector_evidence: List[str],
        suspected_component: Optional[str],
        on_complete: Callable[[AIDiagnosisResult], None],
    ) -> None:
        start_time = time.perf_counter()

        # If no key or mock key, use rule-based fallback immediately
        if not self.api_key or "your_" in self.api_key:
            res = self._fallback_rule_diagnosis(
                suspected_component, detector_evidence, current_route,
                topology_components, "QUALCOMM_AI_KEY not configured", 0.1
            )
            self._dispatch_if_current(epoch, res, on_complete)
            return

        # Prepare strict structured prompt avoiding raw JSON tool-calling triggers on Cirrascale
        actions_str = ", ".join(sorted(list(self.ALLOWED_ACTIONS)))
        comps_str = ", ".join(topology_components)
        user_prompt = (
            "Network Incident Telemetry:\n"
            f"Active Route: {' -> '.join(current_route)}\n"
            f"Detector Evidence: {', '.join(detector_evidence)}\n"
            f"Suspected Component: {suspected_component or 'None'}\n"
            f"Telemetry: PDR={telemetry_summary.get('recent_delivery_rate_pct')}%, "
            f"AvgRTT={telemetry_summary.get('recent_avg_latency_ms')}ms\n"
            f"Allowed Actions: {actions_str}\n"
            f"Valid Topology Components: {comps_str}\n\n"
            "Diagnose the network incident. Respond strictly with the following format:\n"
            "Likely Cause: <LINK_FAILURE or HIGH_CONGESTION>\n"
            f"Affected Component: <one of {comps_str}>\n"
            "Evidence: <evidence list or summary>\n"
            f"Recommended Action: <one of {actions_str}>\n"
            "Explanation: <concise explanation under 30 words>\n"
        )

        chat_url = f"{self.endpoint}/chat/completions"
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 200,
            "temperature": 0.0,
        }).encode("utf-8")

        req = urllib.request.Request(
            chat_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                body = resp.read().decode("utf-8")
                parsed_json = json.loads(body)
                choices = parsed_json.get("choices", [])
                if not choices:
                    raise ValueError("No choices in LLM response")
                content = choices[0]["message"]["content"].strip()
                result = self._parse_and_validate(
                    content, topology_components, current_route, elapsed_ms
                )
                self._dispatch_if_current(epoch, result, on_complete)
                return
        except urllib.error.HTTPError as e:
            err = f"Qualcomm API HTTP {e.code}"
        except urllib.error.URLError as e:
            err = f"Qualcomm API Timeout / Unreachable ({e.reason})"
        except Exception as e:
            err = f"Qualcomm API Exception: {e}"

        # On any error/timeout, trigger verified rule fallback
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        fallback = self._fallback_rule_diagnosis(
            suspected_component, detector_evidence, current_route,
            topology_components, err, elapsed_ms
        )
        self._dispatch_if_current(epoch, fallback, on_complete)

    def _parse_and_validate(
        self,
        raw_text: str,
        valid_components: List[str],
        current_route: List[str],
        elapsed_ms: float,
    ) -> AIDiagnosisResult:
        cause = "UNKNOWN"
        comp = ""
        evidence: List[str] = []
        action = "REROUTE_TRAFFIC"
        explanation = ""

        # First try parsing as JSON (supporting either JSON response or Markdown json fence)
        cleaned = raw_text.strip()
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if match:
                cleaned = match.group(1).strip()
            else:
                cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned).strip()

        is_json = False
        if cleaned.startswith("{") and "}" in cleaned:
            try:
                data = json.loads(cleaned)
                cause = str(data.get("likely_cause", "UNKNOWN")).strip()
                comp = str(data.get("affected_component", "")).strip()
                ev = data.get("evidence", [])
                evidence = ev if isinstance(ev, list) else [str(ev)]
                action = str(data.get("recommended_action", "REROUTE_TRAFFIC")).strip()
                explanation = str(data.get("explanation", "")).strip()
                is_json = True
            except Exception:
                is_json = False

        def _clean(val: Any) -> str:
            return re.sub(r"[\*`\"']", "", str(val or "")).strip()

        if not is_json:
            # Parse Key-Value format: "Likely Cause: ...\nAffected Component: ...\n"
            for line in raw_text.splitlines():
                line = line.strip().lstrip("-* ").strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = _clean(k).lower().replace("_", " ")
                v = _clean(v)
                if "likely cause" in k or "cause" in k:
                    cause = v
                elif "affected component" in k or "affected" in k or "component" in k:
                    comp = v
                elif "evidence" in k:
                    evidence.append(v)
                elif "recommended action" in k or "action" in k:
                    action = v
                elif "explanation" in k or "summary" in k:
                    explanation = v

        cause = _clean(cause)
        comp = _clean(comp)
        action = _clean(action)
        explanation = _clean(explanation)

        # Normalize cause
        cause_upper = cause.upper()
        if any(w in cause_upper for w in ["CONGEST", "DELAY", "QUEUE", "LATENCY", "SLOW"]):
            cause = "HIGH_CONGESTION"
        elif any(w in cause_upper for w in ["FAIL", "DOWN", "SEVER", "DROP", "BROKEN", "LOSS"]):
            cause = "LINK_FAILURE"

        # Normalize component
        # Strip prefixes like 'link ', 'node ', 'router ', 'switch ', etc.
        comp_clean = re.sub(r"^(?:link|node|router|switch|interface|component)\s+", "", comp, flags=re.IGNORECASE).strip()
        comp_clean = re.sub(r"\s*(?:-|–|—|->|to)\s*", "-", comp_clean)

        norm_comps = {c.lower(): c for c in valid_components}
        matched_comp = norm_comps.get(comp_clean.lower())
        if not matched_comp and "-" in comp_clean:
            # Check reverse link: R4-R2 -> R2-R4
            parts = comp_clean.split("-", 1)
            if len(parts) == 2:
                matched_comp = norm_comps.get(f"{parts[1]}-{parts[0]}".lower())

        if not matched_comp:
            # Check if any valid component matches inside comp_clean
            for vc in valid_components:
                if vc.lower() == comp_clean.lower():
                    matched_comp = vc
                    break

        if not matched_comp:
            # Reject invalid component - do NOT silently replace with arbitrary hop and label as valid AI diagnosis
            return self._fallback_rule_diagnosis(
                suspected=None,
                evidence=evidence or [f"AI proposed invalid component '{comp}' not in topology"],
                current_route=current_route,
                valid_components=valid_components,
                reason=f"Rejected hallucinated AI component '{comp}'",
                elapsed_ms=elapsed_ms,
            )

        # Normalize action
        act_lower = action.lower()
        if any(w in act_lower for w in ["reroute", "bypass", "divert", "alternate", "route"]):
            action = "REROUTE_TRAFFIC"
        elif any(w in act_lower for w in ["isolate", "shutdown", "block"]):
            action = "ISOLATE_NODE"
        elif any(w in act_lower for w in ["qos", "bandwidth", "traffic", "shaping", "shape"]):
            action = "TRAFFIC_SHAPING"
        elif any(w in act_lower for w in ["no action", "none", "monitor", "healthy", "pass"]):
            action = "NO_ACTION"
        elif action not in self.ALLOWED_ACTIONS:
            action = "REROUTE_TRAFFIC"

        return AIDiagnosisResult(
            likely_cause=cause,
            affected_component=matched_comp,
            evidence=[str(e) for e in evidence],
            recommended_action=action,
            explanation=explanation or f"Component {matched_comp} degraded/failed according to telemetry.",
            model_name="Llama-3.1-8B (Qualcomm Cloud AI 100)",
            wall_clock_ms=round(elapsed_ms, 1),
            is_fallback=False,
        )

    def _fallback_rule_diagnosis(
        self,
        suspected: Optional[str],
        evidence: List[str],
        current_route: List[str],
        valid_components: List[str],
        reason: str,
        elapsed_ms: float,
    ) -> AIDiagnosisResult:
        comp = suspected
        if not comp:
            if len(current_route) >= 3:
                comp = f"{current_route[1]}-{current_route[2]}"
            else:
                comp = "R2-R4"

        return AIDiagnosisResult(
            likely_cause="LINK_FAILURE" if any("Heartbeat" in e or "drop" in e for e in evidence) else "HIGH_CONGESTION",
            affected_component=comp,
            evidence=list(evidence) or [f"Observed telemetry anomalies on active route hop {comp}"],
            recommended_action="REROUTE_TRAFFIC",
            explanation=f"Deterministic rule engine diagnosis ({reason}). Rerouting away from degraded hop {comp}.",
            model_name="Deterministic Rule Fallback",
            wall_clock_ms=round(elapsed_ms, 1),
            is_fallback=True,
            fallback_reason=reason,
        )

    def _dispatch_if_current(
        self,
        epoch: int,
        result: AIDiagnosisResult,
        on_complete: Callable[[AIDiagnosisResult], None],
    ) -> None:
        with self._lock:
            if epoch != self.current_epoch:
                # Discard stale response
                return
            self.active_diagnosis = result
            self.is_busy = False
        on_complete(result)


# -----------------------------------------------------------------------------
# 6. Validated Healing Manager
# -----------------------------------------------------------------------------

class HealingManager:
    """
    Executes validated, deterministic rerouting and monitors recovery verification window.
    - Capacity constraints: bandwidth must be >= minimum requirement (e.g. 10 Mbps).
    - Failed components stay excluded.
    - Flap prevention: cooldown and route switch hysteresis.
    - Measured verification window: requires N consecutive delivered packets on new route.
    """
    def __init__(self, verification_window: int = 5, hysteresis_cooldown: int = 3) -> None:
        self.verification_window = verification_window
        self.hysteresis_cooldown = hysteresis_cooldown
        self.consecutive_delivered_on_new_route: int = 0
        self.last_reroute_step: int = 0
        self.auto_heal_enabled: bool = True
        self.blacklisted_links: Set[Tuple[str, str]] = set()
        self.blacklisted_nodes: Set[str] = set()
        self.expected_route: Optional[List[str]] = None
        self.action_sim_time: float = 0.0
        self.verified_packet_ids: Set[Any] = set()
        self.service_target_latency_ms: float = 30.0
        self.recovery_max_latency_ms: float = 50.0
        self.delivery_restored: bool = False
        self.service_target_met: bool = False
        self.last_verified_latency_ms: Optional[float] = None
        self.verification_latencies = deque(maxlen=self.verification_window)

    def reset(self) -> None:
        self.consecutive_delivered_on_new_route = 0
        self.last_reroute_step = 0
        self.blacklisted_links.clear()
        self.blacklisted_nodes.clear()
        self.expected_route = None
        self.action_sim_time = 0.0
        self.verified_packet_ids.clear()
        self.delivery_restored = False
        self.service_target_met = False
        self.last_verified_latency_ms = None
        self.verification_latencies.clear()

    def calculate_alternative_path(
        self,
        topology: Any,
        source: str,
        destination: str,
        exclude_component: Optional[str] = None,
        min_bandwidth: float = 10.0,
    ) -> Optional[Tuple[List[str], float]]:
        """
        Dijkstra path search excluding blacklisted/failed components and meeting bandwidth constraints.
        Returns (path, cost) or None if no alternative path exists.
        """
        import heapq

        if source not in topology.graph or destination not in topology.graph or not topology.active_node(source) or not topology.active_node(destination):
            return None
        excluded_edges: Set[Tuple[str, str]] = set(self.blacklisted_links)
        excluded_nodes: Set[str] = set(self.blacklisted_nodes)

        if exclude_component:
            if "-" in exclude_component:
                u, v = exclude_component.split("-", 1)
                excluded_edges.add((u, v))
                excluded_edges.add((v, u))
            else:
                excluded_nodes.add(exclude_component)

        # Build adjacency graph
        distances: Dict[str, float] = {source: 0.0}
        pq = [(0.0, source, [source])]
        visited: Set[str] = set()

        while pq:
            cost, u, path = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)

            if u == destination:
                return path, cost

            # Explore neighbors in topology graph
            for v in topology.graph.neighbors(u):
                if v in excluded_nodes or not topology.active_node(v):
                    continue
                if (u, v) in excluded_edges or (v, u) in excluded_edges:
                    continue

                edge = topology.graph[u][v]
                if edge.get("status") == "DOWN":
                    continue
                if edge.get("bandwidth", 100.0) < min_bandwidth:
                    continue

                edge_latency = edge.get("latency", 5.0)
                edge_cost = edge_latency + (edge.get("congestion", 0.0) * 10.0)
                new_cost = cost + edge_cost

                if new_cost < distances.get(v, float("inf")):
                    distances[v] = new_cost
                    heapq.heappush(pq, (new_cost, v, path + [v]))

        return None

    def record_verification_packet(
        self,
        status: str,
        latency_ms: Optional[float] = None,
        packet_id: Optional[Any] = None,
        timestamp: Optional[float] = None,
        route: Optional[List[str]] = None,
    ) -> bool:
        """
        Returns True if recovery verification threshold is met.
        Requires distinct delivered packets on the bypass route meeting configured service limits (latency <= 50.0 ms).
        - Rejects missing latency (latency_ms is None).
        - Rejects latency > 50.0 ms.
        - Rejects duplicate packet IDs.
        - Rejects packets sent before the action boundary timestamp.
        - Rejects packets not matching the expected bypass route.
        """
        # Reject missing required verification fields (packet_id, timestamp, route, latency_ms)
        if (
            status != "DELIVERED"
            or packet_id is None
            or timestamp is None
            or route is None
            or latency_ms is None
            or not math.isfinite(latency_ms)
            or latency_ms < 0
            or latency_ms > self.recovery_max_latency_ms
        ):
            self.consecutive_delivered_on_new_route = 0
            self.verification_latencies.clear()
            self.delivery_restored = False
            self.service_target_met = False
            return False

        # Route validation if expected_route is established
        if self.expected_route is not None:
            if list(route) != list(self.expected_route):
                self.consecutive_delivered_on_new_route = 0
                self.verification_latencies.clear()
                self.delivery_restored = False
                self.service_target_met = False
                return False

        # Timestamp validation against action boundary
        if timestamp < self.action_sim_time:
            return False

        # Duplicate packet ID validation: repeated packet IDs must NOT count toward recovery
        if packet_id in self.verified_packet_ids:
            return False
        self.verified_packet_ids.add(packet_id)

        self.verification_latencies.append(float(latency_ms))
        self.consecutive_delivered_on_new_route += 1
        if self.consecutive_delivered_on_new_route >= self.verification_window:
            self.delivery_restored = True
            self.last_verified_latency_ms = float(latency_ms)
            self.service_target_met = (max(self.verification_latencies) <= self.service_target_latency_ms)
            return True
        return False


# -----------------------------------------------------------------------------
# 7. Scenario Definitions
# -----------------------------------------------------------------------------

class ReRouteScenarioType(str, Enum):
    GRADUAL_CONGESTION = "gradual_congestion"
    MILD_CONGESTION = "mild_congestion"
    SUDDEN_LINK_FAILURE = "sudden_link_failure"
    NO_ALTERNATE_PATH = "no_alternate_path"


# -----------------------------------------------------------------------------
# 8. ReRoute Controller (Coordinates Complete Lifecycle)
# -----------------------------------------------------------------------------

class ReRouteController:
    """
    Main controller integrating Simulation, Telemetry, Explainable Detection,
    Qualcomm AI Diagnostics, Validated Healing, and Repeatable A/B Comparison.
    """
    def __init__(self, lab_session: Any) -> None:
        self.session = lab_session
        self.telemetry = TelemetryCollector()
        self.detector = ExplainableDetector()
        self.ai_client = QualcommAIClient()
        self.healing = HealingManager()

        self.incident_state = IncidentState.HEALTHY
        self.active_scenario: Optional[str] = None
        self.scenario_step: int = 0
        self.sim_step_count: int = 0
        self.incident_history: List[Dict[str, Any]] = []

        self.current_route: List[str] = ["Client", "R1", "R2", "R4", "R6", "Server"]
        self.previous_route: Optional[List[str]] = None
        self.active_diagnosis: Optional[AIDiagnosisResult] = None
        self.active_diagnosis_thread: Optional[threading.Thread] = None
        self.failed_components: List[str] = []

        # Metrics for incident timeline and comparison
        self.detection_delay_sim_time: Optional[float] = None
        self.recovery_time_sim_time: Optional[float] = None
        self.incident_start_sim_time: Optional[float] = None
        self.fault_onset_sim_time: Optional[float] = None
        self.link_probe_misses: Dict[str, int] = {}
        self.link_probe_history: Dict[str, deque] = {}
        self.last_probe_sim_time: float = 0.0
        self.probe_interval: float = 1.0

        # One-way Telegram incident notification worker
        self.notifier = TelegramNotifier()
        self.run_id: str = f"run-{int(time.time())}"
        self.incident_counter: int = 0
        self.current_incident_id: Optional[str] = None

    def reset(self) -> None:
        self.telemetry.reset()
        self.detector.reset()
        self.ai_client.new_epoch()
        self.healing.reset()
        if hasattr(self, "notifier") and self.notifier is not None:
            self.notifier.reset()
        self.current_incident_id = None
        self.incident_state = IncidentState.HEALTHY
        self.active_scenario = None
        self.scenario_step = 0
        self.sim_step_count = 0
        self.incident_history.clear()
        self.active_diagnosis = None
        self.previous_route = None
        self.failed_components.clear()
        self.detection_delay_sim_time = None
        self.recovery_time_sim_time = None
        self.incident_start_sim_time = None
        self.fault_onset_sim_time = None
        self.link_probe_misses.clear()
        self.link_probe_history.clear()
        self.last_probe_sim_time = 0.0
        self.current_route = ["Client", "R1", "R2", "R4", "R6", "Server"]
        if hasattr(self.session, "simulator") and hasattr(self.session.simulator, "router"):
            graph = self.session.simulator.topology.graph
            if not all(graph.has_edge(a, b) for a, b in zip(self.current_route, self.current_route[1:])):
                self.current_route = []
            self.session.simulator.router.static_route = list(self.current_route) if self.current_route else None

    def set_auto_heal(self, enabled: bool) -> None:
        self.healing.auto_heal_enabled = bool(enabled)
        if not enabled and hasattr(self.session, "simulator") and hasattr(self.session.simulator, "router"):
            self.session.simulator.router.static_route = list(self.current_route)
        self.log_incident_event(f"Auto-Heal set to {'ON' if enabled else 'OFF'}")


    def inject_scenario(self, scenario_name: str) -> None:
        if scenario_name not in {item.value for item in ReRouteScenarioType}:
            raise ValueError("Unknown recovery scenario")
        required = {"Client", "Server", "R1", "R2", "R3", "R4", "R5", "R6"}
        if not required.issubset(self.session.simulator.topology.devices):
            raise ValueError("Load the recovery demo before injecting a scripted fault")
        self.reset()
        self.active_scenario = scenario_name
        self.incident_start_sim_time = self.session.simulator.time
        self.log_incident_event(f"Scenario started: {scenario_name}")

    def log_incident_event(self, text: str) -> None:
        entry = {
            "step": self.sim_step_count,
            "sim_time": round(self.session.simulator.time, 3),
            "state": self.incident_state.value,
            "message": text,
        }
        self.incident_history.append(entry)
        del self.incident_history[:-500]

    def step(self) -> Dict[str, Any]:
        """
        Executes a single coordinated step of the ReRoute simulator.
        1. Fault schedule progression for active scenario.
        2. Advance simulation step (packet processing).
        3. Collect telemetry from the packet and links.
        4. Run explainable detector.
        5. Update incident lifecycle state machine.
        6. If Detected -> trigger non-blocking Qualcomm AI diagnosis.
        7. If Auto-heal enabled and Action selected -> apply deterministic reroute.
        8. If Verifying -> verify recovery window.
        """
        self.sim_step_count += 1
        self.scenario_step += 1

        # 1. Progress active scenario faults
        self._apply_scenario_faults()

        # 2. Advance simulation packet step
        sim_state = self.session.step()
        last_pkt = sim_state.get("last_packet")

        # 3. Telemetry collection
        if last_pkt:
            pkt_id = last_pkt.get("id", self.sim_step_count)
            status = last_pkt.get("status", "DELIVERED")
            raw_lat = last_pkt.get("latency")
            # Convert latency from seconds to milliseconds at the telemetry boundary
            lat_ms = (float(raw_lat) * 1000.0) if raw_lat is not None else None
            route = last_pkt.get("route") or self.current_route
            if route:
                self.current_route = route
            self.telemetry.record_packet(
                pkt_id, self.session.simulator.time, "Client", "Server",
                status, lat_ms, self.current_route
            )

        # Scheduled simulated heartbeat probes across all links
        current_time = self.session.simulator.time
        probe_fired = (current_time - self.last_probe_sim_time >= self.probe_interval) or (self.sim_step_count == 1)
        if probe_fired:
            self.last_probe_sim_time = current_time

        topo = self.session.simulator.topology
        for u, v, data in topo.graph.edges(data=True):
            key = f"{u}-{v}"
            is_down = data.get("status") == "DOWN"
            if key not in self.link_probe_history:
                self.link_probe_history[key] = deque(maxlen=10)

            if probe_fired:
                if is_down:
                    self.link_probe_misses[key] = self.link_probe_misses.get(key, 0) + 1
                    self.link_probe_history[key].append(0)  # probe lost / failed
                else:
                    self.link_probe_misses[key] = 0
                    self.link_probe_history[key].append(1)  # probe delivered / acknowledged

            missed_hb = self.link_probe_misses.get(key, 0)
            history = self.link_probe_history[key]
            if history:
                loss: Optional[float] = float(history.count(0) / len(history))
            else:
                loss = None

            lat = float(data.get("latency", 5.0)) + (float(data.get("congestion", 0.0)) * 20.0)
            util = float(data.get("congestion", 0.0))
            self.telemetry.update_link_telemetry(key, lat, loss, util, missed_hb)

        # 4. State Machine Progression
        self._update_incident_lifecycle(last_pkt)

        return self.get_dashboard_state()

    def _apply_scenario_faults(self) -> None:
        """Applies fault events at designated scenario step marks."""
        topo = self.session.simulator.topology
        step = self.scenario_step

        if self.active_scenario == ReRouteScenarioType.SUDDEN_LINK_FAILURE.value:
            if step == 2:
                # Fail primary trunk link R2-R4 abruptly
                if topo.graph.has_edge("R2", "R4"):
                    topo.fail_link("R2", "R4")
                    self.fault_onset_sim_time = self.session.simulator.time
                    if "R2-R4" not in self.failed_components:
                        self.failed_components.append("R2-R4")
                    self.log_incident_event(f"Injected sudden link failure on R2-R4 at sim time {self.fault_onset_sim_time:.3f}s")

        elif self.active_scenario == ReRouteScenarioType.MILD_CONGESTION.value:
            # Mild, slowly increasing congestion without packet loss
            # Latency increases smoothly across steps: 24ms (pre-breach) -> 32ms (breach) -> 42ms
            if topo.graph.has_edge("R2", "R4"):
                edge = topo.graph["R2"]["R4"]
                if step == 2:
                    edge["congestion"] = 0.20
                    edge["latency"] = 10.0
                    edge["packet_loss"] = 0.0
                    self.fault_onset_sim_time = self.session.simulator.time
                    if "R2-R4" not in self.failed_components:
                        self.failed_components.append("R2-R4")
                    self.log_incident_event("Injected mild congestion onset on R2-R4 (20%, zero loss, path latency ~24ms)")
                elif step == 4:
                    edge["congestion"] = 0.40
                    edge["latency"] = 18.0
                    edge["packet_loss"] = 0.0
                    self.log_incident_event("Mild congestion escalating on R2-R4 (40%, zero loss, path latency ~32ms)")
                elif step == 6:
                    edge["congestion"] = 0.65
                    edge["latency"] = 28.0
                    edge["packet_loss"] = 0.0
                    self.log_incident_event("Mild congestion reached elevated latency on R2-R4 (65%, zero loss)")

        elif self.active_scenario == ReRouteScenarioType.GRADUAL_CONGESTION.value:
            # Gradually increase congestion on R2-R4
            if topo.graph.has_edge("R2", "R4"):
                edge = topo.graph["R2"]["R4"]
                if step == 2:
                    edge["congestion"] = 0.4
                    edge["latency"] = 25.0
                    edge["packet_loss"] = 0.08
                    self.fault_onset_sim_time = self.session.simulator.time
                    self.log_incident_event("Injected minor congestion on R2-R4 (40%)")
                elif step == 4:
                    edge["congestion"] = 0.85
                    edge["latency"] = 65.0
                    edge["packet_loss"] = 0.25
                    if "R2-R4" not in self.failed_components:
                        self.failed_components.append("R2-R4")
                    self.log_incident_event("Severe congestion on R2-R4 (85%, 25% loss)")

        elif self.active_scenario == ReRouteScenarioType.NO_ALTERNATE_PATH.value:
            if step == 2:
                # Isolate egress router R6 so all paths to Server are blocked
                if topo.graph.has_edge("R4", "R6"):
                    topo.fail_link("R4", "R6")
                if topo.graph.has_edge("R5", "R6"):
                    topo.fail_link("R5", "R6")
                self.fault_onset_sim_time = self.session.simulator.time
                for comp in ["R4-R6", "R5-R6"]:
                    if comp not in self.failed_components:
                        self.failed_components.append(comp)
                self.log_incident_event("Injected dual trunk failure (R4-R6 and R5-R6 down - no alternate route exists)")

    def _update_incident_lifecycle(self, last_pkt: Optional[Dict[str, Any]]) -> None:
        """Evaluates detection and drives lifecycle transitions."""
        det_level, evidence, suspected = self.detector.evaluate(self.telemetry, self.current_route)

        # Healthy -> Warning / Detected
        if self.incident_state == IncidentState.HEALTHY:
            if det_level == "DETECTED":
                self._transition_to_detected(evidence, suspected)
            elif det_level == "WARNING":
                self.incident_state = IncidentState.WARNING
                self.log_incident_event(f"Anomaly Warning: {'; '.join(evidence)}")

        elif self.incident_state == IncidentState.WARNING:
            if det_level == "DETECTED":
                self._transition_to_detected(evidence, suspected)
            elif det_level == "HEALTHY":
                self.incident_state = IncidentState.HEALTHY
                self.log_incident_event("Telemetry normalized, returned to Healthy")

        elif self.incident_state == IncidentState.DETECTED:
            # Transition to Diagnosing with Qualcomm AI
            self.incident_state = IncidentState.DIAGNOSING
            self.log_incident_event("Invoking Qualcomm Cloud AI 100 diagnostic agent...")
            self._trigger_ai_diagnosis(evidence, suspected)

        elif self.incident_state == IncidentState.DIAGNOSING:
            # If AI thread has completed and set active_diagnosis or transitioned
            if not self.ai_client.is_busy and self.active_diagnosis:
                self.incident_state = IncidentState.ACTION_SELECTED
                if self.healing.auto_heal_enabled:
                    self._execute_rerouting()

        elif self.incident_state == IncidentState.ACTION_SELECTED:
            if not self.healing.auto_heal_enabled:
                self.log_incident_event("Auto-Heal is OFF. Preserving baseline policy (traffic continues on degraded path).")
                # Remains in Action Selected / degraded without rerouting
            else:
                self._execute_rerouting()

        elif self.incident_state == IncidentState.REROUTING:
            self.incident_state = IncidentState.VERIFYING
            self.log_incident_event("New path active. Verifying recovery over rolling packet window...")

        elif self.incident_state == IncidentState.VERIFYING:
            if last_pkt:
                pkt_id = last_pkt.get("id")
                pkt_time = last_pkt.get("time", self.session.simulator.time)
                pkt_route = last_pkt.get("route") or self.current_route
                status = last_pkt.get("status", "DROPPED")
                raw_lat = last_pkt.get("latency")
                lat_ms = (float(raw_lat) * 1000.0) if raw_lat is not None else None
                is_verified = self.healing.record_verification_packet(
                    status=status,
                    latency_ms=lat_ms,
                    packet_id=pkt_id,
                    timestamp=pkt_time,
                    route=pkt_route,
                )
                if is_verified:
                    self.incident_state = IncidentState.RECOVERED
                    ref_start = self.fault_onset_sim_time or self.incident_start_sim_time or 0.0
                    self.recovery_time_sim_time = round(max(0.0, self.session.simulator.time - ref_start), 3)
                    self.log_incident_event(f"Verified Recovered! 5 consecutive packets delivered on bypass route within latency limits. Recovery duration: {self.recovery_time_sim_time}s relative to fault onset.")
                    if hasattr(self, "notifier") and self.notifier is not None:
                        inc_id = self.current_incident_id or f"INC-{self.incident_counter:04d}"
                        comp = self.active_diagnosis.affected_component if self.active_diagnosis else None
                        self.notifier.notify_incident(
                            IncidentAlertPayload(
                                run_id=self.run_id,
                                incident_id=inc_id,
                                event_type="RECOVERED",
                                epoch=self.notifier.current_epoch,
                                timestamp=self.session.simulator.time,
                                affected_component=comp,
                                new_route=list(self.current_route),
                                recovery_duration_s=self.recovery_time_sim_time,
                                details=f"Recovery duration: {self.recovery_time_sim_time:.3f}s relative to fault onset",
                            )
                        )
                elif status == "DROPPED":
                    # Packet drop while verifying
                    if self.active_scenario == ReRouteScenarioType.NO_ALTERNATE_PATH.value:
                        self.incident_state = IncidentState.UNRESOLVED
                        self.log_incident_event("Unresolved: No alternate path exists meeting capacity. Packets dropping.")
                        if hasattr(self, "notifier") and self.notifier is not None:
                            inc_id = self.current_incident_id or f"INC-{self.incident_counter:04d}"
                            comp = self.active_diagnosis.affected_component if self.active_diagnosis else None
                            self.notifier.notify_incident(
                                IncidentAlertPayload(
                                    run_id=self.run_id,
                                    incident_id=inc_id,
                                    event_type="UNRESOLVED",
                                    epoch=self.notifier.current_epoch,
                                    timestamp=self.session.simulator.time,
                                    affected_component=comp,
                                    details="No alternate path exists meeting capacity. Packets dropping.",
                                )
                            )

        elif self.incident_state == IncidentState.UNRESOLVED:
            if (self.healing.auto_heal_enabled and
                    self._topology_signature() != getattr(self, "_last_route_topology", None)):
                self.log_incident_event("Network changed: rechecking recovery routes.")
                self._execute_rerouting()

        elif self.incident_state == IncidentState.RECOVERED:
            # A second fault after recovery creates a new incident lifecycle
            if det_level == "DETECTED":
                self.healing.consecutive_delivered_on_new_route = 0
                self._transition_to_detected(evidence, suspected)
            elif det_level == "WARNING":
                self.incident_state = IncidentState.WARNING
                self.log_incident_event(f"New Anomaly Warning after recovery: {'; '.join(evidence)}")

    def _transition_to_detected(self, evidence: List[str], suspected: Optional[str]) -> None:
        self.recovery_time_sim_time = None
        self.healing.delivery_restored = False
        self.healing.service_target_met = False
        self.active_diagnosis = None
        self.incident_counter += 1
        self.current_incident_id = f"INC-{self.incident_counter:04d}"
        self.incident_state = IncidentState.DETECTED
        ref_start = self.fault_onset_sim_time or self.incident_start_sim_time or 0.0
        self.detection_delay_sim_time = round(max(0.0, self.session.simulator.time - ref_start), 3)
        self.log_incident_event(f"Failure Detected! Detection delay: {self.detection_delay_sim_time}s relative to fault onset. Evidence: {'; '.join(evidence)}")

        if hasattr(self, "notifier") and self.notifier is not None:
            self.notifier.notify_incident(
                IncidentAlertPayload(
                    run_id=self.run_id,
                    incident_id=self.current_incident_id,
                    event_type="DETECTED",
                    epoch=self.notifier.current_epoch,
                    timestamp=self.session.simulator.time,
                    affected_component=suspected,
                    evidence=evidence,
                    details=f"Detection delay: {self.detection_delay_sim_time}s relative to fault onset",
                )
            )

    def _trigger_ai_diagnosis(self, evidence: List[str], suspected: Optional[str]) -> None:
        epoch = self.ai_client.new_epoch()
        topo_comps = [f"{u}-{v}" for u, v in self.session.simulator.topology.graph.edges()] + list(self.session.simulator.topology.devices.keys())

        def _on_diag(result: AIDiagnosisResult) -> None:
            with self.session.lock:
                if epoch != self.ai_client.current_epoch:
                    return
                self.active_diagnosis = result
                fallback_label = " [RULE FALLBACK]" if result.is_fallback else f" [QUALCOMM AI 100 - {result.model_name}]"
                self.log_incident_event(
                    f"Diagnosis complete{fallback_label}: Cause={result.likely_cause}, "
                    f"Component={result.affected_component}, Action={result.recommended_action} "
                    f"(Latency: {result.wall_clock_ms}ms)"
                )
                self.incident_state = IncidentState.ACTION_SELECTED
                if self.healing.auto_heal_enabled:
                    self._execute_rerouting()
        self.active_diagnosis_thread = self.ai_client.diagnose_async(
            epoch=epoch,
            telemetry_summary=self.telemetry.to_summary_dict(),
            topology_components=topo_comps,
            current_route=self.current_route,
            detector_evidence=evidence,
            suspected_component=suspected,
            on_complete=_on_diag,
        )

    def _topology_signature(self):
        topo = self.session.simulator.topology
        return (
            tuple(sorted((name, topo.active_node(name)) for name in topo.devices)),
            tuple(sorted((min(u, v), max(u, v), edge.get("status", "UP"),
                          edge.get("latency", 5), edge.get("bandwidth", 100),
                          edge.get("congestion", 0), edge.get("packet_loss", 0))
                         for u, v, edge in topo.graph.edges(data=True))),
        )

    def _execute_rerouting(self) -> None:
        """Finds deterministic alternative path excluding the affected component."""
        topo = self.session.simulator.topology
        self._last_route_topology = self._topology_signature()
        diag = self.active_diagnosis
        comp = diag.affected_component if diag else None

        # AI suspicion alone must not remove a healthy edge from the only usable route.
        # Corroborate against current link/node state before excluding a component.
        if comp and comp in topo.devices:
            if topo.active_node(comp):
                comp = None
        elif comp and "-" in comp:
            u, v = comp.split("-", 1)
            edge = topo.graph.get_edge_data(u, v)
            if edge is not None and edge.get("status", "UP") == "UP":
                degraded = (float(edge.get("packet_loss", 0)) >= 0.05
                            or float(edge.get("congestion", 0)) >= 0.30)
                if not degraded:
                    comp = None

        alt = self.healing.calculate_alternative_path(topo, "Client", "Server", exclude_component=comp)
        if not alt:
            self.recovery_time_sim_time = None
            self.incident_state = IncidentState.UNRESOLVED
            self.log_incident_event("Unresolved: All alternate routes exhausted or blocked. Packets will be dropped.")
            if hasattr(self, "notifier") and self.notifier is not None:
                inc_id = self.current_incident_id or f"INC-{self.incident_counter:04d}"
                self.notifier.notify_incident(
                    IncidentAlertPayload(
                        run_id=self.run_id,
                        incident_id=inc_id,
                        event_type="UNRESOLVED",
                        epoch=self.notifier.current_epoch,
                        timestamp=self.session.simulator.time,
                        affected_component=comp,
                        details="All alternate routes exhausted or blocked. Packets will be dropped.",
                    )
                )
            return

        new_route, cost = alt
        route_is_healthy = all(
            topo.graph[a][b].get("status", "UP") == "UP"
            and float(topo.graph[a][b].get("packet_loss", 0)) == 0
            and float(topo.graph[a][b].get("congestion", 0)) < 0.30
            for a, b in zip(new_route, new_route[1:])
        )
        if new_route == self.current_route and not route_is_healthy:
            self.recovery_time_sim_time = None
            self.incident_state = IncidentState.UNRESOLVED
            self.log_incident_event("Unresolved: Alternative path equals degraded path.")
            if hasattr(self, "notifier") and self.notifier is not None:
                inc_id = self.current_incident_id or f"INC-{self.incident_counter:04d}"
                self.notifier.notify_incident(
                    IncidentAlertPayload(
                        run_id=self.run_id,
                        incident_id=inc_id,
                        event_type="UNRESOLVED",
                        epoch=self.notifier.current_epoch,
                        timestamp=self.session.simulator.time,
                        affected_component=comp,
                        details="Alternative path equals degraded path.",
                    )
                )
            return

        old_route = list(self.current_route)
        self.previous_route = list(old_route)
        self.current_route = new_route
        self.incident_state = IncidentState.REROUTING
        # Reset rolling packet observations and update baseline latency to new path cost
        self.telemetry.packet_history.clear()
        self.telemetry.baseline_latency = max(19.0, float(cost))
        self.healing.consecutive_delivered_on_new_route = 0
        self.healing.expected_route = list(new_route)
        self.healing.action_sim_time = self.session.simulator.time
        self.healing.verified_packet_ids.clear()
        self.healing.verification_latencies.clear()
        self.healing.delivery_restored = False
        self.healing.service_target_met = False
        self.log_incident_event(
            f"Rerouting executed in engine: {' -> '.join(old_route)} ➔ {' -> '.join(new_route)} (Cost: {cost:.1f})"
        )

        # Update active flow and router static route in simulator
        for flow in self.session.simulator.active_flows.values():
            flow.previous_route = old_route
            flow.current_route = list(new_route)
            flow.route_status = "REROUTED"

        if hasattr(self.session, "simulator") and hasattr(self.session.simulator, "router"):
            self.session.simulator.router.static_route = list(new_route)

    def on_topology_change(self) -> None:
        """Handle dynamic manual edits: device shutdowns, link deletions, reconnections."""
        was_diagnosing = self.incident_state == IncidentState.DIAGNOSING
        self.ai_client.new_epoch()
        if was_diagnosing:
            self.active_diagnosis = None
            self.incident_state = IncidentState.DETECTED
        topo = self.session.simulator.topology

        # Check if endpoints still exist and are active
        if "Client" not in topo.graph or "Server" not in topo.graph or not topo.active_node("Client") or not topo.active_node("Server"):
            self.incident_state = IncidentState.UNRESOLVED
            self.current_route = []
            self.log_incident_event("Topology modified: Endpoints missing or inactive. Traffic halted.")
            return

        # Check if current route is broken
        route_broken = False
        for i in range(len(self.current_route) - 1):
            u, v = self.current_route[i], self.current_route[i + 1]
            if not topo.active_node(u) or not topo.active_node(v) or not topo.graph.has_edge(u, v):
                route_broken = True
                break
            edge = topo.graph[u][v]
            if edge.get("status") == "DOWN":
                route_broken = True
                break

        if route_broken:
            if self.healing.auto_heal_enabled:
                self.log_incident_event("Manual edit severed active route. Initiating self-healing...")
                self._execute_rerouting()
            else:
                self.incident_state = IncidentState.UNRESOLVED
                self.log_incident_event("Manual edit severed route. Auto-Heal is OFF: packets dropping.")
        elif self.incident_state == IncidentState.UNRESOLVED and self.healing.auto_heal_enabled:
            # Check if network repair allows reconnecting
            alt = self.healing.calculate_alternative_path(topo, "Client", "Server")
            if alt:
                self.log_incident_event("Repaired link/device restored connectivity. Rerouting...")
                self._execute_rerouting()

    def get_dashboard_state(self) -> Dict[str, Any]:
        """Provides complete unified state for the frontend dashboard."""
        return {
            "incident_state": self.incident_state.value,
            "auto_heal_enabled": self.healing.auto_heal_enabled,
            "active_scenario": self.active_scenario,
            "current_route": self.current_route,
            "previous_route": self.previous_route,
            "failed_components": list(self.failed_components),
            "telemetry": self.telemetry.to_summary_dict(),
            "incident_timeline": self.incident_history[-10:],
            "delivery_restored": self.healing.delivery_restored,
            "service_target_met": self.healing.service_target_met,
            "service_target_latency_ms": self.healing.service_target_latency_ms,
            "recovery_max_latency_ms": self.healing.recovery_max_latency_ms,
            "last_verified_latency_ms": self.healing.last_verified_latency_ms,
            "diagnosis": {
                "likely_cause": self.active_diagnosis.likely_cause if self.active_diagnosis else None,
                "affected_component": self.active_diagnosis.affected_component if self.active_diagnosis else None,
                "evidence": self.active_diagnosis.evidence if self.active_diagnosis else [],
                "recommended_action": self.active_diagnosis.recommended_action if self.active_diagnosis else None,
                "explanation": self.active_diagnosis.explanation if self.active_diagnosis else None,
                "model_name": self.active_diagnosis.model_name if self.active_diagnosis else None,
                "wall_clock_ms": self.active_diagnosis.wall_clock_ms if self.active_diagnosis else None,
                "is_fallback": self.active_diagnosis.is_fallback if self.active_diagnosis else False,
            } if self.active_diagnosis else None,
            "metrics": {
                "detection_delay_sim_time": self.detection_delay_sim_time,
                "recovery_time_sim_time": self.recovery_time_sim_time,
                "recent_pdr_pct": self.telemetry.get_recent_delivery_rate(),
                "recent_latency_ms": self.telemetry.to_summary_dict()["recent_avg_latency_ms"],
            },
        }


# -----------------------------------------------------------------------------
# 8. Milestone 5: Repeatable Comparison Runner (A/B Test)
# -----------------------------------------------------------------------------

@dataclass
class PhaseMetrics:
    phase_name: str
    packets_offered: int = 0
    packets_delivered: int = 0
    packets_dropped: int = 0
    delivery_rate_pct: Optional[float] = None
    avg_latency_ms: Optional[float] = None


@dataclass
class RunResult:
    auto_heal: bool
    scenario_name: str
    pre_fault: PhaseMetrics
    disruption: PhaseMetrics
    post_action: PhaseMetrics
    detection_delay_sim_time: Optional[float] = None
    recovery_time_sim_time: Optional[float] = None
    final_status: str = "Healthy"
    wall_clock_ai_response_ms: Optional[float] = None
    diagnosis_provider: str = "Deterministic Rule Fallback"


@dataclass
class ComparisonReport:
    scenario: str
    seed: int
    offered_packets: int
    auto_heal_off: RunResult
    auto_heal_on: RunResult
    pdr_delta_pct: Optional[float] = None
    latency_delta_ms: Optional[float] = None
    diagnosis_provider: str = "Deterministic Rule Fallback"

    def to_dict(self) -> Dict[str, Any]:
        def _phase_dict(pm: PhaseMetrics) -> Dict[str, Any]:
            return {
                "phase_name": pm.phase_name,
                "packets_offered": pm.packets_offered,
                "packets_delivered": pm.packets_delivered,
                "packets_dropped": pm.packets_dropped,
                "delivery_rate_pct": pm.delivery_rate_pct if pm.delivery_rate_pct is not None else "N/A",
                "avg_latency_ms": pm.avg_latency_ms if pm.avg_latency_ms is not None else "N/A",
            }

        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "offered_packets": self.offered_packets,
            "diagnosis_provider": self.diagnosis_provider,
            "auto_heal_off": {
                "final_status": self.auto_heal_off.final_status,
                "detection_delay_sim_s": self.auto_heal_off.detection_delay_sim_time,
                "recovery_time_sim_s": self.auto_heal_off.recovery_time_sim_time if self.auto_heal_off.recovery_time_sim_time is not None else "not recovered within observation window",
                "diagnosis_provider": self.auto_heal_off.diagnosis_provider,
                "pre_fault": _phase_dict(self.auto_heal_off.pre_fault),
                "disruption": _phase_dict(self.auto_heal_off.disruption),
                "post_action": _phase_dict(self.auto_heal_off.post_action),
                "wall_clock_ai_response_ms": self.auto_heal_off.wall_clock_ai_response_ms,
            },
            "auto_heal_on": {
                "final_status": self.auto_heal_on.final_status,
                "detection_delay_sim_s": self.auto_heal_on.detection_delay_sim_time,
                "recovery_time_sim_s": self.auto_heal_on.recovery_time_sim_time if self.auto_heal_on.recovery_time_sim_time is not None else "not recovered within observation window",
                "diagnosis_provider": self.auto_heal_on.diagnosis_provider,
                "pre_fault": _phase_dict(self.auto_heal_on.pre_fault),
                "disruption": _phase_dict(self.auto_heal_on.disruption),
                "post_action": _phase_dict(self.auto_heal_on.post_action),
                "wall_clock_ai_response_ms": self.auto_heal_on.wall_clock_ai_response_ms,
            },
            "summary_comparison": {
                "post_action_pdr_improvement_pct": round(self.pdr_delta_pct, 2) if self.pdr_delta_pct is not None else "N/A",
                "post_action_latency_delta_ms": round(self.latency_delta_ms, 2) if self.latency_delta_ms is not None else "N/A",
            }
        }

    def to_csv(self) -> str:
        def _fmt_pdr(val: Optional[float]) -> str:
            return f"{val:.1f}%" if val is not None else "N/A"

        def _fmt_lat(val: Optional[float]) -> str:
            return f"{val:.2f}" if val is not None else "N/A"

        lines = [
            "Metric,Auto-Heal OFF,Auto-Heal ON,Unit",
            f"Scenario,{self.scenario},{self.scenario},name",
            f"Diagnosis Provider,{self.diagnosis_provider},{self.diagnosis_provider},provider",
            f"Final Lifecycle Status,{self.auto_heal_off.final_status},{self.auto_heal_on.final_status},state",
            f"Detection Delay,{self.auto_heal_off.detection_delay_sim_time or 'N/A'},{self.auto_heal_on.detection_delay_sim_time or 'N/A'},sim_seconds",
            f"Recovery Time,{self.auto_heal_off.recovery_time_sim_time if self.auto_heal_off.recovery_time_sim_time is not None else 'not recovered within window'},{self.auto_heal_on.recovery_time_sim_time if self.auto_heal_on.recovery_time_sim_time is not None else 'not recovered within window'},sim_seconds",
            f"Pre-Fault PDR,{_fmt_pdr(self.auto_heal_off.pre_fault.delivery_rate_pct)},{_fmt_pdr(self.auto_heal_on.pre_fault.delivery_rate_pct)},percent",
            f"Disruption PDR,{_fmt_pdr(self.auto_heal_off.disruption.delivery_rate_pct)},{_fmt_pdr(self.auto_heal_on.disruption.delivery_rate_pct)},percent",
            f"Post-Action PDR,{_fmt_pdr(self.auto_heal_off.post_action.delivery_rate_pct)},{_fmt_pdr(self.auto_heal_on.post_action.delivery_rate_pct)},percent",
            f"Post-Action Latency,{_fmt_lat(self.auto_heal_off.post_action.avg_latency_ms)},{_fmt_lat(self.auto_heal_on.post_action.avg_latency_ms)},ms",
            f"AI Wall-Clock Response,{self.auto_heal_off.wall_clock_ai_response_ms or 'N/A'},{self.auto_heal_on.wall_clock_ai_response_ms or 'N/A'},wall_ms",
        ]
        return "\n".join(lines)


def run_comparison_benchmark(
    scenario_name: str = "sudden_link_failure",
    offered_packets: int = 30,
    seed: int = 42,
    diagnosis_provider: Optional[Any] = None,
) -> ComparisonReport:
    """
    Executes a strictly repeatable A/B benchmark comparing Auto-Heal OFF vs Auto-Heal ON.
    Uses identical initial topology, identical offered traffic workload, and isolated RNG.
    Aligns post-action observation windows based on when the action point occurred.
    - Honors offered_packets exactly or validates minimum requirement (at least 15 packets).
    - Injects deterministic diagnosis provider (default: Deterministic Rule Fallback).
    - Sets avg_latency_ms to None when delivered packets == 0.
    """
    if offered_packets < 15:
        raise ValueError("offered_packets must be at least 15 to accommodate pre-fault baseline and post-action evaluation")

    try:
        from lab_session import LabSession
    except ModuleNotFoundError:
        from netadapt.lab_session import LabSession

    provider_name = "Deterministic Rule Fallback"
    if diagnosis_provider is not None:
        if isinstance(diagnosis_provider, str):
            provider_name = diagnosis_provider
        elif hasattr(diagnosis_provider, "model_name"):
            provider_name = str(diagnosis_provider.model_name)
        elif hasattr(diagnosis_provider, "__name__"):
            provider_name = diagnosis_provider.__name__
        else:
            provider_name = str(diagnosis_provider)

    # First run: run Auto-Heal ON to identify the exact action trigger packet index
    def _execute_run_recording_packets(auto_heal: bool) -> Tuple[RunResult, List[Dict[str, Any]], int]:
        random.seed(seed)
        session = LabSession()
        session.seed = seed
        session.document.setdefault("settings", {})["seed"] = seed
        session.load_reroute_demo()
        session.simulator.rng = random.Random(seed)
        ctrl = session.reroute_controller
        ctrl.healing.auto_heal_enabled = auto_heal
        # Requirement 9: Disable external notifications in automated tests and comparison benchmarks
        if hasattr(ctrl, "notifier") and ctrl.notifier is not None:
            ctrl.notifier.enabled = False

        # Inject deterministic diagnosis provider to remove dependence on live network / thread timing
        ctrl.ai_client.api_key = ""  # Enforces offline deterministic rule fallback without network latency
        if diagnosis_provider is not None and callable(diagnosis_provider):
            original_diagnose_async = ctrl.ai_client.diagnose_async
            def _custom_diagnose_async(epoch, telemetry_summary, topology_components, current_route, detector_evidence, suspected_component, on_complete):
                res = diagnosis_provider(epoch, telemetry_summary, topology_components, current_route, detector_evidence, suspected_component)
                ctrl.ai_client._dispatch_if_current(epoch, res, on_complete)
                return None
            ctrl.ai_client.diagnose_async = _custom_diagnose_async

        packets_log: List[Dict[str, Any]] = []
        action_idx = -1

        # Phase 1: Pre-fault baseline (first 10 packets)
        for i in range(10):
            res = session.reroute_step()
            last_p = res.get("last_packet")
            if last_p:
                raw_l = last_p.get("latency")
                lat_ms = (float(raw_l) * 1000.0) if raw_l is not None else None
                packets_log.append({"status": last_p.get("status", "DELIVERED"), "latency": lat_ms})

        # Phase 2: Inject scenario fault
        ctrl.inject_scenario(scenario_name)

        # Phase 2 & 3: Run remaining packets up to offered_packets exactly
        for i in range(10, offered_packets):
            res = session.reroute_step()
            # If in DIAGNOSING state during an offline benchmark evaluation, await diagnosis if thread exists
            if ctrl.incident_state == IncidentState.DIAGNOSING and ctrl.active_diagnosis_thread:
                ctrl.active_diagnosis_thread.join(timeout=4.0)
                # re-evaluate lifecycle now that diagnosis finished
                ctrl._update_incident_lifecycle(None)

            last_p = res.get("last_packet")
            if action_idx == -1 and ctrl.incident_state in (IncidentState.REROUTING, IncidentState.VERIFYING, IncidentState.RECOVERED):
                action_idx = len(packets_log)
            if last_p:
                raw_l = last_p.get("latency")
                lat_ms = (float(raw_l) * 1000.0) if raw_l is not None else None
                packets_log.append({"status": last_p.get("status", "DROPPED"), "latency": lat_ms})

        wall_ms = ctrl.active_diagnosis.wall_clock_ms if ctrl.active_diagnosis else 0.1
        actual_provider = ctrl.active_diagnosis.model_name if (ctrl.active_diagnosis and ctrl.active_diagnosis.model_name) else provider_name
        return (
            RunResult(
                auto_heal=auto_heal,
                scenario_name=scenario_name,
                pre_fault=PhaseMetrics("pre_fault"),
                disruption=PhaseMetrics("disruption"),
                post_action=PhaseMetrics("post_action"),
                detection_delay_sim_time=ctrl.detection_delay_sim_time,
                recovery_time_sim_time=ctrl.recovery_time_sim_time,
                final_status=ctrl.incident_state.value,
                wall_clock_ai_response_ms=wall_ms,
                diagnosis_provider=actual_provider,
            ),
            packets_log,
            action_idx,
        )

    run_on, packets_on, action_idx = _execute_run_recording_packets(auto_heal=True)
    run_off, packets_off, _ = _execute_run_recording_packets(auto_heal=False)

    # Use the action split index from the ON run (or fault + 2 if never triggered)
    split_idx = action_idx if action_idx != -1 else min(12, len(packets_on))

    def _compile_phases(pkts: List[Dict[str, Any]]) -> Tuple[PhaseMetrics, PhaseMetrics, PhaseMetrics]:
        def _calc_subset(name: str, items: List[Dict[str, Any]]) -> PhaseMetrics:
            offered = len(items)
            if offered == 0:
                return PhaseMetrics(phase_name=name, packets_offered=0, packets_delivered=0, packets_dropped=0, delivery_rate_pct=None, avg_latency_ms=None)
            delivered = sum(1 for p in items if p["status"] == "DELIVERED")
            dropped = sum(1 for p in items if p["status"] == "DROPPED")
            lats = [p["latency"] for p in items if p["status"] == "DELIVERED" and p["latency"] is not None]
            pdr = round((delivered / offered) * 100.0, 1)
            # When delivered == 0, avg latency is None (N/A) rather than 0.0
            avg_lat = round(sum(lats) / len(lats), 2) if lats else None
            return PhaseMetrics(name, offered, delivered, dropped, pdr, avg_lat)

        pre_list = pkts[:10]
        disrupt_list = pkts[10:split_idx]
        post_list = pkts[split_idx:]
        return _calc_subset("pre_fault", pre_list), _calc_subset("disruption", disrupt_list), _calc_subset("post_action", post_list)

    run_on.pre_fault, run_on.disruption, run_on.post_action = _compile_phases(packets_on)
    run_off.pre_fault, run_off.disruption, run_off.post_action = _compile_phases(packets_off)

    pdr_on = run_on.post_action.delivery_rate_pct
    pdr_off = run_off.post_action.delivery_rate_pct
    pdr_delta = (pdr_on - pdr_off) if (pdr_on is not None and pdr_off is not None) else None

    lat_on = run_on.post_action.avg_latency_ms
    lat_off = run_off.post_action.avg_latency_ms
    lat_delta = (lat_on - lat_off) if (lat_on is not None and lat_off is not None) else None

    report_provider = run_on.diagnosis_provider or provider_name
    return ComparisonReport(
        scenario=scenario_name,
        seed=seed,
        offered_packets=offered_packets,
        auto_heal_off=run_off,
        auto_heal_on=run_on,
        pdr_delta_pct=pdr_delta,
        latency_delta_ms=lat_delta,
        diagnosis_provider=report_provider,
    )

