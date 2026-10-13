# Nexus — Team Setu demo

## Start

From the project directory, run `.venv\Scripts\python.exe netadapt/lab_server.py --host 127.0.0.1 --port 8000` and open http://localhost:8000. Restart an already running server after backend changes.

## Three-minute presentation

1. Open Simulator and choose **Reset to Recovery Demo**. This replaces the current topology; use **Save network** first to keep a custom network.
2. Choose **Sudden Link Failure**, keep **Auto-heal** on, and click **Run**. Point out simulated packet delivery and measured latency.
3. Click **Inject Fault**. Explain detection, diagnosis, bypass selection and the five-packet verification window. The failed link stays visible; the working bypass is highlighted.
4. Read the result precisely: delivery restoration and the 30 ms service target are separate. A slower bypass can restore delivery within the 50 ms recovery limit without meeting the stricter target.
5. Open **Incidents** to explain the actual evidence and diagnosis provider; export the report.
6. Open **Compare**, run the same fault with a fixed seed, and compare auto-heal on/off. This benchmark uses deterministic rules for repeatability; it is not a live AI speed benchmark.
7. Reset and demonstrate **No Alternate Path**. Nexus must say unresolved rather than invent a recovery.
8. Optionally enter **Build**, add/connect a device, select it to inspect real interfaces and traversing flows, then save the network.

## AI and alerts

The provider badge identifies live Qualcomm inference or rule fallback. Missing credentials or an unavailable endpoint trigger labelled fallback. Configure credentials locally; never include them in reports. Telegram alerts are optional; automated release checks keep them disabled.

## Verified on 13 October 2026

- 377 backend tests passed.
- Four scenarios across five seeds: 20 expected outcomes passed.
- Browser acceptance: themes without page loss, multi-flow route rendering, disconnected routes, four device types, connect/select, link creation, shutdown/start/delete, topology import/export, pause/resume, four recovery scenarios, incident export, comparison/export, reset and page navigation.
- One live Qualcomm Llama-3.1-8B diagnosis recovered successfully with fallback=false (about 2.13 seconds inference wall time).

## Reproduce checks

Backend: `.venv\Scripts\python.exe -m pytest -q netadapt -p no:cacheprovider`

Browser: start a disposable server on port 8017, then run `node test_nexus_release.cjs`. The browser suite replaces that server's topology. Override NEXUS_TEST_URL only with a disposable local instance. Keep TELEGRAM_ENABLED=false during testing.

## Scope

This is a simulated network MVP, not a production network controller. Sudden failures may drop packets; congestion detection cannot promise zero impact. Recovery requires a usable alternate path. External AI availability and latency vary.

## Preventive recovery demonstration

Click **Prevent impact demo**. This explicitly loads a separate lab with a 19 ms primary route and a 20 ms standby route, starts traffic, and applies slow congestion at scenario steps 2, 12 and 18. The demo pauses at verified recovery. Use **Focus view** for presentation.

The impact card measures every observed packet from fault onset to verified recovery. Earlier drops or breaches are never erased by successful recovery. This is a bounded measurement window, not a universal zero-impact guarantee.

In **Compare**, choose preventive congestion. Auto-heal ON and OFF use the same topology, workload and seed. Compare **Packets above 30 ms** as well as delivery rate.

Live Qualcomm check on 13 October 2026: Llama-3.1-8B, fallback=false, inference 2211 ms. Traffic advanced every 0.5 wall-clock seconds without waiting for diagnosis. Recovery completed with 13 observed packets, zero drops, zero latency breaches, maximum observed latency 28.5184 ms, and the primary SLO met.

AI receives observed per-link metrics. A model attribution unsupported by those metrics, or an unsupported action, is labelled as rule fallback. The executor retains graph and capacity checks. AI Hub Snapdragon image-model profiling remains separate from this cloud Llama integration.

## Final release checks

Custom R7 bypass, consecutive faults, complete disconnection and repair now have regression coverage. Browser checks cover exclusive tools, Escape cancellation and accurate device dragging at zoom. The complete acceptance suite passed with no page errors. See DEMO_PITCH.md for the 90-second pitch and judge questions.
