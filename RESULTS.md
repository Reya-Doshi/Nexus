# NC-04 base repository evaluation

Date: 2026-10-12. Recommendation: NetAdapt, https://github.com/akashgoudsidduluri/adaptive-self-healing-network-routing

Four repositories were cloned and inspected. Repository implementation code was not repaired or modified for this evaluation. Dependencies were installed locally. This is a focused suitability evaluation, not a security audit or an exhaustive browser interaction test.

## Selected repository: NetAdapt

Commit: 5125a31e0499bee4a8dc44362ffe4b1f3b491b89

- Full supplied suite on Windows/Python 3.12: 308 passed, one CSV text-encoding assertion failed with the default Windows encoding.
- Full suite with Python UTF-8 mode: **309 passed in 4.75 seconds**. See netadapt-tests-utf8.log and netadapt-tests-utf8.xml.
- Secondary Streamlit app initial render: PASS via Streamlit AppTest. Deprecation warnings were emitted.
- Independent primary HTTP API smoke test: PASS. Created a redundant six-device network, sent traffic, failed R2, advanced simulation through detection, and sent five further packets.
- Before: PC1 -> R1 -> R2 -> R4 -> PC2.
- After: PC1 -> R1 -> R3 -> R4 -> PC2. All five subsequent packets delivered; failed router excluded; FAILURE_DETECTED event present.
- After also failing R3, traffic was correctly dropped rather than falsely reported recovered.
- Test harness initially failed an endpoint gateway in the default topology; traffic correctly became unreachable. The subsequent positive recovery check explicitly constructed a redundant topology. Not every router failure in every topology is recoverable.
- No Qualcomm AI integration was added or tested. Predictive warning and evidence-based AI diagnosis remain submission work.
- No standalone license file found; README license section only says educational project. Reuse permission and competition rules remain to be checked.

## Other candidates checked

### AutoMesh Learning

Commit: f9eab209f83c2e803bd6f56720c09ea0824b4b0f

Evaluated the main Next.js application in 10-final-outcome/mesh-network-simulator, not every educational example in this collection.

- Dependencies installed successfully.
- Production build FAILED: required @/lib/utils module is absent (five reported build errors).
- Standalone TypeScript check also reported missing-module and other errors; details in automesh-types.log. Build configuration suppresses TypeScript build errors, so a successful build alone would not establish type correctness.
- Code inspection: components/mesh-network-simulator.tsx declares healing complete on a fixed 4000ms timer without checking successfulReroutes. This is a weakness for verified-recovery claims.

### Network Router Simulator

Commit: ba8a59c917fd36d3a3bc9fc45f41f86d4354b14c

- No supplied automated test suite found.
- Independent routing checks PASS: shortest path, failed-link bypass, and disconnected destination.
- Streamlit main.py initial render PASS.
- Smaller routing demonstration; substantially more detection, diagnosis, and recovery-validation work remains than in NetAdapt.

### SRE Sentinel

Commit: c3332393da7658a2b1fc9067e0263f033da1e0bc

- Frontend npm ci succeeded; production build including TypeScript succeeded (bundle-size warning).
- Python backend syntax compilation succeeded.
- No supplied automated test suite found.
- Full backend/healing NOT tested: Docker was not available on PATH, and provider credentials were not supplied. Code expects OPENROUTER_API_KEY, despite older provider descriptions in the README.
- Focuses on container operations; network topology simulation and packet rerouting would need additional implementation.

## Reproduce selected checks

From the netadapt directory:

```powershell
..\.venv\Scripts\python.exe -X utf8 -m pytest -q
..\.venv\Scripts\python.exe -X utf8 lab_server.py --host 127.0.0.1 --port 8765
```

From repo-comparison:

```powershell
.\.venv\Scripts\python.exe smoke_checks.py netadapt
```

The selected foundation has the strongest tested fit among these four for the one-day NC-04 task. No winning probability can be inferred from these checks.
