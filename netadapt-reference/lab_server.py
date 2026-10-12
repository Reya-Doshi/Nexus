"""Dependency-free HTTP server for the NetAdapt interactive network laboratory.

Run with::

    python lab_server.py --port 8765

The browser sends topology and simulation actions to :class:`LabSession`;
there is no frontend-only simulation state.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lab_session import LabError, LabSession


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "lab_frontend"
SESSION = LabSession()


class LabHandler(BaseHTTPRequestHandler):
    server_version = "NetAdaptLab/1.0"

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LabError(f"Invalid JSON: {exc}") from exc

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(SESSION.state())
            return
        if path == "/api/presets":
            from lab_session import PRESETS
            self._json({"presets": [{"key": key, "name": value["name"]} for key, value in PRESETS.items()]})
            return
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        target = (FRONTEND / relative).resolve()
        if FRONTEND not in target.parents and target != FRONTEND:
            self._json({"error": "Forbidden"}, 403)
            return
        if not target.is_file():
            self._json({"error": "Not found"}, 404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if urlparse(self.path).path != "/api/action":
            self._json({"error": "Not found"}, 404)
            return
        try:
            payload = self._read_json()
            action = str(payload.get("action", ""))
            if action == "new":
                result = SESSION.new_network()
            elif action == "load_preset":
                result = SESSION.load_preset(str(payload["preset"]))
            elif action == "import":
                result = SESSION.import_document(payload["document"])
            elif action == "export":
                result = {"document": SESSION.export_document()}
            elif action == "add_device":
                result = SESSION.add_device(payload["type"], payload.get("x", 200), payload.get("y", 200), payload.get("name"))
            elif action == "move_device":
                result = SESSION.move_device(payload["id"], payload["x"], payload["y"])
            elif action == "rename_device":
                result = SESSION.rename_device(payload["id"], payload["name"])
            elif action == "delete_device":
                result = SESSION.delete_device(payload["id"])
            elif action == "duplicate_device":
                result = SESSION.duplicate_device(payload["id"])
            elif action == "add_link":
                result = SESSION.add_link(payload["source"], payload["target"], payload.get("latency", 5), payload.get("bandwidth", 100))
            elif action == "delete_link":
                result = SESSION.delete_link(payload["source"], payload["target"])
            elif action == "configure_link":
                result = SESSION.configure_link(payload["source"], payload["target"], payload.get("values", {}))
            elif action == "configure_interface":
                result = SESSION.configure_interface(payload["id"], payload["interface_id"], payload.get("values", {}))
            elif action == "shutdown_device":
                result = SESSION.shutdown_device(payload["id"])
            elif action == "restart_device":
                result = SESSION.restart_device(payload["id"])
            elif action == "start_traffic":
                result = SESSION.start_traffic(payload["source"], payload["destination"], int(payload.get("packet_count", 10)), float(payload.get("pps", 5)), str(payload.get("traffic_type", "Video")), int(payload.get("packet_size", 1500)))
            elif action == "step":
                result = SESSION.step()
            elif action == "running":
                result = SESSION.set_running(bool(payload.get("value", False)))
            elif action == "reset":
                result = SESSION.reset_simulation()
            elif action == "speed":
                result = SESSION.set_speed(float(payload.get("value", 1)))
            elif action == "scheduler":
                result = SESSION.set_scheduler(str(payload["scheduler"]))
            elif action == "qos_config":
                result = SESSION.set_qos_config(
                    payload.get("priorities"), payload.get("weights")
                )
            elif action == "routing_algorithm":
                result = SESSION.set_routing_algorithm(str(payload["algorithm"]))
            elif action == "routing_weights":
                result = SESSION.set_routing_weights(payload.get("weights", {}))
            elif action == "undo":
                result = SESSION.undo()
            elif action == "redo":
                result = SESSION.redo()
            elif action == "ping":
                result = SESSION.run_ping(
                    str(payload["source"]),
                    str(payload["destination"]),
                    int(payload.get("count", 4)),
                )
            elif action == "traceroute":
                result = SESSION.run_traceroute(
                    str(payload["source"]), str(payload["destination"])
                )
            elif action == "transport_create":
                result = SESSION.create_transport(
                    str(payload.get("protocol", "TCP")).upper(),
                    str(payload["source"]),
                    str(payload["destination"]),
                    int(payload.get("source_port", 5000)),
                    int(payload.get("destination_port", 8080)),
                    int(payload.get("payload_size", 1000)),
                    int(payload.get("initial_cwnd", 1)),
                    int(payload.get("receiver_window", 8)),
                    float(payload.get("ssthresh", 16)),
                    float(payload.get("timeout", 0.1)),
                    str(payload.get("traffic_class", "HTTP")),
                )
            elif action == "transport_send":
                result = SESSION.send_transport_data(
                    str(payload.get("protocol", "TCP")).upper(),
                    str(payload["flow_id"]),
                    int(payload.get("packet_count", 1)),
                    int(payload.get("payload_size", 1000)),
                )
            elif action == "transport_close":
                result = SESSION.close_transport(str(payload["flow_id"]))
            elif action == "transport_tick":
                result = SESSION.process_transport_tick(float(payload.get("delta", 0.1)))
            elif action == "transport_reset":
                result = SESSION.reset_transport()
            elif action == "transport_compare":
                result = SESSION.compare_transport(
                    str(payload["source"]),
                    str(payload["destination"]),
                    int(payload.get("packet_count", 6)),
                    int(payload.get("payload_size", 1000)),
                )
            elif action == "arp_table":
                result = SESSION.inspect_arp(str(payload["id"]))
            elif action == "clear_arp":
                result = SESSION.clear_arp(str(payload["id"]))
            elif action == "mac_table":
                result = SESSION.inspect_mac(str(payload["id"]))
            elif action == "clear_mac":
                result = SESSION.clear_mac(str(payload["id"]))
            elif action == "service_install":
                result = SESSION.install_service(
                    str(payload["name"]), str(payload["id"]), payload.get("config", {})
                )
            elif action == "service_remove":
                result = SESSION.remove_service(str(payload["name"]), str(payload["id"]))
            elif action == "service_control":
                result = SESSION.service_control(
                    str(payload["control"]), str(payload["name"]), str(payload["id"])
                )
            elif action == "service_configure":
                result = SESSION.configure_service(
                    str(payload["name"]), str(payload["id"]), payload.get("values", {})
                )
            elif action == "dhcp_acquire":
                result = SESSION.dhcp_acquire(str(payload["client"]), payload.get("server"))
            elif action == "dhcp_renew":
                result = SESSION.dhcp_renew(str(payload["client"]), payload.get("server"))
            elif action == "dhcp_release":
                result = SESSION.dhcp_release(str(payload["client"]))
            elif action == "dns_query":
                result = SESSION.dns_query(
                    str(payload["client"]),
                    str(payload["hostname"]),
                    payload.get("server"),
                )
            elif action == "dns_record":
                result = SESSION.dns_add_record(
                    str(payload["id"]),
                    str(payload["hostname"]),
                    str(payload["address"]),
                    float(payload.get("ttl", 300)),
                )
            elif action == "http_request":
                result = SESSION.http_request(
                    str(payload["client"]),
                    payload.get("server"),
                    str(payload.get("method", "GET")),
                    str(payload.get("path", "/")),
                    payload.get("body"),
                )
            elif action == "ftp_command":
                result = SESSION.ftp_command(
                    str(payload["client"]),
                    payload.get("server"),
                    str(payload.get("command", "LIST")),
                    payload.get("filename"),
                    payload.get("content"),
                )
            elif action == "smtp_send":
                result = SESSION.smtp_send(
                    str(payload["client"]),
                    payload.get("server"),
                    str(payload.get("sender", "student@netadapt.local")),
                    str(payload.get("recipient", "server@netadapt.local")),
                    str(payload.get("subject", "Stage 10 lab message")),
                    str(payload.get("body", "Hello from NetAdapt.")),
                )
            elif action == "firewall_add_rule":
                result = SESSION.firewall_add_rule(payload.get("rule", payload))
            elif action == "firewall_remove_rule":
                result = SESSION.firewall_remove_rule(int(payload["rule_id"]))
            elif action == "firewall_clear":
                result = SESSION.firewall_clear()
            elif action == "acl_create":
                result = SESSION.acl_create(
                    str(payload["name"]), str(payload.get("type", "STANDARD"))
                )
            elif action == "acl_add_entry":
                result = SESSION.acl_add_entry(
                    str(payload["name"]), payload.get("entry", payload)
                )
            elif action == "acl_attach":
                result = SESSION.acl_attach(
                    str(payload["name"]),
                    str(payload["id"]),
                    str(payload.get("interface_id", "eth0")),
                )
            elif action == "acl_detach":
                result = SESSION.acl_detach(
                    str(payload["name"]),
                    str(payload["id"]),
                    str(payload.get("interface_id", "eth0")),
                )
            elif action == "acl_remove":
                result = SESSION.acl_remove(str(payload["name"]))
            elif action == "security_configure":
                result = SESSION.configure_security(payload.get("values", payload))
            elif action == "arp_spoof":
                result = SESSION.arp_spoof(
                    str(payload["attacker"]),
                    str(payload["victim"]),
                    payload.get("target_ip"),
                    bool(payload.get("detect", True)),
                    payload.get("protect"),
                )
            elif action == "flood_start":
                result = SESSION.start_flood(
                    str(payload["attacker"]),
                    str(payload["target"]),
                    str(payload.get("protocol", "UDP")),
                    float(payload.get("rate", 1000)),
                    float(payload.get("duration", 1.0)),
                    payload.get("threshold"),
                    payload.get("protect"),
                )
            elif action == "learning_run":
                result = SESSION.run_learning_topic(str(payload["topic"]))
            elif action == "learning_step":
                result = SESSION.learning_step(str(payload.get("step", "next")))
            elif action == "challenge":
                result = SESSION.challenge_mode(
                    str(payload.get("mode", "state")),
                    payload.get("challenge"),
                    payload.get("key"),
                    payload.get("value"),
                )
            elif action == "quiz":
                result = SESSION.quiz_mode(
                    str(payload.get("mode", "state")), payload.get("answer")
                )
            elif action == "demo":
                result = SESSION.demo_mode(
                    str(payload.get("mode", "state")), payload.get("demo")
                )
            elif action == "packet_journey":
                result = SESSION.packet_journey(int(payload["packet_id"]))
            elif action == "explain":
                result = SESSION.explain(payload.get("packet_id"))
            elif action == "report":
                result = SESSION.network_report(str(payload.get("format", "json")))
            elif action == "console":
                result = SESSION.console(str(payload["id"]), str(payload["command"]))
            else:
                raise LabError(f"Unknown action: {action}")
            self._json({"ok": True, "state": result})
        except (KeyError, TypeError, ValueError, LabError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the lab terminal quiet; the UI timeline is the event surface.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="NetAdapt interactive network lab")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LabHandler)
    print(f"NetAdapt network lab running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
