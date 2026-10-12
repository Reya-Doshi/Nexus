const SVG_NS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);
const canvas = $('networkCanvas');
const linkLayer = $('linkLayer');
const routeLayer = $('routeLayer');
const nodeLayer = $('nodeLayer');
const packetLayer = $('packetLayer');
const connectPreview = $('connectPreview');

let state = null;
let mode = 'select';
let selected = null;
let connectStart = null;
let drag = null;
let seenPacketIds = new Set();
let playTimer = null;
let zoom = 1;
let origin = { x: 0, y: 0 };
let isInjecting = false; // Debounce flag to guarantee exactly once execution

const TYPES = {pc:'PC',laptop:'Laptop',server:'Server',printer:'Printer',router:'Router',switch:'Switch',hub:'Hub',access_point:'Access Point',cloud:'Cloud',internet:'Internet'};
const TYPE_CLASS = {pc:'host',laptop:'host',server:'server',printer:'host',router:'router',switch:'switch',hub:'switch',access_point:'switch',cloud:'cloud',internet:'cloud'};
const COLORS = {Emergency:'#f16c7a',VoIP:'#56d9e8',Video:'#bb9cff',HTTP:'#5ca9ff',FTP:'#f3bf5b'};

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function key(a, b) { return [a,b].sort().join('--'); }
function fmt(n, digits=0) { return Number(n || 0).toFixed(digits); }
function clock(seconds) { const s=Math.max(0,Number(seconds)||0); return `${String(Math.floor(s/60)).padStart(2,'0')}:${(s%60).toFixed(1).padStart(4,'0')}`; }
function toast(message, error=false) {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = 'toast'; }, 2800);
}

async function api(action, payload={}) {
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, ...payload})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Backend request failed');
    if (data.state && Array.isArray(data.state.devices)) {
      state = data.state;
      render();
    }
    return data;
  } catch (error) {
    toast(error.message, true);
    throw error;
  }
}

async function loadState() {
  const response = await fetch('/api/state');
  state = await response.json();
  // Ensure reroute demo is initialized if not already loaded with Client & Server
  if (!state?.devices?.some(d => d.id === 'Client')) {
    await api('reroute_init');
  } else {
    render();
  }
}

function icon(type) {
  if (type === 'router') return '<circle class="device-glyph" cx="0" cy="-2" r="10"/><path class="device-glyph" d="M-10 10h20M-5 6l5 4 5-4M-5 14l5-4 5 4"/>';
  if (type === 'server') return '<rect class="device-glyph" x="-13" y="-17" width="26" height="34" rx="2"/><path class="device-glyph" d="M-8-9H8M-8 0H8M-8 9H8"/><circle cx="7" cy="-9" r="1.5" fill="#5ee59f"/><circle cx="7" cy="0" r="1.5" fill="#54d8e8"/>';
  return '<rect class="device-glyph" x="-15" y="-14" width="30" height="23" rx="2"/><path class="device-glyph" d="M-8 16h16M-4 9l4 4 4-4"/>';
}

function nodeMarkup(device) {
  const down = device.status === 'DOWN';
  return `<g class="node ${TYPE_CLASS[device.type]||'host'} ${down?'down':''} ${selected?.kind==='device'&&selected.id===device.id?'selected':''}" data-id="${esc(device.id)}" transform="translate(${device.x},${device.y})">
    <rect class="selection" x="-34" y="-38" width="68" height="82" rx="7"/>
    <rect class="device-body" x="-30" y="-33" width="60" height="60" rx="8"/>
    ${icon(device.type)}
    <circle class="port" cx="-25" cy="30" r="2.5"/>
    <circle class="port" cx="25" cy="30" r="2.5"/>
    <text class="node-label" y="45">${esc(device.name)}</text>
    <text class="node-type" y="57">${esc(TYPES[device.type]||device.type)}</text>
    <text class="node-status ${down?'down':'up'}" y="68">${down?'DOWN':'ONLINE'}</text>
  </g>`;
}

function routeEdges() {
  const edges = new Set();
  const currentRoute = state?.reroute?.current_route;
  if (Array.isArray(currentRoute) && currentRoute.length > 1) {
    for (let i = 0; i < currentRoute.length - 1; i++) {
      edges.add(key(currentRoute[i], currentRoute[i+1]));
    }
  } else {
    for (const flow of state?.flows || []) {
      if (Array.isArray(flow.route) && flow.route.length > 1) {
        for (let i = 0; i < flow.route.length - 1; i++) {
          edges.add(key(flow.route[i], flow.route[i+1]));
        }
      }
    }
  }
  return edges;
}

