# BUILD_STATUS: ReRoute (NC-04 AI-Assisted Self-Healing Network Simulator)

## 1. Project Overview & Scope
- **Project Name:** ReRoute (internal code: Vanguard / NC-04)
- **Goal:** AI-assisted self-healing network simulator built on top of the tested NetAdapt engine.
- **Hardware Integration:** Qualcomm Cloud AI 100 on Cirrascale platform (`https://aisuite.cirrascale.com/apis/v2`), using `Llama-3.1-8B` for live structured network diagnosis.
- **Scope Constraints Maintained:** Preserved Python discrete-event simulation engine and HTML5/SVG frontend. No databases, authentication, Kubernetes, or multi-agent frameworks introduced.

---

## 2. Implemented Behavior Across All Milestones

### Milestone 1: Focused Demo Dashboard
- **6-Router Redundant Topology:** Fixed preset (`reroute_six_router`) featuring Client, Ingress Router R1, Upper Tier (R2, R4), Lower Tier (R3, R5), Egress Router R6, Server, with 12 interconnected links.
- **Primary & Secondary Routes:** Primary baseline path (`Client -> R1 -> R2 -> R4 -> R6 -> Server`, ~19ms); secondary redundant paths via cross-links and lower tier.
- **Interactive Controls:** Start Flow, Pause, Step, Reset, Scenario selection, and Auto-Heal ON/OFF toggle.
- **Simulation-Driven SVG Canvas:** Real-time animated packet journeys, active route highlighting, failed link red coloring, and telemetry state directly from simulation engine.

### Milestone 2: Telemetry and Detection
- **Rolling Observations:** `TelemetryCollector` tracks rolling 30-packet window, delivery rate, average latency, link utilization, and heartbeats.
- **Segregated Detector Input:** Ground-truth fault injection state is strictly isolated from telemetry. The detector and AI observe only measurable anomalies.
- **Explainable Detection Rules:** Documented thresholds (latency > 35ms, packet loss > 20%, heartbeat misses >= 2), sample consistency criteria, and cooldown hysteresis.
- **Incident Lifecycle State Machine:**
  `Healthy` ➔ `Warning` ➔ `Detected` ➔ `Diagnosing` ➔ `Action selected` ➔ `Rerouting` ➔ `Verifying` ➔ `Recovered` (or `Unresolved`).

### Milestone 3: Qualcomm AI Diagnosis
- **Cirrascale Qualcomm Cloud AI 100:** Integrated via OpenAI-compatible endpoint with account API credentials loaded via local environment variables.
- **Structured Schema:** Requests and strictly validates JSON with keys: `likely_cause`, `affected_component`, `evidence`, `recommended_action`, `explanation`.
- **Validation & Non-Blocking Async:** Rejects hallucinated component names or unsupported actions. Inference runs in background thread (`join(timeout=0.25)` non-blocking loop) without freezing packet delivery.
- **Stale Epoch Invalidation:** Reset or subsequent incidents increment epoch, immediately dropping outdated in-flight AI responses.
- **Rule-Based Fallback:** Instant rule-based fallback if timeout occurs, credentials are missing, or API is unreachable, clearly labeled in dashboard.
- **Security:** Zero credential or API key leakage in logs, frontend payloads, or git commits.

### Milestone 4: Validated Healing
- **Deterministic Rerouting:** Constrained Dijkstra algorithm calculating alternative path excluding degraded/failed links and satisfying bandwidth constraints (>= 10 Mbps).
- **Auto-Heal OFF Preservation:** When Auto-Heal is OFF, the baseline static route is strictly preserved; traffic does not silently reroute, correctly dropping packets and highlighting link outage.
- **Measured Verification Window:** Verified recovery requires a sequence of 5 consecutive successfully delivered packets on the new route.
- **Permanent Link Failure:** Bypassed failed components remain permanently marked DOWN until explicit administrative repair.
- **Unresolved State:** In dual trunk failures (`no_alternate_path`), correctly transitions to `Unresolved` and records packet loss.

### Milestone 5: Repeatable Comparison Runner (A/B Testing)
- **Deterministic A/B Benchmark:** `run_comparison_benchmark` executes Auto-Heal OFF vs Auto-Heal ON under identical seeds, initial topologies, and offered packet workloads.
- **Phase Metrics:** Computes Pre-Fault, Disruption, and Post-Action packet delivery rate (PDR %) and latency (ms).
- **Simulated vs Wall-Clock Separation:** Simulated detection delay (s) and recovery time (s) are cleanly separated from wall-clock AI response latency (ms).
- **Export Formats:** Generates compact reports in both CSV (`reroute_comparison_report.csv`) and JSON (`reroute_comparison_report.json`).

