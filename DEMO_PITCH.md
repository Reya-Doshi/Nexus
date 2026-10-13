# Nexus — Team Setu: 90-second demo pitch

## Before presenting
Open Simulator, verify the provider badge, and use Focus view. Save any custom topology. Keep the browser at 100% zoom. Start with Prevent impact demo; it loads a clearly labelled preventive lab and pauses on its result. Keep a successful incident export as backup evidence if the external AI service is unavailable.

## Say and show
**0–15 seconds — Problem**
“Network dashboards often tell us something failed after users feel it. Nexus is our safe simulation environment for detecting degradation, diagnosing its cause, choosing a bypass, and measuring whether service recovered.”

**15–45 seconds — Live demonstration**
Click **Prevent impact demo**.
“Watch this link degrade. We monitor packet delivery, latency and link health. The diagnosis panel identifies the provider and the measured evidence. The routing engine checks connectivity and the proposed bypass before applying it. We then verify delivered packets instead of declaring success immediately.”
Point to the failed/degraded link, highlighted bypass, and impact card. Read the actual result aloud: observed packets, drops, latency breaches and maximum latency. If recovery fails or fallback is active, explain the displayed result honestly.

**45–65 seconds — Evidence**
Open **Compare**, select preventive congestion, and run the comparison.
“This repeatable experiment uses the same topology, workload and seed with healing disabled and enabled. Look at packets exceeding 30 milliseconds, not just the final green status.”

**65–80 seconds — Safety and flexibility**
“Devices and links are editable. We tested a custom R7 bypass, a second failure, and repair. If all routes fail, Nexus reports unresolved. It cannot create connectivity that does not exist.”

**80–90 seconds — Close**
“Nexus makes detect–diagnose–heal observable and testable without touching production. Our result is a measured recovery loop with evidence you can inspect and export.”

## Judge questions
- **What does AI do?** Qualcomm-hosted Llama diagnoses observed per-link telemetry and recommends an allowed action. The backend corroborates attribution and validates execution. Unsupported responses or outages use clearly labelled deterministic fallback.
- **Why not rules alone?** Rules provide repeatable detection and safety. AI supplies an evidence-based interpretation. We have not established that AI outperforms rules on an independent labelled fault dataset; do not claim that.
- **Does it prevent every outage?** No. The preventive experiment demonstrates a bounded window with a usable standby path and gradual degradation. Sudden failures can lose packets; disconnected networks remain unresolved.
- **What ran on Snapdragon?** A separate AI Hub model profiling job reached the Snapdragon X Elite NPU. Its random image-shaped input benchmark does not establish that Nexus telemetry diagnosis runs on-device. Present that as hardware exploration, separately from the live cloud diagnosis.
- **What is the stack?** Python simulation and HTTP backend, graph-based routing, Qualcomm cloud Llama inference, and HTML/CSS/JavaScript with an SVG topology and telemetry dashboard. Optional Telegram alerts.
- **What is simulated?** Devices, traffic, faults, routing and recovery. This MVP does not change production network equipment.

## Presentation rule
Use the values on screen. Never describe 0.6 ms model profiling as the application's recovery time. Delivery restored, service target met, and no observed user impact are different claims.