function linkMarkup(link, active) {
  const source = state.devices.find(d => d.id === link.source);
  const target = state.devices.find(d => d.id === link.target);
  if (!source || !target) return '';
  const mx = (source.x + target.x) / 2;
  const my = (source.y + target.y) / 2;

  // Check if link is failed in topology or in reroute failed_components list
  const linkKey = `${link.source}-${link.target}`;
  const revKey = `${link.target}-${link.source}`;
  const failedComps = state?.reroute?.failed_components || [];
  const isFailedInEngine = failedComps.includes(linkKey) || failedComps.includes(revKey);
  const isDown = link.status === 'DOWN' || isFailedInEngine;

  let cls = 'healthy';
  if (isDown) cls = 'failed';
  else if (active) cls = 'active';
  else if (link.congestion > 0.3) cls = 'warning';

  return `<g data-link="${esc(link.id)}" data-source="${esc(link.source)}" data-target="${esc(link.target)}">
    <line class="link hit" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/>
    <line class="link ${cls}" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/>
    <g class="link-label ${active ? 'active' : ''}" transform="translate(${mx},${my})">
      <rect x="-30" y="-9" width="60" height="18"/>
      <text y="0">${fmt(link.bandwidth)}M · ${Math.round(link.latency)}ms</text>
    </g>
  </g>`;
}

function renderCanvas() {
  if (!state || !state.devices) return;
  const edges = routeEdges();
  linkLayer.innerHTML = (state.links || []).map(l => linkMarkup(l, edges.has(key(l.source, l.target)))).join('');
  nodeLayer.innerHTML = (state.devices || []).map(nodeMarkup).join('');
  const empty = $('canvasEmpty');
  if (empty) empty.style.display = state.devices?.length ? 'none' : 'grid';

  const packets = state.packets || [];
  if (!packets.length) {
    seenPacketIds.clear();
    packetLayer.innerHTML = '';
    routeLayer.innerHTML = '';
  }
  packets.forEach(packet => {
    const packetKey = String(packet.id);
    if (!seenPacketIds.has(packetKey)) {
      seenPacketIds.add(packetKey);
      animatePacket(packet);
    }
  });
}

function animatePacket(packet) {
  const route = packet.route || state?.reroute?.current_route || [];
  const sourceDevice = state.devices.find(d => d.id === packet.source);
  if (!sourceDevice) return;

  if (packet.status === 'DROPPED' || route.length < 2) {
    const markerId = `packetDrop-${packet.id}-${Date.now()}`;
    const reason = packet.drop_reason || packet.reason || (route.length < 2 ? 'NO_ROUTE' : 'DROPPED');
    packetLayer.insertAdjacentHTML('beforeend',
      `<g id="${markerId}" class="packet dropped" data-packet="${packet.id}" style="color:#f26674" transform="translate(${sourceDevice.x},${sourceDevice.y})">
        <circle class="packet-ring" r="8"/>
        <circle class="packet-core" r="3"/>
        <text class="packet-label" y="-13">P${packet.id} ${esc(reason)}</text>
      </g>`
    );
    setTimeout(() => { const el = document.getElementById(markerId); if (el) el.remove(); }, 2400);
    return;
  }

  const points = route.map(id => state.devices.find(d => d.id === id)).filter(Boolean);
  if (points.length < 2) return;

  const id = `packetPath-${packet.id}-${Date.now()}`;
  const d = points.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' ');
  const color = packet.status === 'DROPPED' ? '#f26674' : '#54d8e8';

  packetLayer.insertAdjacentHTML('beforeend',
    `<path id="${id}" d="${d}" fill="none" stroke="none"/>
     <g class="packet" data-packet="${packet.id}" style="color:${color}">
       <circle class="packet-ring" r="7"/>
       <circle class="packet-core" r="2.5"/>
       <text class="packet-label" y="-12">P${packet.id}</text>
       <animateMotion dur="${Math.max(800, route.length * 360)}ms" repeatCount="1" fill="freeze">
         <mpath href="#${id}"/>
       </animateMotion>
     </g>`
  );

  setTimeout(() => {
    const el = document.getElementById(id);
    if (el) el.remove();
  }, Math.max(900, route.length * 360) + 300);
}

