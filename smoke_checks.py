"""Independent, local comparison checks. No cloud credentials or real network changes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent

def request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)

def netadapt():
    proc = subprocess.Popen([sys.executable, "lab_server.py", "--host", "127.0.0.1", "--port", "18765"], cwd=ROOT / "netadapt", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(60):
            try:
                state = request("http://127.0.0.1:18765/api/state")
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode())
                time.sleep(0.5)
        else:
            raise RuntimeError("Server startup timeout")
        def action(action_name, **kwargs):
            result = request("http://127.0.0.1:18765/api/action", {"action": action_name, **kwargs})
            assert result["ok"], result
            return result["state"]
        # Build a known redundant diamond; not every node of the default demo
        # has a bypass (its destination gateway is a cut vertex).
        action("new")
        for name, kind in [("PC1", "pc"), ("R1", "router"), ("R2", "router"), ("R3", "router"), ("R4", "router"), ("PC2", "pc")]:
            action("add_device", type=kind, name=name)
        for a, b, latency in [("PC1", "R1", 1), ("R1", "R2", 1), ("R2", "R4", 1), ("R1", "R3", 5), ("R3", "R4", 5), ("R4", "PC2", 1)]:
            action("add_link", source=a, target=b, latency=latency, bandwidth=100)
        action("start_traffic", source="PC1", destination="PC2", packet_count=1)
        before = action("step")
        route = before["last_packet"]["route"]
        assert "R2" in route, route
        failed = "R2"
        action("shutdown_device", id=failed)
        for _ in range(5):
            action("step")
        action("start_traffic", source="PC1", destination="PC2", packet_count=5)
        packets = [action("step")["last_packet"] for _ in range(5)]
        assert all(p["status"] == "DELIVERED" for p in packets), packets
        assert all(failed not in p["route"] for p in packets), packets
        assert any(p["route"] != route for p in packets)
        state = request("http://127.0.0.1:18765/api/state")
        assert any(e["event"] == "FAILURE_DETECTED" for e in state["events"])
        healed_metrics = state["metrics"]
        action("shutdown_device", id="R3")
        action("start_traffic", source="PC1", destination="PC2", packet_count=1)
        isolated = action("step")["last_packet"]
        assert isolated["status"] == "DROPPED", isolated
        return {"http_api": "PASS", "failed_router": failed, "before_route": route, "after_route": packets[-1]["route"], "post_failure_delivered": len(packets), "no_alternate_path": "DROPPED as expected", "metrics": healed_metrics}
    finally:
        proc.terminate()
        proc.wait(timeout=10)

def router():
    sys.path.insert(0, str(ROOT / "router-simulator"))
    from network_core import NetworkSimulator
    sim = NetworkSimulator()
    for node in "ABCD":
        sim.add_router(node)
    for a, b, latency in [("A", "B", 1), ("B", "D", 1), ("A", "C", 3), ("C", "D", 3)]:
        sim.add_link(a, b, latency=latency)
    before, _ = sim.dijkstra("A", "D")
    assert before == ["A", "B", "D"]
    sim.update_link("B", "D", status="failed")
    after, _ = sim.dijkstra("A", "D")
    assert after == ["A", "C", "D"]
    sim.update_link("C", "D", status="failed")
    assert sim.dijkstra("A", "D")[0] == []
    return {"routing_checks": "PASS", "before_route": before, "after_route": after, "disconnected_route": []}

def streamlit_startup(name, entry):
    from streamlit.testing.v1 import AppTest
    folder = ROOT / name
    os.chdir(folder)
    sys.path.insert(0, str(folder))
    app = AppTest.from_file(str(folder / entry)).run(timeout=90)
    errors = [str(e.message) for e in app.exception]
    assert not errors, errors
    return {"app_initial_render": "PASS"}

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "netadapt":
        result = netadapt()
    elif mode == "router":
        result = router()
    else:
        name, entry = ("netadapt", "app.py") if mode == "netadapt-ui" else ("router-simulator", "main.py")
        result = streamlit_startup(name, entry)
    print(json.dumps(result, indent=2))
    (ROOT / f"{mode}-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