---

## 3. Checks Run & Verification Results

| Check / Test Suite | Scope | Result | Notes |
|---|---|---|---|
| `test_qualcomm_conn.py` | Live Qualcomm Cloud AI 100 on Cirrascale | **PASS** (Exit 0) | Connected to `https://aisuite.cirrascale.com/apis/v2`, queried models, executed live inference with `Llama-3.1-8B`. |
| `test_telegram_notifier.py` | Telegram notification worker, formatting, dedup, rate limits, reset | **PASS** (8/8) | 8 mocked tests passed in 1.50s (`python -X utf8 -m pytest -q netadapt/test_telegram_notifier.py`). |
| `test_reroute.py` | Milestones 1–6 ReRoute engine, healing, A/B benchmark + 15 review regression tests | **PASS** (28/28) | 28 tests passed in 7.55s (`python -X utf8 -m pytest -q netadapt/test_reroute.py`). |
| NetAdapt Full Test Suite | All baseline unit tests + ReRoute tests + Telegram tests | **PASS** (345/345) | 345 tests passed in 11.77s (`python -X utf8 -m pytest -q netadapt/`). |
| Comparison Benchmark Export | Repeatable A/B evaluation | **PASS** | `reroute_comparison_report.json` and `reroute_comparison_report.csv` generated. |

### Backend Correctness Invariants Independently Verified:
1. **Healthy Traffic Never Triggers Incidents:** Continuous clean traffic produces 0 false positives (`IncidentState.HEALTHY`), 100% PDR.
2. **Observation-Driven Fault Detection:** Fault detection evaluates measured telemetry observations (loss, latency, scheduled heartbeats); link `DOWN` status does not leak directly to detector until periodic probes accumulate. Unprobed links remain marked `None` (unknown).
3. **Consistent Millisecond Latency:** Packet latency and summary link stats are converted to milliseconds (`ms = s * 1000.0`) at the boundary.
4. **Deterministic Reroute Avoidance:** Dijkstra path search excludes failed or degraded components.
5. **Distinct Recovery Service Limits & Duplicate-ID Rejection:** Recovery requires distinct post-action packets processed after the action boundary on the validated route meeting latency limits (<= 50.0 ms). Replaying identical packet IDs five times does NOT recover. Packets with missing latency are rejected.
6. **No Alternate Path Unresolved:** When all egress paths are blocked, engine correctly transitions to and remains in `Unresolved`.
7. **Labelled Fallback on AI Timeout / Malformed Output:** Malformed JSON or unknown components immediately yield an explicitly labelled deterministic rule fallback (`model_name="Deterministic Rule Fallback"`, `is_fallback=True`).
8. **Epoch Invalidation on Reset:** Resetting advances client epoch and invalidates pending in-flight AI responses.
9. **Subsequent Faults Create New Incidents:** Additional faults injected after recovery trigger new incident lifecycles (`Warning`/`Detected` -> `Rerouting`).
10. **Strict Baseline Preservation when Auto-Heal is OFF:** Static route is preserved without silent rerouting.
11. **Aligned A/B Benchmarks with N/A Representation:** Post-action observation windows for Auto-Heal ON and OFF are strictly aligned on identical packet counts; empty observation windows report `None` (`"N/A"` in CSV).
12. **Exact Workload Enforcement in A/B Benchmark:** `offered_packets` is strictly enforced and validated (minimum >= 15); unsupported sizes are explicitly rejected with `ValueError`.
13. **Deterministic Diagnosis Provider Injection:** A/B benchmarks inject a deterministic diagnosis provider (e.g. `Deterministic Rule Fallback` or custom callable), avoiding reliance on live network/inference timing, and record provider identity in reports.
14. **Zero-Delivery Latency None/N/A:** Phases with 0 delivered packets record average latency as `None` (`"N/A"`), cleanly propagating to deltas and CSV exports without assuming 0.0ms.

### Summary Comparison (from `reroute_comparison_report.csv`):
- **Scenario:** Sudden Link Failure (`R2-R4`)
- **Diagnosis Provider:** Deterministic Rule Fallback
- **Auto-Heal OFF:** Post-Action PDR: **0.0%**, Post-Action Latency: **N/A**, Status: *Action selected / Unrecovered* ("not recovered within window").
- **Auto-Heal ON:** Post-Action PDR: **94.1%**, Post-Action Latency: **25.00ms**, Status: *Recovered*, Recovery Time: **2.32s** (simulated).
- **Net Improvement:** **+94.1% PDR recovery** via deterministic rerouting; latency delta: **N/A** (due to 0 deliveries on OFF baseline).