function updateStatusSentence(rerouteState, isRunning) {
  const statusDot = $('statusIndicatorDot');
  const statusText = $('statusText');
  if (!statusText || !statusDot) return;

  const incident = rerouteState?.incident_state || 'Healthy';
  const autoHeal = rerouteState?.auto_heal_enabled !== false;
  const currentRoute = rerouteState?.current_route || [];
  const routeStr = currentRoute.length ? currentRoute.join(' ➔ ') : 'Primary Route';
  const failed = rerouteState?.failed_components || [];

  let text = '';
  let dotColor = '#5ee59f';

  if (!isRunning && (!state.flows || !state.flows.some(f => f.status === 'ACTIVE'))) {
    text = 'Simulation paused. Click "Start Traffic" to send packets through the network.';
    dotColor = '#8ea5bb';
  } else if (incident === 'Healthy') {
    text = `Network healthy. Traffic actively streaming along ${routeStr} with 100% delivery.`;
    dotColor = '#5ee59f';
  } else if (incident === 'Warning') {
    text = `Telemetry anomaly detected on ${failed.join(', ') || 'trunk link'}. Evaluating persistence...`;
    dotColor = '#f2bd57';
  } else if (incident === 'Detected') {
    text = `Fault verified on ${failed.join(', ') || 'link'}. Engaging non-blocking AI diagnostic engine...`;
    dotColor = '#f26674';
  } else if (incident === 'Diagnosing') {
    text = `Inference underway. Qualcomm AI analyzing link metrics and generating recommendation...`;
    dotColor = '#b998ff';
  } else if (incident === 'Action selected') {
    if (!autoHeal) {
      text = `Diagnosis complete. Auto-Heal is OFF: baseline routing preserved, packets dropping on degraded path.`;
      dotColor = '#f2874e';
    } else {
      text = `Action approved: Rerouting traffic to deterministic alternative bypass route.`;
      dotColor = '#4d9eff';
    }
  } else if (incident === 'Rerouting') {
    text = `Applying bypass path (${routeStr}). Updating flow static route tables...`;
    dotColor = '#54d8e8';
  } else if (incident === 'Verifying') {
    text = `Traffic switched to bypass route (${routeStr}). Verifying 5 consecutive successful packet deliveries...`;
    dotColor = '#f2bd57';
  } else if (incident === 'Recovered') {
    const dur = rerouteState?.metrics?.recovery_time_sim_time;
    text = `Recovery verified! 5 consecutive packets delivered on bypass route${dur != null ? ` in ${dur.toFixed(2)}s` : ''}. Service restored.`;
    dotColor = '#5ee59f';
  } else if (incident === 'Unresolved') {
    text = `Unresolved: All alternate paths exhausted or severed. Traffic dropping permanently on blocked routes.`;
    dotColor = '#f26674';
  }

  statusText.textContent = text;
  statusDot.style.background = dotColor;
  statusDot.style.boxShadow = `0 0 8px ${dotColor}`;
}

function updatePipelineSteps(rerouteState) {
  const incident = rerouteState?.incident_state || 'Healthy';
  const stages = ['Healthy', 'Detected', 'Diagnosing', 'Rerouting', 'Verifying', 'Final'];

  // Map backend state to pipeline index
  let activeIndex = 0;
  if (incident === 'Healthy') activeIndex = 0;
  else if (incident === 'Warning' || incident === 'Detected') activeIndex = 1;
  else if (incident === 'Diagnosing') activeIndex = 2;
  else if (incident === 'Action selected' || incident === 'Rerouting') activeIndex = 3;
  else if (incident === 'Verifying') activeIndex = 4;
  else if (incident === 'Recovered' || incident === 'Unresolved') activeIndex = 5;

  const stageEls = [
    $('stage-healthy'),
    $('stage-detected'),
    $('stage-diagnosing'),
    $('stage-rerouting'),
    $('stage-verifying'),
    $('stage-final')
  ];

  stageEls.forEach((el, idx) => {
    if (!el) return;
    el.classList.remove('active-step', 'passed-step');
    if (idx < activeIndex) {
      el.classList.add('passed-step');
    } else if (idx === activeIndex) {
      el.classList.add('active-step');
    }
  });

  const finalLabel = $('finalStageLabel');
  const recoveryLabel = $('recoveryTimeLabel');
  if (finalLabel && recoveryLabel) {
    if (incident === 'Recovered') {
      finalLabel.textContent = 'Recovered ✓';
      finalLabel.style.color = 'var(--green)';
      const dur = rerouteState?.metrics?.recovery_time_sim_time;
      recoveryLabel.textContent = dur != null ? `Verified in ${dur.toFixed(2)}s` : 'Service restored';
    } else if (incident === 'Unresolved') {
      finalLabel.textContent = 'Unresolved ✗';
      finalLabel.style.color = 'var(--red)';
      recoveryLabel.textContent = 'No alternate route exists';
    } else {
      finalLabel.textContent = 'Recovered / Unresolved';
      finalLabel.style.color = 'var(--text)';
      recoveryLabel.textContent = 'Awaiting verification';
    }
  }

  const detectionDelayLabel = $('detectionDelayLabel');
  if (detectionDelayLabel) {
    const delay = rerouteState?.metrics?.detection_delay_sim_time;
    detectionDelayLabel.textContent = delay != null ? `Identified in ${delay.toFixed(2)}s` : 'Anomaly identified';
  }
}

function updateDiagnosisCard(diag) {
  const providerBadge = $('providerBadge');
  const providerName = $('diagProviderName');
  const inferenceTime = $('diagInferenceTime');
  const likelyCause = $('diagLikelyCause');
  const affectedComp = $('diagAffectedComponent');
  const diagAction = $('diagAction');
  const explanation = $('diagExplanation');

  if (!diag) {
    if (providerBadge) {
      providerBadge.className = 'provider-badge provider-fallback';
      providerBadge.textContent = 'RULE FALLBACK';
    }
    if (providerName) providerName.textContent = 'Deterministic Fallback';
    if (inferenceTime) inferenceTime.textContent = '—';
    if (likelyCause) likelyCause.textContent = 'Awaiting incident';
    if (affectedComp) affectedComp.textContent = 'None';
    if (diagAction) diagAction.textContent = 'None';
    if (explanation) explanation.textContent = 'System running baseline routing policy. Telemetry normal.';
    return;
  }

  const isFallback = diag.is_fallback !== false;
  if (providerBadge) {
    providerBadge.className = `provider-badge ${isFallback ? 'provider-fallback' : 'provider-live'}`;
    providerBadge.textContent = isFallback ? 'RULE FALLBACK' : 'LIVE QUALCOMM AI 100';
  }
  if (providerName) providerName.textContent = diag.model_name || (isFallback ? 'Deterministic Fallback' : 'Llama-3.1-8B (Cloud AI 100)');
  if (inferenceTime) inferenceTime.textContent = diag.wall_clock_ms != null ? `${fmt(diag.wall_clock_ms, 1)} ms` : '—';
  if (likelyCause) likelyCause.textContent = diag.likely_cause || 'Awaiting incident';
  if (affectedComp) affectedComp.textContent = diag.affected_component || 'None';
  if (diagAction) diagAction.textContent = diag.recommended_action || 'None';
  if (explanation) explanation.textContent = diag.explanation || 'Diagnosis completed.';
}

function updateBottomMetrics(rerouteState) {
  const pdrEl = $('metricPdr');
  const packetsEl = $('metricPackets');
  const latEl = $('metricLatency');
  const recEl = $('metricRecoveryDuration');
  const routeEl = $('metricActiveRoute');

  const pdr = rerouteState?.telemetry?.recent_delivery_rate_pct ?? state?.metrics?.pdr ?? 100.0;
  const sent = Math.round(state?.metrics?.sent || 0);
  const delivered = Math.round(state?.metrics?.delivered || 0);
  const lat = rerouteState?.telemetry?.recent_avg_latency_ms ?? state?.metrics?.latency ?? 19.0;
  const recoveryDur = rerouteState?.metrics?.recovery_time_sim_time;
  const route = rerouteState?.current_route || ['Client', 'R1', 'R2', 'R4', 'R6', 'Server'];

  if (pdrEl) {
    pdrEl.textContent = `${fmt(pdr, 1)}%`;
    pdrEl.className = `metric-value ${pdr < 85 ? 'pdr-bad' : 'metric-good'}`;
  }
  if (packetsEl) packetsEl.textContent = `${delivered} delivered / ${sent} sent`;
  if (latEl) latEl.textContent = `${fmt(lat, 1)} ms`;
  if (recEl) recEl.textContent = recoveryDur != null ? `${recoveryDur.toFixed(2)}s` : '—';
  if (routeEl) routeEl.textContent = `Route: ${route.join(' ➔ ')}`;

  // Update Compact Incident Timeline
  const timeline = rerouteState?.incident_timeline || [];
  const listEl = $('compactTimelineList');
  const countEl = $('timelineCount');
  if (countEl) countEl.textContent = `${timeline.length} event${timeline.length === 1 ? '' : 's'}`;

  if (listEl) {
    if (!timeline.length) {
      listEl.innerHTML = '<div class="timeline-empty-msg">No incident events yet. Start traffic or inject a fault.</div>';
    } else {
      listEl.innerHTML = timeline.slice().reverse().map(e => `
        <div class="compact-event-row">
          <span class="event-time">${fmt(e.sim_time, 2)}s</span>
          <b class="event-state">[${esc(e.state)}]</b>
          <span class="event-msg">${esc(e.message)}</span>
        </div>
      `).join('');
    }
  }
}