---

## 4. Setup & Running Instructions

### Environment Variables
Store in `.env` or set in shell (values not hardcoded in repo):
```bash
QUALCOMM_AI_ENDPOINT=https://aisuite.cirrascale.com/apis/v2
QUALCOMM_AI_KEY=<your_account_api_key>
QUALCOMM_AI_MODEL=Llama-3.1-8B
```

### Running the Server
```powershell
# From the ReRoute repository root
.venv\Scripts\python.exe netadapt/lab_server.py --port 8000
```
Open browser at: `http://localhost:8000`

### Running Tests
```powershell
.venv\Scripts\python.exe -X utf8 -m pytest -q
```

---

## 5. Three-Minute Demo Script

1. **Minute 1: Baseline Network Operation**
   - Click the **★ ReRoute AI** button in the header toolbar.
   - Note the 6-router redundant topology loaded on screen.
   - Click **▶ Start Flow** to send video packets from `Client` to `Server`.
   - Observe live packets moving through primary route `Client -> R1 -> R2 -> R4 -> R6 -> Server` with ~100% PDR and low latency (~19ms). Incident status displays `Healthy`.

2. **Minute 2: Fault Injection & Qualcomm Cloud AI Diagnosis**
   - With Auto-Heal set to **ON**, select scenario **Sudden Link Failure (R2-R4)** and click **⚡ Trigger Fault**.
   - Watch the incident lifecycle advance: `Warning` ➔ `Detected` ➔ `Diagnosing`.
   - The **Qualcomm Cloud AI 100 Diagnosis** panel populates:
     - Model: `Llama-3.1-8B`
     - Likely Cause: Link severed / high packet loss
     - Affected Component: `R2-R4`
     - Action Selected: `REROUTE_PATH`
   - Observe engine rerouting: `Client -> R1 -> R2 -> R5 -> R6 -> Server`.
   - Packets resume delivery; after 5 consecutive successful packets in the verification window, the badge turns green: `Recovered`.

3. **Minute 3: A/B Comparison & Auto-Heal OFF Baseline**
   - Click **↻ Reset** and toggle Auto-Heal to **Set OFF**.
   - Click **⚡ Trigger Fault**. Observe that without auto-healing, packets continue to hit the severed link and drop (PDR drops to 0%).
   - Click **📊 Run Repeatable A/B Comparison (M5)**.
   - The system executes the exact same workload with isolated RNG seeds and outputs the side-by-side metrics:
     - Auto-Heal OFF Post-Action PDR: 0.0%
     - Auto-Heal ON Post-Action PDR: 94.4%
     - Demonstrating repeatable self-healing verification.

---

## 6. One-Way Telegram Notification Module
- **Segregation & Concurrency:** Runs completely asynchronously via `TelegramNotifier` on a single daemon background thread with a bounded queue. Never acquires the simulation lock or blocks packet progression.
- **Alert Transitions:** Transmits plain text notifications only on `Detected`, verified `Recovered`, and `Unresolved` lifecycle transitions.
- **Message Content:** Every alert includes `🚨 [SIMULATED NETWORK] Incident Alert`, incident ID, run ID, timestamp, affected component, and observed evidence. Recovery alerts include the active bypass route and simulated recovery duration.
- **Deduplication & Hygiene:** Deduplicates by `(run_id, incident_id, event_type)`. Old queue entries are purged upon simulation reset. Bot tokens are strictly redacted from logs and error handlers.
- **Setup Helper:** `telegram_setup.py` provides `--get-chat-id` (listing chats from getUpdates without selecting one automatically or printing message contents) and `--send-test` (sending a single verified test alert).
- **Test Isolation:** Automatically disabled in comparison benchmarks and test suites (`notifier.enabled = False`).

---

## 7. Known Limitations
- Rerouting operates within the simulated network graph; physical link repairs require explicit topology reset or administrative manual interface recovery.
- In `no_alternate_path` scenarios where both upper and lower paths are simultaneously severed, the engine correctly reports `Unresolved`, as no physical alternative exists.
- In offline environments without network connectivity to Cirrascale, the engine automatically and seamlessly transitions to the local rule-based diagnostic fallback.
- Telegram notifications are strictly one-way alerts; no inbound commands, webhooks, or remote execution capabilities are supported.