function updateAutoHealButton(autoHeal) {
  const btn = $('autoHealToggleBtn');
  if (!btn) return;
  if (autoHeal) {
    btn.textContent = 'Auto-Heal: ON';
    btn.style.background = '#0f2c25';
    btn.style.borderColor = '#206d58';
    btn.style.color = '#79f0ba';
  } else {
    btn.textContent = 'Auto-Heal: OFF';
    btn.style.background = '#35181e';
    btn.style.borderColor = '#722b37';
    btn.style.color = '#ff9faa';
  }
}

function render() {
  if (!state) return;
  const r = state.reroute || {};
  const autoHeal = r.auto_heal_enabled !== false;
  const isRunning = Boolean(state.running);

  renderCanvas();
  updateStatusSentence(r, isRunning);
  updatePipelineSteps(r);
  updateDiagnosisCard(r.diagnosis);
  updateBottomMetrics(r);
  updateAutoHealButton(autoHeal);

  // Update Play/Pause Button Visual State
  const startBtn = $('mainStartBtn');
  const pauseBtn = $('mainPauseBtn');
  if (startBtn && pauseBtn) {
    if (isRunning) {
      startBtn.classList.remove('primary-btn');
      startBtn.classList.add('action-btn');
      startBtn.textContent = '▶ Running';
      pauseBtn.classList.add('primary-btn');
      pauseBtn.classList.remove('action-btn');
    } else {
      startBtn.classList.add('primary-btn');
      startBtn.classList.remove('action-btn');
      startBtn.textContent = '▶ Start Traffic';
      pauseBtn.classList.remove('primary-btn');
      pauseBtn.classList.add('action-btn');
    }
  }
}

async function play() {
  stop();
  playTimer = setInterval(async () => {
    if (!state?.running) {
      stop();
      return;
    }
    try {
      await api('reroute_step');
    } catch (e) {
      stop();
    }
  }, 250);
}

function stop() {
  if (playTimer) {
    clearInterval(playTimer);
    playTimer = null;
  }
}

// -----------------------------------------------------------------------------
// Interactive Control Handlers
// -----------------------------------------------------------------------------

function bindControls() {
  // Scenario persistence via localStorage
  const scenarioSelect = $('scenarioSelect');
  if (scenarioSelect) {
    const savedScenario = localStorage.getItem('reroute_selected_scenario');
    if (savedScenario && Array.from(scenarioSelect.options).some(o => o.value === savedScenario)) {
      scenarioSelect.value = savedScenario;
    }
    scenarioSelect.addEventListener('change', () => {
      localStorage.setItem('reroute_selected_scenario', scenarioSelect.value);
    });
  }

  // 1. Start Traffic / Resume
  $('mainStartBtn')?.addEventListener('click', async () => {
    try {
      // If there are no active flows, create one cleanly
      const hasActiveFlow = state?.flows && state.flows.some(f => f.status === 'ACTIVE');
      if (!hasActiveFlow) {
        await api('start_traffic', {
          source: 'Client',
          destination: 'Server',
          packet_count: 60,
          pps: 5.0,
          traffic_type: 'Video',
          packet_size: 1200
        });
      }
      await api('running', {value: true});
      play();
      toast('Traffic streaming Client ➔ Server');
    } catch (e) {
      toast(e.message, true);
    }
  });

  // 2. Pause
  $('mainPauseBtn')?.addEventListener('click', async () => {
    try {
      stop();
      await api('running', {value: false});
      toast('Simulation paused');
    } catch (e) {
      toast(e.message, true);
    }
  });

  // 3. Reset
  $('mainResetBtn')?.addEventListener('click', async () => {
    try {
      stop();
      await api('reroute_reset');
      toast('Network reset to initial baseline');
    } catch (e) {
      toast(e.message, true);
    }
  });

  // 5. Inject Fault (Debounced to execute exactly once)
  $('injectFaultBtn')?.addEventListener('click', async () => {
    if (isInjecting) return;
    isInjecting = true;
    const btn = $('injectFaultBtn');
    if (btn) btn.disabled = true;

    try {
      const scn = scenarioSelect ? scenarioSelect.value : 'sudden_link_failure';
      await api('reroute_inject_scenario', {scenario: scn});
      toast(`Fault injected: ${scn}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      setTimeout(() => {
        isInjecting = false;
        if (btn) btn.disabled = false;
      }, 500);
    }
  });

  // 6. Auto-Heal Toggle
  $('autoHealToggleBtn')?.addEventListener('click', async () => {
    try {
      const current = state?.reroute?.auto_heal_enabled !== false;
      await api('reroute_set_auto_heal', {value: !current});
      toast(`Auto-Heal set to ${!current ? 'ON' : 'OFF'}`);
    } catch (e) {
      toast(e.message, true);
    }
  });

  // 7. A/B Comparison Modal Panel
  $('openComparisonBtn')?.addEventListener('click', () => {
    runComparisonBenchmark();
  });

  $('closeComparisonBtn')?.addEventListener('click', () => {
    const modal = $('comparisonModal');
    if (modal) modal.style.display = 'none';
  });

  $('modalCloseActionBtn')?.addEventListener('click', () => {
    const modal = $('comparisonModal');
    if (modal) modal.style.display = 'none';
  });

  $('modalRerunBtn')?.addEventListener('click', () => {
    runComparisonBenchmark();
  });

  // Advanced Drawer
  $('toggleAdvancedBtn')?.addEventListener('click', () => {
    const drawer = $('advancedDrawer');
    if (drawer) drawer.style.display = drawer.style.display === 'none' ? 'flex' : 'none';
  });

  $('closeAdvancedBtn')?.addEventListener('click', () => {
    const drawer = $('advancedDrawer');
    if (drawer) drawer.style.display = 'none';
  });

  document.querySelectorAll('[data-legacy]').forEach(btn => {
    btn.addEventListener('click', () => {
      toast(`Legacy module "${btn.dataset.legacy}" is preserved in advanced settings.`);
    });
  });
}

async function runComparisonBenchmark() {
  const modal = $('comparisonModal');
  const loading = $('comparisonLoading');
  const results = $('comparisonResults');
  const summary = $('comparisonSummaryBox');
  if (modal) modal.style.display = 'flex';
  if (loading) loading.style.display = 'flex';
  if (results) results.style.display = 'none';
  if (summary) summary.style.display = 'none';

  try {
    const scn = $('scenarioSelect')?.value || 'sudden_link_failure';
    const resp = await api('reroute_run_comparison', {scenario: scn, offered_packets: 30, seed: 42});
    const report = resp.state.report_dict;

    const formatLat = (val) => {
      if (val === null || val === undefined || val === 'N/A') return 'N/A';
      const num = Number(val);
      return isNaN(num) ? 'N/A' : `${num.toFixed(1)} ms`;
    };
    const formatDur = (val) => {
      if (val === null || val === undefined || val === 'N/A') return 'N/A';
      const num = Number(val);
      return isNaN(num) ? 'N/A' : `${num.toFixed(2)}s`;
    };

    // Populate Modal Content
    $('compOffPdr').textContent = `${Number(report.auto_heal_off.post_action.delivery_rate_pct || 0).toFixed(1)}%`;
    $('compOffLatency').textContent = formatLat(report.auto_heal_off.post_action.avg_latency_ms);

    $('compOnPdr').textContent = `${Number(report.auto_heal_on.post_action.delivery_rate_pct || 0).toFixed(1)}%`;
    $('compOnLatency').textContent = formatLat(report.auto_heal_on.post_action.avg_latency_ms);
    $('compOnRecovery').textContent = formatDur(report.auto_heal_on.recovery_time_sim_s);

    const delta = report.summary_comparison.post_action_pdr_improvement_pct;
    $('compPdrDelta').textContent = delta != null && !isNaN(Number(delta)) ? `+${Number(delta).toFixed(1)}%` : 'N/A';
    $('compProviderName').textContent = report.diagnosis_provider || 'Deterministic Fallback';

    if (loading) loading.style.display = 'none';
    if (results) results.style.display = 'grid';
    if (summary) summary.style.display = 'flex';
  } catch (err) {
    if (loading) loading.style.display = 'none';
    toast(`Benchmark failed: ${err.message}`, true);
  }
}

// Initialize Application
window.addEventListener('DOMContentLoaded', () => {
  bindControls();
  loadState().catch(e => toast(e.message, true));
});
