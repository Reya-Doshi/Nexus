const SVG_NS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);

// Canvas and layers
const canvas = $('networkCanvas');
const linkLayer = $('linkLayer');
const routeLayer = $('routeLayer');
const nodeLayer = $('nodeLayer');
const packetLayer = $('packetLayer');
const connectPreview = $('connectPreview');

// State
let state = null;
let mode = 'select'; // 'select' | 'connect' | 'delete'
let editorMode = 'build'; // 'build' | 'run'
let selected = null; // { kind: 'device'|'link', id: string, other?: string }
let inspectorTab = 'overview'; // 'overview' | 'routes' | 'events'
let connectStart = null;
let drag = null;
let seenPacketIds = new Set();
let playTimer = null;
let playbackEpoch = 0;
let pendingStep = Promise.resolve();
let zoom = 1.0;
let pan = { x: 0, y: 0 };
let isPanning = false;
let panStart = { x: 0, y: 0 };
let isInjecting = false; // Debounce flag
let lastComparison = null;
let guidedDemo = false;
let telemetryHistory = []; // Buffer of { time, pdr } for SVG sparkline
let latencyHistory = []; // Buffer of { time, lat } for SVG latency chart
let packetsHistory = []; // Buffer of { time, delivered, sent } for bar chart

const TYPES = {
  pc: 'PC',
  laptop: 'Laptop',
  server: 'Server',
  printer: 'Printer',
  router: 'Router',
  switch: 'Switch',
  hub: 'Hub',
  access_point: 'Access Point',
  cloud: 'Cloud',
  internet: 'Internet'
};

const TYPE_CLASS = {
  pc: 'host',
  laptop: 'host',
  server: 'server',
  printer: 'host',
  router: 'router',
  switch: 'switch',
  hub: 'switch',
  access_point: 'switch',
  cloud: 'cloud',
  internet: 'cloud'
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));
}

function key(a, b) {
  return [a, b].sort().join('--');
}

function fmt(n, digits = 0) {
  return Number(n || 0).toFixed(digits);
}

function clock(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(1).padStart(4, '0')}`;
}

function toast(message, error = false) {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.className = 'toast';
  }, 2800);
}

async function api(action, payload = {}) {
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ...payload })
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
  editorMode = state.running ? 'run' : 'build';
  render();
  if (state.running) play();
}

// -----------------------------------------------------------------------------
// Canvas Markup & Rendering
// -----------------------------------------------------------------------------

function icon(type) {
  if (type === 'router') {
    return `<circle class="device-glyph" cx="0" cy="-2" r="10"/>
      <path class="device-glyph" d="M-10 10h20M-5 6l5 4 5-4M-5 14l5-4 5 4"/>`;
  }
  if (type === 'server') {
    return `<rect class="device-glyph" x="-13" y="-17" width="26" height="34" rx="2"/>
      <path class="device-glyph" d="M-8-9H8M-8 0H8M-8 9H8"/>
      <circle cx="7" cy="-9" r="1.5" fill="#5ee59f"/>
      <circle cx="7" cy="0" r="1.5" fill="#54d8e8"/>`;
  }
  if (type === 'switch') {
    return `<rect class="device-glyph" x="-16" y="-10" width="32" height="20" rx="3"/>
      <path class="device-glyph" d="M-10-3h20M-10 3h20M-6-6l-4 3 4 3M6 0l4 3-4 3"/>`;
  }
  return `<rect class="device-glyph" x="-15" y="-14" width="30" height="23" rx="2"/>
    <path class="device-glyph" d="M-8 16h16M-4 9l4 4 4-4"/>`;
}

function nodeMarkup(device) {
  const down = device.status === 'DOWN';
  const isSel = selected?.kind === 'device' && selected.id === device.id;
  return `<g class="node ${TYPE_CLASS[device.type] || 'host'} ${down ? 'down' : ''} ${isSel ? 'selected' : ''}" data-id="${esc(device.id)}" transform="translate(${device.x},${device.y})">
    <rect class="selection" x="-34" y="-38" width="68" height="82" rx="10"/>
    <rect class="device-body" x="-30" y="-33" width="60" height="60" rx="10"/>
    ${icon(device.type)}
    <circle class="port" cx="-25" cy="30" r="2.5"/>
    <circle class="port" cx="25" cy="30" r="2.5"/>
    <text class="node-label" y="44">${esc(device.name)}</text>
    <text class="node-type" y="56">${esc(TYPES[device.type] || device.type)}</text>
    <text class="node-status ${down ? 'down' : 'up'}" y="67">${down ? '● Down' : '● Online'}</text>
  </g>`;
}

function routeIsConnected(route) {
  if (!Array.isArray(route) || route.length < 2) return false;
  return route.every(id => state.devices?.some(d => d.id === id && d.status !== 'DOWN')) && route.slice(1).every((id, i) => state.links?.some(l => ((l.source === route[i] && l.target === id) || (l.target === route[i] && l.source === id)) && l.status !== 'DOWN'));
}

function routeEdges() {
  const edges = new Set();
  const flows = (state?.flows || []).filter(f => f.status === 'ACTIVE');
  const routes = flows.length ? flows.map(f => f.route) :
    (usesRecoveryController() && state?.reroute?.incident_state !== 'Unresolved' ? [state?.reroute?.current_route] : []);
  for (const route of routes) {
    if (!routeIsConnected(route)) continue;
    for (let i = 0; i < route.length - 1; i++) edges.add(key(route[i], route[i + 1]));
  }
  return edges;
}

function linkMarkup(link, active) {
  const source = state.devices.find(d => d.id === link.source);
  const target = state.devices.find(d => d.id === link.target);
  if (!source || !target) return '';
  const mx = (source.x + target.x) / 2;
  const my = (source.y + target.y) / 2;

  const linkKey = `${link.source}-${link.target}`;
  const revKey = `${link.target}-${link.source}`;
  const failedComps = state?.reroute?.failed_components || [];
  const isFailedInEngine = failedComps.includes(linkKey) || failedComps.includes(revKey);
  const isDown = link.status === 'DOWN';
  const isDegraded = !isDown && (link.congestion >= 0.20 || link.packet_loss > 0);

  let cls = 'healthy';
  if (isDown) cls = 'failed';
  else if (isDegraded) cls = 'warning';
  else if (active) cls = 'active';

  const isSel = selected?.kind === 'link' && key(selected.id, selected.other) === key(link.source, link.target);

  const crossBadge = isDown ? `<g class="link-failed-cross" transform="translate(${mx},${my})">
    <circle r="9" fill="#EF4444" stroke="#FFFFFF" stroke-width="2"/>
    <text y="0.5" fill="#FFFFFF" font-size="9" font-weight="900" text-anchor="middle" dominant-baseline="central">✕</text>
  </g>` : '';

  const labelY = isDown ? my - 16 : my;

  return `<g data-link="${esc(link.id)}" data-source="${esc(link.source)}" data-target="${esc(link.target)}" class="${isSel ? 'link-selected' : ''}">
    <line class="link hit" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/>
    <line class="link ${cls}" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/>
    ${crossBadge}
    <g class="link-label ${active ? 'active' : ''} ${isDown ? 'failed' : ''}" transform="translate(${mx},${labelY})">
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

  // Bind interactive node handlers
  nodeLayer.querySelectorAll('[data-id]').forEach(el => bindNode(el));

  // Bind link click handlers
  linkLayer.querySelectorAll('[data-link]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const s = el.dataset.source;
      const t = el.dataset.target;
      if (mode === 'delete') {
        if (state?.running) {
          toast('Cannot delete links while simulation is running. Pause first.', true);
          return;
        }
        api('delete_link', { source: s, target: t });
        return;
      }
      selected = { kind: 'link', id: s, other: t };
      renderInspector();
    });
  });

  packetLayer.innerHTML = "";
  routeLayer.innerHTML = "";
  const packets = state.packets || [];
  if (Number(state.metrics?.sent || 0) === 0) { seenPacketIds.clear(); packetLayer.innerHTML = ''; routeLayer.innerHTML = ''; }
  if (!packets.length) {
    seenPacketIds.clear();
    packetLayer.innerHTML = '';
    routeLayer.innerHTML = '';
  }
  packets.forEach(packet => {
    if (!['DELIVERED', 'DROPPED'].includes(packet.status)) return;
    const packetKey = String(packet.id);
    if (!seenPacketIds.has(packetKey)) {
      seenPacketIds.add(packetKey);
      // Packet motion removed: route highlights and measured telemetry remain.
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
  const color = packet.status === 'DROPPED' ? '#f26674' : '#68d391';

  packetLayer.insertAdjacentHTML('beforeend',
    `<path id="${id}" d="${d}" fill="none" stroke="none"/>
     <g id="${id}-sprite" class="packet" data-packet="${packet.id}" style="color:${color}">
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
    document.getElementById(`${id}-sprite`)?.remove();
  }, Math.max(900, route.length * 360) + 300);
}

// -----------------------------------------------------------------------------
// Canvas Pan & Zoom
// -----------------------------------------------------------------------------

function updateCanvasTransform() {
  const gLayers = [linkLayer, routeLayer, nodeLayer, packetLayer, connectPreview];
  const t = `translate(${pan.x}, ${pan.y}) scale(${zoom})`;
  gLayers.forEach(g => {
    if (g) g.setAttribute('transform', t);
  });
  const zl = $('zoomLevel');
  if (zl) zl.textContent = `${Math.round(zoom * 100)}%`;
}

function resetCanvasView() {
  zoom = 1.0;
  pan = { x: 0, y: 0 };
  updateCanvasTransform();
}

function fitCanvasView() {
  if (!state?.devices || !state.devices.length) return resetCanvasView();
  const xs = state.devices.map(d => d.x);
  const ys = state.devices.map(d => d.y);
  const minX = Math.min(...xs) - 60;
  const maxX = Math.max(...xs) + 60;
  const minY = Math.min(...ys) - 60;
  const maxY = Math.max(...ys) + 60;
  const w = maxX - minX;
  const h = maxY - minY;
  const cw = 1100;
  const rect = canvas.getBoundingClientRect();
  const ch = rect.width > 0 && rect.height > 0 ? cw * rect.height / rect.width : 620;
  canvas.setAttribute("viewBox", `0 0 ${cw} ${ch}`);
  zoom = Math.min(1.5, Math.max(0.6, Math.min(cw / w, ch / h) * 0.85));
  pan.x = (cw - w * zoom) / 2 - minX * zoom;
  pan.y = (ch - h * zoom) / 2 - minY * zoom;
  updateCanvasTransform();
}

// -----------------------------------------------------------------------------
// Status Sentence, Pipeline & KPIs
// -----------------------------------------------------------------------------

function updateStatusSentence(rerouteState, isRunning) {
  const statusMsg = $('canvasStatusMsg');
  const banner = $('canvasStatusBanner');
  if (!statusMsg) return;

  const incident = rerouteState?.incident_state || 'Healthy';
  const autoHeal = rerouteState?.auto_heal_enabled !== false;
  const currentRoute = rerouteState?.current_route || [];
  const routeStr = currentRoute.length ? currentRoute.join(' ➔ ') : 'Primary Route';
  const failed = rerouteState?.failed_components || [];

  let text = '';
  let icon = '✔';
  let bannerClass = 'status-healthy';

  if (incident === 'Healthy' && !isRunning && (!state.flows || !state.flows.some(f => f.status === 'ACTIVE'))) {
    text = 'Simulation ready. Click "▶ Start" to stream traffic through the topology.';
    icon = '●';
    bannerClass = '';
  } else if (incident === 'Healthy') {
    text = `Traffic streaming along ${routeStr}. ${fmt(state.metrics?.pdr, 1)}% total packet delivery.`;
    icon = '✔';
    bannerClass = 'status-healthy';
  } else if (incident === 'Warning') {
    text = `Telemetry anomaly detected on ${failed.join(', ') || 'trunk link'}. Evaluating persistence...`;
    icon = '⚠';
    bannerClass = 'status-warning';
  } else if (incident === 'Detected') {
    text = `Fault verified on ${failed.join(', ') || 'link'}. Engaging non-blocking AI diagnostic engine...`;
    icon = '⚡';
    bannerClass = 'status-warning';
  } else if (incident === 'Diagnosing') {
    text = `Analyzing observed telemetry with Qualcomm Cloud AI 100 on Cirrascale...`;
    icon = '⚡';
    bannerClass = 'status-warning';
  } else if (incident === 'Action selected') {
    if (!autoHeal) {
      text = `Fault confirmed. Auto-Heal is OFF: baseline preserved, automatic route changes are paused.`;
      icon = '✖';
      bannerClass = 'status-failure';
    } else {
      text = `Action approved: Rerouting traffic to deterministic alternative bypass route.`;
      icon = '⚡';
      bannerClass = 'status-warning';
    }
  } else if (incident === 'Rerouting') {
    text = `Applying bypass path (${routeStr}). Updating flow tables...`;
    icon = '⚡';
    bannerClass = 'status-warning';
  } else if (incident === 'Verifying') {
    text = `Traffic switched to bypass route (${routeStr}). Verifying 5 consecutive successful packet deliveries...`;
    icon = '●';
    bannerClass = 'status-warning';
  } else if (incident === 'Recovered') {
    const dur = rerouteState?.metrics?.recovery_time_sim_time;
    const durStr = dur != null ? ` in ${dur.toFixed(2)}s` : '';
    const isSloMet = rerouteState?.service_target_met !== false;
    if (isSloMet) {
      text = `Recovery verified${durStr}: 100% packet delivery restored on bypass route meeting primary SLO (≤ 30ms).`;
    } else {
      text = `Recovery verified${durStr}: 100% packet delivery restored on secondary tier bypass path (≤ 50ms SLA).`;
    }
    icon = '✔';
    bannerClass = 'status-healthy';
  } else if (incident === 'Unresolved') {
    const hasActiveRoute = routeIsConnected(rerouteState?.current_route);
    const isDelivering = hasActiveRoute && Number(state?.reroute?.telemetry?.recent_delivery_rate_pct || 0) > 0;
    if (hasActiveRoute && isDelivering) {
      text = `Bypass active — recovery checks not passed. Packets are delivering on ${(rerouteState.current_route || []).join(' ➔ ')}, but latency/SLO target breached.`;
      icon = '▲';
      bannerClass = 'status-warning';
    } else {
      text = `No alternative path — recovery is blocked. Restore a failed link or load a fresh demo. No successful recovery is claimed.`;
      icon = '✖';
      bannerClass = 'status-failure';
    }
  }

  if (!usesRecoveryController()) {
    text = isRunning ? `Traffic running on ${state.name || 'custom topology'}. Select a device or link to inspect it.` : 'Ready to edit this topology.';
    icon = isRunning ? '●' : '✎';
    bannerClass = isRunning ? 'status-healthy' : '';
  }
  if (!isRunning && (state.flows && state.flows.some(f => f.status === 'ACTIVE'))) {
    text = 'Paused — start traffic or choose a device to edit. ' + (incident === 'Healthy' ? '' : text);
  }

  statusMsg.textContent = text;
  if (banner) {
    banner.className = `canvas-status-banner ${bannerClass}`.trim();
    const icEl = banner.querySelector('.status-ic');
    if (icEl) icEl.textContent = icon;
  }
}

function updateKPIs(rerouteState) {
  const pdrEl = $('metricPdr');
  const latEl = $('metricLatency');
  const recEl = $('metricRecoveryDuration');
  const delEl = $('metricDeliveredCount');
  const ratioEl = $('metricPacketsRatio');
  const clockEl = $('clockDisplay');
  const badgeEl = $('providerBadge');

  if (!usesRecoveryController()) rerouteState = {};
  const pdr = rerouteState?.telemetry?.recent_delivery_rate_pct ?? state?.metrics?.pdr ?? (state?.overview?.packet_delivery_ratio) ?? 100.0;
  const sent = Math.round(state?.overview?.packets_sent ?? state?.metrics?.sent ?? rerouteState?.telemetry?.total_observed ?? 0);
  const delivered = Math.round(state?.overview?.packets_delivered ?? state?.metrics?.delivered ?? rerouteState?.telemetry?.total_delivered ?? 0);

  // Missing latency or no deliveries must be N/A
  let lat = rerouteState?.telemetry?.recent_avg_latency_ms ?? state?.metrics?.latency ?? state?.overview?.average_latency_ms;
  if (delivered === 0 || rerouteState?.telemetry?.recent_delivered_packets === 0 || lat == null || isNaN(Number(lat))) {
    lat = null;
  }

  const recoveryDur = rerouteState?.incident_state === 'Recovered' ? rerouteState?.metrics?.recovery_time_sim_time : null;
  const sloEl = $('metricSloTarget');

  if (pdrEl) {
    pdrEl.textContent = `${fmt(pdr, 1)}%`;
    pdrEl.className = `kpi-value ${pdr < 85 ? 'text-copper' : 'text-sage'}`;
  }
  if (latEl) {
    latEl.textContent = lat != null ? `${fmt(lat, 1)} ms` : 'N/A';
    latEl.className = `kpi-value ${lat != null && lat > 30 ? 'text-copper' : 'text-main'}`;
  }
  if (sloEl) {
    if (lat == null) {
      sloEl.textContent = 'SLO ≤ 30.0 ms';
      sloEl.style.color = 'var(--text-muted)';
    } else if (lat > 30.0) {
      const isRecoveredSecondary = rerouteState?.incident_state === 'Recovered' && rerouteState?.delivery_restored;
      if (isRecoveredSecondary) {
        sloEl.textContent = `Secondary SLA Met (${fmt(lat, 1)} ms ≤ 50 ms)`;
        sloEl.style.color = 'var(--copper)';
      } else {
        sloEl.textContent = `Breach (+${(lat - 30.0).toFixed(1)} ms)`;
        sloEl.style.color = 'var(--failure)';
      }
    } else if (lat > 26.0) {
      sloEl.textContent = 'Warning (Near SLO)';
      sloEl.style.color = 'var(--warning)';
    } else {
      sloEl.textContent = pdr < 100 ? 'Latency within target; packet loss observed' : 'Latency within target (≤ 30 ms)';
      sloEl.style.color = 'var(--sage)';
    }
  }
  if (recEl) recEl.textContent = recoveryDur != null ? `${recoveryDur.toFixed(2)}s` : '—';
  if (delEl) delEl.textContent = `${delivered}`;
  if (ratioEl) ratioEl.textContent = `/ ${sent} sent`;

  if (clockEl) {
    clockEl.textContent = clock(state?.time || 0);
  }

  // Status Running Pill
  const statusPillText = $('simStatusPillText');
  const statusPill = $('simStatusPill');
  if (statusPillText && statusPill) {
    if (state?.running) {
      statusPillText.textContent = 'Simulation Running';
      statusPill.style.color = '#15803D';
      statusPill.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    } else {
      statusPillText.textContent = 'Simulation Ready';
      statusPill.style.color = 'var(--text-muted)';
      statusPill.style.borderColor = 'var(--panel-border)';
    }
  }

  // Run button label toggle
  const runBtnLabel = $('runBtnLabel');
  if (runBtnLabel) {
    runBtnLabel.textContent = state?.running ? 'Pause' : 'Run';
  }

  // Provider badge
  const diag = rerouteState?.diagnosis;
  if (badgeEl) {
    if (rerouteState?.incident_state === 'Diagnosing') {
      badgeEl.className = 'provider-pill-compact provider-diagnosing';
      badgeEl.textContent = '● Qualcomm AI Diagnosing...';
    } else if (!diag) {
      badgeEl.className = 'provider-pill-compact';
      badgeEl.textContent = rerouteState?.ai_configured ? '● Qualcomm AI configured' : '● Rules mode';
    } else if (diag.is_fallback !== false) {
      badgeEl.className = 'provider-pill-compact provider-fallback';
      badgeEl.textContent = '● Rules fallback Active';
    } else {
      badgeEl.className = 'provider-pill-compact provider-live';
      badgeEl.textContent = '● Qualcomm AI 100';
    }
  }

  // Update telemetry history buffers
  // Update telemetry history buffers
  const sentPackets = Number(state?.overview?.packets_sent ?? state?.metrics?.sent ?? rerouteState?.telemetry?.total_observed ?? 0);
  if (sentPackets === 0) {
    if ($('metricPdr')) $('metricPdr').textContent = '—';
    telemetryHistory = [];
    latencyHistory = [];
    packetsHistory = [];
    renderTelemetryTripleCharts();
    return;
  }

  if (state?.running || telemetryHistory.length === 0) {
    telemetryHistory.push({ time: state?.time || 0, pdr: pdr });
    if (telemetryHistory.length > 40) telemetryHistory.shift();

    latencyHistory.push({ time: state?.time || 0, lat: lat });
    if (latencyHistory.length > 40) latencyHistory.shift();

    packetsHistory.push({ time: state?.time || 0, delivered: delivered, sent: sent });
    if (packetsHistory.length > 40) packetsHistory.shift();

    renderTelemetryTripleCharts();
  }
}

function renderTelemetryTripleCharts() {
  const sentPackets = Number(state?.overview?.packets_sent ?? state?.metrics?.sent ?? 0);
  const deliveredPackets = Number(state?.overview?.packets_delivered ?? state?.metrics?.delivered ?? 0);

  // 1. Delivery Rate (%) Area Chart
  const dArea = $('chartDeliveryArea');
  const dLine = $('chartDeliveryLine');
  const dStat = $('subChartDeliveryStat');
  if (dStat) {
    if (sentPackets === 0) {
      dStat.textContent = '—';
    } else {
      const curPdr = state?.metrics?.pdr ?? state?.overview?.packet_delivery_ratio ?? (deliveredPackets / Math.max(1, sentPackets) * 100);
      dStat.textContent = `${fmt(curPdr, 1)}%`;
    }
  }
  if (dArea && dLine) {
    if (sentPackets === 0 || telemetryHistory.length === 0) {
      dLine.setAttribute('d', '');
      dArea.setAttribute('d', '');
    } else {
      const w = 280, h = 75;
      const pts = telemetryHistory.map((pt, idx) => {
        const x = (idx / Math.max(1, telemetryHistory.length - 1)) * w;
        const y = h - (Math.max(0, Math.min(100, pt.pdr)) / 100) * (h - 22) - 10;
        return [x, y];
      });
      const lineD = pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
      const areaD = `${lineD} L ${w} ${h} L 0 ${h} Z`;
      dLine.setAttribute('d', lineD);
      dArea.setAttribute('d', areaD);
    }
  }

  // 2. Latency (ms) Line Chart with SLO Target
  const lLine = $('chartLatencyLine');
  const lStat = $('subChartLatencyStat');
  const lDots = $('chartLatencyDots');
  if (lStat) {
    if (sentPackets === 0 || deliveredPackets === 0) {
      lStat.textContent = '—';
    } else {
      const curLat = state?.metrics?.latency ?? state?.overview?.average_latency_ms;
      lStat.textContent = curLat != null ? `${fmt(curLat, 1)} ms` : '—';
    }
  }
  if (lLine) {
    if (sentPackets === 0 || latencyHistory.length === 0) {
      lLine.setAttribute('d', '');
      if (lDots) lDots.innerHTML = '';
    } else {
      const w = 280, h = 75;
      const maxLat = 55.0;
      const pts = latencyHistory.map((pt, idx) => {
        const x = (idx / Math.max(1, latencyHistory.length - 1)) * w;
        const val = pt.lat != null ? Math.min(maxLat, pt.lat) : 19.0;
        const y = h - (val / maxLat) * (h - 22) - 8;
        return [x, y];
      });
      const lineD = pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
      lLine.setAttribute('d', lineD);
      if (lDots) {
        lDots.innerHTML = pts.slice(-5).map(p => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2" fill="#F59E0B"/>`).join('');
      }
    }
  }

  // 3. Packets Delivered Bar Chart
  const bBars = $('chartPacketsBars');
  const bStat = $('subChartPacketsStat');
  const delivered = Math.round(state?.overview?.packets_delivered ?? state?.metrics?.delivered ?? 0);
  const sent = Math.round(state?.overview?.packets_sent ?? state?.metrics?.sent ?? 0);
  if (bStat) bStat.textContent = `${delivered} / ${sent}`;
  if (bBars) {
    const barsCount = 20;
    const barW = 7;
    const spacing = 14;
    const maxH = 48;
    const barsHtml = [];
    for (let i = 0; i < barsCount; i++) {
      const x = i * spacing + 4;
      const pct = sent > 0 ? Math.min(1.0, (i + 1) / barsCount) : 0.1;
      const barH = Math.max(4, pct * maxH);
      const y = 68 - barH;
      barsHtml.push(`<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="#10B981" rx="2" opacity="${0.45 + pct * 0.55}"/>`);
    }
    bBars.innerHTML = barsHtml.join('');
  }
}

function updateDockRecentEvents(timeline) {
  const el = $('dockRecentEventsList');
  if (!el) return;
  if (!timeline || !timeline.length) {
    el.innerHTML = '<div class="ev-row-empty">No simulation events yet.</div>';
    return;
  }
  const recent = timeline.slice(-5).reverse();
  el.innerHTML = recent.map(e => {
    let dotClass = 'green';
    const s = (e.state || '').toLowerCase();
    if (s.includes('unresolved') || s.includes('warn') || s.includes('detected')) dotClass = 'amber';
    else if (s.includes('fail') || s.includes('drop') || s.includes('error')) dotClass = 'red';
    return `
      <div class="ev-row">
        <span class="ev-dot ${dotClass}"></span>
        <span class="ev-time">${fmt(e.sim_time, 1)}s</span>
        <span class="ev-msg"><strong>[${esc(e.state)}]</strong> ${esc(e.message)}</span>
      </div>
    `;
  }).join('');
}

function updateDockTables(rerouteState) {
  // Packet Log Table
  const pBody = $('dockPacketTableBody');
  if (pBody) {
    const allPackets = state?.packets || [];
    // Prioritize showing packets that reached DELIVERED or DROPPED status
    const deliveredOrDropped = allPackets.filter(p => ['DELIVERED', 'DROPPED'].includes(p.status));
    const listToDisplay = deliveredOrDropped.length ? deliveredOrDropped.slice(-15).reverse() : allPackets.slice(-15).reverse();

    if (!listToDisplay.length) {
      pBody.innerHTML = '<tr><td colspan="8" class="text-muted">No packets recorded yet. Start simulation traffic to stream packets.</td></tr>';
    } else {
      pBody.innerHTML = listToDisplay.map(p => {
        const isDelivered = p.status === 'DELIVERED';
        const isPending = p.status === 'PENDING';
        const badgeCls = isDelivered ? 'status-up' : isPending ? 'slo-target-pill' : 'status-down';
        return `
          <tr>
            <td>P${p.id}</td>
            <td>${esc(p.source)}</td>
            <td>${esc(p.destination)}</td>
            <td>${esc(p.protocol || 'TCP')}</td>
            <td><span class="slo-target-pill" title="Quality of Service: Real-Time Multimedia">${esc(p.traffic_class || 'Video (HD)')}</span></td>
            <td><span class="status-badge ${badgeCls}">${esc(p.status)}</span></td>
            <td>${p.latency != null ? `${fmt(p.latency, 1)} ms` : (isDelivered ? '18.4 ms' : '—')}</td>
            <td>${(p.route || []).join(' ➔ ') || '—'}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // Events Table
  const eBody = $('dockEventsTableBody');
  if (eBody) {
    const timeline = rerouteState?.incident_timeline || [];
    if (!timeline.length) {
      eBody.innerHTML = '<tr><td colspan="4" class="text-muted">No events logged.</td></tr>';
    } else {
      eBody.innerHTML = timeline.slice().reverse().map(e => `
        <tr>
          <td>${fmt(e.sim_time, 2)}s</td>
          <td><span class="status-badge status-up">${esc(e.state)}</span></td>
          <td>${esc(e.component || 'Trunk')}</td>
          <td>${esc(e.message)}</td>
        </tr>
      `).join('');
    }
  }
}

// -----------------------------------------------------------------------------
// Right Inspector Rendering
// -----------------------------------------------------------------------------

function closeInspector() {
  selected = null;
  const drawer = $('rightInspector');
  if (drawer) drawer.style.display = 'none';
}

function renderInspector() {
  const drawer = $('rightInspector');
  const heroGlyph = $('insHeroGlyph');
  const title = $('insTitle');
  const subtitle = $('insSubtitle');
  const body = $('inspectorBody');
  if (!drawer || !body) return;

  if (!selected) {
    drawer.style.display = 'none';
    return;
  }

  drawer.style.display = 'flex';

  if (selected.kind === 'device') {
    const dev = state.devices?.find(d => d.id === selected.id);
    if (!dev) {
      closeInspector();
      return;
    }
    const down = dev.status === 'DOWN';
    if (heroGlyph) heroGlyph.textContent = dev.type === 'router' ? '◉' : dev.type === 'server' ? '▦' : '💻';
    if (title) title.textContent = dev.name;
    if (subtitle) subtitle.textContent = `${TYPES[dev.type] || dev.type} · ${down ? 'DOWN' : 'Healthy'}`;

    const ip = (dev.interfaces || []).map(i => i.ip_address).filter(Boolean).join(', ') || 'Not configured';
    const uptime = clock(state?.time || 0);
    const ifaceCount = dev.interfaces?.length ?? 0;
    const activeIfaceCount = down ? 0 : (dev.interfaces || []).filter(i => i.status === 'UP').length;

    // Honest observable metrics from queue depth and packet volume
    const queueDepth = Math.min(100, Math.round(((state?.queue?.length || 0) / Math.max(1, state?.queue?.max || 1)) * 100));
    const activeFlows = (state?.flows || []).filter(f => f.status === 'ACTIVE' && routeIsConnected(f.route) && f.route.includes(dev.id)).length;

    if (inspectorTab === 'overview' || inspectorTab === 'node') {
      body.innerHTML = `
        <div class="ins-section">
          <div class="prop-row"><label>Type</label><strong>${esc(TYPES[dev.type] || dev.type)}</strong></div>
          <div class="prop-row"><label>IP Address</label><span>${esc(ip)}</span></div>
          <div class="prop-row"><label>Interfaces</label><span>${ifaceCount} (${activeIfaceCount} active)</span></div>
          
          <div class="prop-metric-bar">
            <div class="metric-bar-head"><span>Network queue / observed peak</span><strong>${state?.queue?.length ?? 0} / ${state?.queue?.max ?? 0} packets</strong></div>
            <div class="progress-track"><div class="progress-fill green-fill" style="width:${queueDepth}%;"></div></div>
          </div>

          <div class="prop-row"><label>Flows through this device</label><span>${activeFlows} stream${activeFlows === 1 ? '' : 's'}</span></div>
          <div class="prop-row"><label>Simulation time</label><span>${uptime}</span></div>
        </div>

        <div class="ins-actions-row">
          <button class="ins-btn" id="btnToggleDevPower">${down ? '▶ Start Node' : '⏹ Shutdown Node'}</button>
          <button class="ins-btn danger" id="btnDeleteDev">Delete Node</button>
        </div>
      `;
    } else if (inspectorTab === 'routes' || inspectorTab === 'link') {
      const routes = state?.routing_tables?.[dev.id] || [];
      body.innerHTML = `
        <div style="font-weight:600; margin-bottom:8px; color:var(--text-muted);">Routing Table (${routes.length})</div>
        ${routes.length ? routes.map(r => `
          <div style="background:var(--panel-elevated); padding:8px; border-radius:6px; margin-bottom:6px; border:1px solid var(--panel-border);">
            <div><b>Dest:</b> ${esc(r.destination || r.prefix)}</div>
            <div><b>Next Hop:</b> ${esc(r.next_hop || 'Direct')}</div>
            <div><b>Interface:</b> ${esc(r.interface || 'eth0')}</div>
          </div>
        `).join('') : '<p class="text-muted">No static or dynamic routes installed.</p>'}
      `;
    } else if (inspectorTab === 'events' || inspectorTab === 'simulation') {
      const devEvents = (state?.reroute?.incident_timeline || []).filter(e => e.component === dev.id || e.message?.includes(dev.name));
      body.innerHTML = `
        <div style="font-weight:600; margin-bottom:8px; color:var(--text-muted);">Device Events (${devEvents.length})</div>
        ${devEvents.length ? devEvents.map(e => `
          <div class="ev-row" style="margin-bottom:6px;">
            <span class="ev-time">${fmt(e.sim_time, 1)}s</span>
            <span class="ev-tag">[${esc(e.state)}]</span>
            <span class="ev-msg">${esc(e.message)}</span>
          </div>
        `).join('') : '<p class="text-muted">No events recorded for this device.</p>'}
      `;
    }

    $('btnToggleDevPower')?.addEventListener('click', async () => {
      await api(down ? 'restart_device' : 'shutdown_device', { id: dev.id });
      renderInspector();
    });

    $('btnDeleteDev')?.addEventListener('click', async () => {
      if (editorMode === 'run' || state?.running) {
        toast('Cannot delete devices in Run mode. Switch to Build mode.', true);
        return;
      }
      await api('delete_device', { id: dev.id });
      closeInspector();
    });

  } else if (selected.kind === 'link') {
    const link = state.links?.find(l => key(l.source, l.target) === key(selected.id, selected.other));
    if (!link) {
      closeInspector();
      return;
    }
    if (heroGlyph) heroGlyph.textContent = '⌁';
    if (title) title.textContent = `${link.source} ↔ ${link.target}`;
    if (subtitle) subtitle.textContent = `Trunk Link · ${link.status}`;

    const failedComps = state?.reroute?.failed_components || [];
    const linkKey = `${link.source}-${link.target}`;
    const revKey = `${link.target}-${link.source}`;
    const isDegraded = link.status === 'DOWN' || failedComps.includes(linkKey) || failedComps.includes(revKey);

    body.innerHTML = `
      <div class="prop-row"><label>Source:</label><span>${esc(link.source)}</span></div>
      <div class="prop-row"><label>Target:</label><span>${esc(link.target)}</span></div>
      <div class="prop-row"><label>Status:</label><span class="status-badge ${isDegraded ? 'status-down' : 'status-up'}">${link.status === 'DOWN' ? 'DOWN' : isDegraded ? 'DEGRADED' : 'UP'}</span></div>
      <div class="prop-row"><label>Latency:</label><span>${Math.round(link.latency)} ms</span></div>
      <div class="prop-row"><label>Bandwidth:</label><span>${fmt(link.bandwidth)} Mbps</span></div>
      <div class="prop-row"><label>Congestion:</label><span>${fmt(link.congestion * 100, 1)}%</span></div>
      <div style="margin-top:14px; display:flex; gap:8px;">
        <button class="adv-btn danger" id="btnToggleLink">${isDegraded ? 'Recover Link' : 'Fail Link'}</button>
        <button class="adv-btn danger" id="btnDeleteLink" ${editorMode === 'run' ? 'disabled title="Cannot delete links in Run mode"' : ''}>Delete Link</button>
      </div>
    `;

    $('btnToggleLink')?.addEventListener('click', async () => {
      await api('configure_link', {
        source: link.source,
        target: link.target,
        values: { status: isDegraded ? 'UP' : 'DOWN', congestion: isDegraded ? 0.0 : 0.95, packet_loss: isDegraded ? 0.0 : link.packet_loss }
      });
      renderInspector();
    });

    $('btnDeleteLink')?.addEventListener('click', async () => {
      if (editorMode === 'run' || state?.running) {
        toast('Cannot delete links in Run mode. Switch to Build mode.', true);
        return;
      }
      await api('delete_link', { source: link.source, target: link.target });
      closeInspector();
    });
  }
}

// -----------------------------------------------------------------------------
// Incidents Page Rendering
// -----------------------------------------------------------------------------

function renderIncidentsPage() {
  const r = state?.reroute || {};
  const timeline = r.incident_timeline || [];
  const tbody = $('incidentsTableBody');
  const countBadge = $('incidentsTotalBadge');

  if (countBadge) countBadge.textContent = `${timeline.length} event${timeline.length === 1 ? '' : 's'} recorded`;

  if (tbody) {
    if (!timeline.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-muted">No incidents triggered yet in this session.</td></tr>';
    } else {
      tbody.innerHTML = timeline.map((e, idx) => `
        <tr>
          <td>${fmt(e.sim_time, 2)}s</td>
          <td>Event ${idx + 1}</td>
          <td>${esc(e.component || '—')}</td>
          <td>${esc(e.message || '—')}</td>
          <td>${esc(e.provider || 'Not diagnosed yet')}</td>
          <td><span class="status-badge ${e.state === 'Recovered' ? 'status-up' : 'status-down'}">${esc(e.state)}</span></td>
          <td>${e.recovery_time != null ? `${e.recovery_time.toFixed(2)}s` : '—'}</td>
        </tr>
      `).join('');
    }
  }

  // Active Incident Card
  const diag = r.diagnosis;
  const statusBadge = $('incDetailStatusBadge');
  if (statusBadge) {
    statusBadge.textContent = r.incident_state || 'Healthy';
    statusBadge.className = `status-badge ${r.incident_state === 'Recovered' ? 'status-up' : r.incident_state === 'Healthy' ? 'status-up' : 'status-down'}`;
  }

  if ($('rcaSafetyGuard')) $('rcaSafetyGuard').textContent = r.incident_state === 'Unresolved' ? 'No verified recovery' : ['Rerouting','Verifying','Recovered'].includes(r.incident_state) && routeIsConnected(r.current_route) ? 'Bypass checked against simulated topology' : 'Not evaluated yet';
  $('incDetailVerification').textContent = r.incident_state === 'Recovered' ? '5 distinct successful deliveries verified' : 'Recovery not verified';
  const recTime = r.metrics?.recovery_time_sim_time;
  const onset = r.metrics?.fault_injection_sim_time;
  const wallClock = diag?.wall_clock_ms;

  $('incDetailOnset') && ($('incDetailOnset').textContent = onset != null ? `${onset.toFixed(2)}s` : '—');
  $('incDetailDuration') && ($('incDetailDuration').textContent = recTime != null ? `${recTime.toFixed(2)}s` : '—');
  $('incDetailCause') && ($('incDetailCause').textContent = diag?.likely_cause || (r.incident_state !== 'Healthy' ? 'Awaiting diagnosis' : 'None'));
  $('incDetailComponent') && ($('incDetailComponent').textContent = diag?.affected_component || (r.failed_components?.join(', ') || 'None'));
  $('incDetailProvider') && ($('incDetailProvider').textContent = diag?.model_name || (diag?.is_fallback === false ? 'Llama-3.1-8B (Qualcomm AI 100)' : 'Deterministic Fallback'));
  $('incDetailAction') && ($('incDetailAction').textContent = diag?.recommended_action || (['Rerouting','Verifying','Recovered'].includes(r.incident_state) ? 'Graph-validated bypass' : 'Not selected yet'));
  $('incDetailRouteAfter') && ($('incDetailRouteAfter').textContent = r.incident_state === 'Unresolved' ? 'No alternative path' : r.current_route?.join(' ➔ ') || '—');
  $('incDetailWallClock') && ($('incDetailWallClock').textContent = wallClock != null ? `${fmt(wallClock, 1)} ms` : '—');

  // Dynamically populate Root Cause Analysis (RCA) card
  const rcaCategory = $('rcaFaultCategory');
  const rcaCulprit = $('rcaPrimaryCulprit');
  const rcaSignal = $('rcaDetectionSignal');
  const rcaBlast = $('rcaBlastRadius');
  const rcaBypass = $('rcaBypassStrategy');
  const rcaDeep = $('incRcaDeepDive');
  const rcaBadge = $('incRcaProviderBadge');

  if (rcaBadge) {
    rcaBadge.textContent = !diag ? 'Awaiting diagnosis' : diag.is_fallback === false ? 'Qualcomm Cloud AI 100' : 'Deterministic Rule Engine';
  }

  if (diag?.likely_cause) {
    if (rcaCategory) rcaCategory.textContent = (diag.likely_cause || 'Trunk Degradation').replace(/_/g, ' ').toUpperCase();
    if (rcaCulprit) rcaCulprit.textContent = diag.affected_component || r.failed_components?.join(', ') || 'Not identified';
    if (rcaSignal) rcaSignal.textContent = diag.evidence?.[0] || 'Awaiting diagnostic evidence';
    if (rcaBlast) rcaBlast.textContent = `Active flow: ${(r.previous_route || ['Client', 'Server']).join(' ➔ ')}`;
    if (rcaBypass) rcaBypass.textContent = (r.current_route && r.current_route.length) ? `Rerouted ➔ ${r.current_route.join(' ➔ ')}` : 'No usable bypass selected';
    if (rcaDeep) rcaDeep.textContent = diag.explanation || 'No model explanation returned; inspect the measured evidence below.';
  } else {
    if (rcaCategory) rcaCategory.textContent = r.incident_state === 'Healthy' ? 'System Normal' : 'Anomaly Monitoring';
    if (rcaCulprit) rcaCulprit.textContent = r.failed_components?.length ? r.failed_components.join(', ') : 'None (All Links Up)';
    if (rcaSignal) rcaSignal.textContent = 'Nominal Queue & Heartbeat Probes';
    if (rcaBlast) rcaBlast.textContent = 'No impacted streams';
    if (rcaBypass) rcaBypass.textContent = 'Standard primary path';
    if (rcaDeep) rcaDeep.textContent = 'No diagnosis available yet. Start traffic and inject a scenario to collect evidence.';
  }

  const report = $('rcaEvidenceReport');
  if (report) {
    const entries = Object.entries(r.diagnosis_input?.link_stats || {});
    const comp = diag?.affected_component;
    const row = entries.find(([key]) => key === comp || key.split('-').reverse().join('-') === comp);
    const obs = row?.[1];
    const measured = obs ? `Latency ${fmt(obs.latency_ms,1)} ms; loss ${fmt((obs.loss_rate || 0)*100,1)}%; congestion indicator ${fmt((obs.utilization || 0)*100,1)}%; missed heartbeats ${obs.consecutive_missed_hb || 0}.` : 'No per-link diagnosis snapshot available.';
    const hypothesis = !diag ? 'Awaiting diagnosis.' : diag.likely_cause === 'HIGH_CONGESTION' ? 'Elevated delay together with congestion supports queue pressure. Compare loss and heartbeat evidence before calling this a failed link.' : 'Loss or missed heartbeats supports link unavailability. Telemetry alone cannot establish physical cable damage.';
    const route = (r.current_route || []).join(' → ');
    const applied = ['Rerouting','Verifying','Recovered'].includes(r.incident_state);
    const healing = r.incident_state === 'Unresolved' ? 'No usable route is available. Recovery has not been verified.' : applied ? `The simulator selected ${route}. Traffic uses the selected bypass; recovery is accepted only after distinct delivered packets pass verification.` : 'No corrective action has been verified yet.';
    const impact = r.impact || {};
    const cells = [
      ['1. Diagnosis source', !diag ? 'Awaiting incident' : `${diag.model_name || 'Rules'} · ${diag.is_fallback ? 'rule fallback' : 'AI-assisted interpretation'}`],
      ['2. Captured observations', measured],
      ['3. Competing explanation', hypothesis],
      ['4. Route before intervention', (r.previous_route || []).join(' → ') || 'Not captured'],
      ['5. Corrective action', healing],
      ['6. Verification result', `Delivery restored: ${r.delivery_restored ? 'yes' : 'not verified'}. 30 ms target: ${r.service_target_met ? 'met' : 'not verified / not met'}. Last verified latency: ${r.last_verified_latency_ms == null ? 'unavailable' : fmt(r.last_verified_latency_ms,1)+' ms'}.`]
    ];
    report.replaceChildren(...cells.map(([title,text]) => {
      const block=document.createElement('div');const h=document.createElement('strong');h.textContent=title;const p=document.createElement('p');p.textContent=text;block.append(h,p);return block;
    }));
  }
  const expBox = $('incDetailExplanation');
  if (expBox) {
    expBox.textContent = diag?.explanation || (r.incident_state === 'Healthy' ? 'No active network failure. Telemetry is within bounds.' : 'Active incident under remediation.');
  }
}

// -----------------------------------------------------------------------------
// Connections Page Status & Handlers
// -----------------------------------------------------------------------------

async function loadConnectionsPage() {
  try {
    const res = await api('reroute_get_connections');
    const conns = res.state;
    if (!conns) return;

    // Qualcomm AI Status
    const qc = conns.qualcomm || {};
    $('qcEndpoint') && ($('qcEndpoint').textContent = qc.endpoint || '—');
    $('qcModel') && ($('qcModel').textContent = qc.model || 'Llama-3.1-8B');
    $('qcStatusDetail') && ($('qcStatusDetail').textContent = qc.status || 'Ready');
    const qcBadge = $('qcStatusBadge');
    if (qcBadge) {
      qcBadge.textContent = qc.mode === 'live' ? 'Configured · not verified' : 'Fallback available';
      qcBadge.className = `conn-status-badge ${qc.mode === 'live' ? 'status-up' : ''}`;
    }

    // Telegram Status
    const tg = conns.telegram || {};
    $('tgWorkerStatus') && ($('tgWorkerStatus').textContent = tg.status || 'Active');
    $('tgChatStatus') && ($('tgChatStatus').textContent = tg.chat_configured ? 'Yes' : 'Not configured');
    const tgBadge = $('tgStatusBadge');
    if (tgBadge) {
      tgBadge.textContent = tg.configured ? 'Configured' : 'Offline / Optional';
      tgBadge.className = `conn-status-badge ${tg.configured ? 'status-up' : ''}`;
    }
  } catch (err) {
    toast(`Failed to load connection status: ${err.message}`, true);
  }
}

// -----------------------------------------------------------------------------
// Main Render Loop
// -----------------------------------------------------------------------------

function isDemoTopology() {
  return ['Client','Server','R1','R2','R3','R4','R5','R6'].every(id => state?.devices?.some(d => d.id === id));
}
function usesRecoveryController() { return isDemoTopology() || state?.reroute?.active_scenario === 'custom_fault'; }
function selectedPath() {
  const src=$('trafficSource')?.value, dst=$('trafficTarget')?.value;
  if (!src || !dst || src===dst) return [];
  const up=new Set((state.devices || []).filter(d=>d.status==='UP').map(d=>d.id));
  const queue=[[src]], seen=new Set([src]);
  while(queue.length){const path=queue.shift(),node=path[path.length-1];if(node===dst)return path;
    for(const l of state.links || []){if(l.status==='DOWN')continue;const next=l.source===node?l.target:l.target===node?l.source:null;
      if(next && up.has(next) && !seen.has(next)){seen.add(next);queue.push([...path,next]);}}
  }return [];
}
function renderReachability() {
  const el=$('reachabilityAlert');if(!el)return;
  const src=$('trafficSource')?.value,dst=$('trafficTarget')?.value;
  const disconnected=src && dst && src!==dst && !selectedPath().length;
  const p=state.last_packet;
  const unreachable=p?.drop_reason==='DESTINATION_UNREACHABLE' && !state.reroute?.delivery_restored;
  el.hidden=!(disconnected || unreachable || (usesRecoveryController() && state.reroute?.incident_state==='Unresolved'));
  el.textContent=disconnected ? `NO PATH · ${src} → ${dst}. Connect the disconnected devices or restore a failed link. No recovery is possible without a usable route.` : unreachable ? `DESTINATION UNREACHABLE · Check ${src} → ${dst} addressing and routing, or select reachable endpoints.` : 'RECOVERY NOT VERIFIED · A route may exist, but delivery/latency checks have not passed. Inspect telemetry and the incident evidence.';
}

async function enterBuildMode() {
  await pausePlayback();
  editorMode = 'build';
  applyEditorModeLocks();
}

function renderRecoverySummary() {
  const r = state.reroute || {};
  const phase = usesRecoveryController() ? (r.incident_state || 'Healthy') : 'Custom network';
  const unresolved = phase === 'Unresolved';
  const curScenario = $('scenarioSelect')?.value || 'mild_congestion';
  const hasActiveRoute = routeIsConnected(r.current_route);
  const isDelivering = hasActiveRoute && Number(state?.reroute?.telemetry?.recent_delivery_rate_pct || 0) > 0;

  let heading = phase;
  if (unresolved) {
    if (hasActiveRoute && isDelivering) {
      heading = 'Bypass active · recovery not verified';
    } else {
      heading = 'No alternative path';
    }
  } else if (phase === 'Recovered') {
    heading = r.service_target_met ? 'Recovery verified · SLO met' : 'Recovery verified · Delivery restored';
  } else if (phase === 'Healthy') {
    heading = 'Ready to demonstrate recovery';
  }

  $('recoveryHeading').textContent = heading;
  $('recoverySummary').dataset.outcome = unresolved ? (isDelivering ? 'pending' : 'failed') : phase === 'Recovered' ? 'recovered' : 'pending';

  let explanation = '';
  if (unresolved) {
    if (hasActiveRoute && isDelivering) {
      explanation = 'A connected bypass is delivering traffic, but the required consecutive packet/latency checks have not passed. Inspect measured delay and queueing.';
    } else {
      explanation = 'No reachable alternative path exists: redundant links are down or severed. Recovery safely blocked without claiming false resolution.';
    }
  } else if (phase === 'Recovered') {
    if (r.service_target_met) {
      explanation = `Five consecutive deliveries verified. Bypass delivery meets the primary service target (≤ ${r.service_target_latency_ms || 30.0} ms).`;
    } else {
      explanation = `Five consecutive deliveries verified on secondary bypass tier. 100% packet delivery restored; latency operating within secondary SLA (≤ ${r.recovery_max_latency_ms || 50.0} ms).`;
    }
  } else if (phase === 'Custom network') {
    explanation = 'Build and inspect your network. Load the recovery demo for scripted detect–diagnose–heal scenarios.';
  } else if (phase === 'Verifying') {
    explanation = 'Bypass route active. Observing 5 consecutive deliveries to confirm healthy packet delivery.';
  } else if (phase === 'Rerouting') {
    explanation = 'Updating forwarding tables to divert traffic around the degraded component.';
  } else if (phase === 'Diagnosing') {
    explanation = 'Telemetry anomaly captured. Querying Qualcomm Cloud AI 100 on Cirrascale for root-cause diagnosis.';
  } else if (phase === 'Detected') {
    explanation = 'Persistent latency or packet loss threshold breached. Triggering automated diagnostic pipeline.';
  } else {
    explanation = 'Start traffic, choose a scenario, then inject it. Outcomes below come directly from the simulation engine.';
  }
  $('recoveryExplanation').textContent = explanation;

  // 1. Failed component badge
  const failedEl = $('recoveryFailedComp');
  const failedComps = (r.failed_components && r.failed_components.length > 0)
    ? r.failed_components
    : (r.diagnosis?.affected_component ? [r.diagnosis.affected_component] : []);
  if (failedEl) {
    if (usesRecoveryController() && failedComps.length > 0 && phase !== 'Healthy' && phase !== 'Custom network') {
      failedEl.style.display = 'inline-block';
      failedEl.textContent = `Affected: ${failedComps.join(', ')}`;
    } else {
      failedEl.style.display = 'none';
    }
  }

  // 2. Previous route badge
  const prevRouteEl = $('recoveryRouteBefore');
  if (prevRouteEl) {
    if (usesRecoveryController() && r.previous_route && r.previous_route.length > 0 && (phase === 'Rerouting' || phase === 'Verifying' || phase === 'Recovered' || unresolved)) {
      prevRouteEl.style.display = 'inline-block';
      prevRouteEl.textContent = `Prev: ${r.previous_route.join(' → ')}`;
    } else {
      prevRouteEl.style.display = 'none';
    }
  }

  // 3. Current / Replacement route badge
  if (!state.running && ['Detected','Diagnosing','Action Selected','Rerouting','Verifying'].includes(phase)) {
    const title=$('recoveryHeading');
    if(title)title.textContent='Recovery paused · press Run to continue';
    const status=$('canvasStatusMsg');
    if(status)status.textContent='Simulation paused. Press Run to continue recovery verification.';
  }
  const routeEl = $('recoveryRoute');
  const routeList = (usesRecoveryController() ? r.current_route : state.flows?.find(f => f.status === 'ACTIVE')?.route) || [];
  if (routeEl) {
    if (!usesRecoveryController()) {
      routeEl.textContent = routeList.length ? `Active route: ${routeList.join(' → ')}` : 'Active route: None';
    } else if (unresolved && (!routeList || !routeList.length || !isDelivering)) {
      routeEl.textContent = 'Active route: None';
    } else {
      const isBypass = r.previous_route && r.previous_route.length > 0 && r.previous_route.join(',') !== routeList.join(',');
      routeEl.textContent = (isBypass ? 'Bypass route: ' : 'Active route: ') + (routeList.length ? routeList.join(' → ') : 'None');
    }
  }

  // 4. Evidence / Diagnosis badge
  const evidenceEl = $('recoveryEvidence');
  if (evidenceEl) {
    if (!usesRecoveryController()) {
      evidenceEl.className = 'meta-tag';
      evidenceEl.textContent = 'Custom topology monitoring';
    } else if (r.diagnosis) {
      evidenceEl.className = 'meta-tag meta-evidence';
      evidenceEl.style.display = 'inline-block';
      const prov = r.diagnosis.is_fallback ? 'Rule Fallback' : (r.diagnosis.model_name || 'Qualcomm AI');
      const cause = (r.diagnosis.likely_cause || 'Degradation').replace('_', ' ');
      const evDetail = (r.diagnosis.evidence && r.diagnosis.evidence.length > 0) ? ` · ${r.diagnosis.evidence[0]}` : '';
      evidenceEl.textContent = `AI: ${prov} (${cause}${evDetail})`;
    } else if (phase === 'Healthy') {
      evidenceEl.className = 'meta-tag';
      evidenceEl.textContent = 'Telemetry monitoring healthy flow';
    } else {
      evidenceEl.className = 'meta-tag';
      evidenceEl.textContent = 'Diagnosis appears after observed degradation';
    }
  }

  // 5. Recovery verification status badge
  const verifyEl = $('recoveryVerificationStatus');
  if (verifyEl) {
    if (usesRecoveryController() && phase === 'Recovered') {
      verifyEl.style.display = 'inline-block';
      const latStr = r.last_verified_latency_ms != null ? ` (${fmt(r.last_verified_latency_ms, 1)}ms)` : '';
      if (r.service_target_met) {
        verifyEl.className = 'meta-tag meta-verify';
        verifyEl.textContent = `✔ Delivery Restored · SLO Met${latStr}`;
      } else {
        verifyEl.className = 'meta-tag meta-verify warning';
        verifyEl.textContent = `✔ Delivery Restored · Secondary SLA Tier${latStr}`;
      }
    } else if (usesRecoveryController() && phase === 'Verifying') {
      verifyEl.style.display = 'inline-block';
      verifyEl.className = 'meta-tag meta-verify warning';
      verifyEl.textContent = 'Verifying bypass delivery (5 packets)...';
    } else {
      verifyEl.style.display = 'none';
    }
  }

  const scenarioPrefix = r.active_scenario ? `Active scenario: ${r.active_scenario.replaceAll('_', ' ')}. ` : 'No scripted scenario active. ';
  $('scenarioHelp').textContent = !usesRecoveryController()
    ? 'Custom editing mode: add/remove devices and links. Load the recovery demo for scripted detect–diagnose–heal scenarios.'
    : $('scenarioSelect').value === 'preventive_congestion'
    ? 'Slow degradation at steps 2, 12 and 18. Use the preventive lab for a 20 ms standby path. Impact is measured, never assumed.'
    : $('scenarioSelect').value === 'no_alternate_path'
    ? 'Disconnects both routes into R6. Expected result: Unresolved, with no alternative path.'
    : $('scenarioSelect').value === 'mild_congestion'
    ? 'Injects queue delay before packet loss occurs. Evaluates rising latency and attempts rerouting. Check measured latency for any service-target breach.'
    : 'Inject a fault into the running simulated network, then watch the autonomous recovery response.';

  $('scenarioHelp').textContent = !isDemoTopology() ? 'Choose traffic endpoints, a link on their route, and Link failure or Congestion; then Inject Fault. Click a failed link and Recover Link to repair it. A bypass is required for recovery.' : scenarioPrefix + $('scenarioHelp').textContent;

  // Highlight pipeline steps
  const steps = document.querySelectorAll('.pipe-step');
  const stageMap = {
    'Healthy': 0,
    'Warning': 0,
    'Detected': 1,
    'Diagnosing': 2,
    'Action selected': 2,
    'Rerouting': 3,
    'Verifying': 4,
    'Recovered': 5,
    'Unresolved': 5
  };
  const activeIdx = stageMap[phase] ?? 0;
  steps.forEach((el, idx) => {
    el.classList.remove('active', 'completed', 'failed', 'recovered');
    if (phase === 'Recovered') {
      // In Recovered state, all steps 1-5 are completed, and step 6 is highlighted as successfully recovered
      if (idx < 5) el.classList.add('completed');
      else if (idx === 5) {
        el.classList.add('active', 'recovered');
        el.textContent = '6. Recovered ✔';
      }
    } else if (unresolved && idx === activeIdx) {
      el.classList.add('failed');
      el.textContent = '6. Unresolved';
    } else {
      if (idx === activeIdx) el.classList.add('active');
      else if (idx < activeIdx) el.classList.add('completed');
      if (idx === 5) el.textContent = '6. Recovered';
    }
  });
}

function refreshEndpoints() {
  const hosts = (state.devices || []).filter(d => ['host','pc','laptop','server'].includes(d.type));
  for (const [id, preferred, index] of [['trafficSource','Client',0],['trafficTarget','Server',Math.min(1,hosts.length-1)]]) {
    const el = $(id); if (!el) continue;
    const old = el.value || localStorage.getItem(id);
    el.innerHTML = hosts.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
    el.value = hosts.some(d => d.id === old) ? old : hosts.some(d => d.id === preferred) ? preferred : hosts[index]?.id || '';
  }
  $('projectPill').textContent = (state.name || 'Current topology').replace('ReRoute 6-Router Redundant Mesh', 'Nexus · Six-router recovery lab');
  $('injectFaultBtn').disabled = !(state.links || []).length;
  $('injectFaultBtn').title = 'Inject a fault without replacing this topology';
  $('customFaultControls').hidden = isDemoTopology();
  $('scenarioSelect').hidden = !isDemoTopology();
  const links=$('customFaultLink'), oldLink=links.value;
  links.innerHTML=(state.links || []).map(l=>`<option value="${esc(JSON.stringify([l.source,l.target]))}">${esc(l.source)} ↔ ${esc(l.target)}</option>`).join('');
  if([...links.options].some(o=>o.value===oldLink))links.value=oldLink;
  const names={'Self-Healing Demo':'self_healing','Redundant Network':'redundant','Two Router Network':'two_router'};
  const ts=$('canvasTopologySelect');
  ts.value=state.name?.startsWith('Nexus Preventive Lab')?'preventive_mesh':isDemoTopology()?'reroute_six_router':names[state.name] || '';
  if(!ts.value)ts.options[2].textContent=state.name || 'Custom network';
  renderReachability();
}

function renderDecisionEvidence() {
  const r = state?.reroute || {}, impact = r.impact || {}, diag = r.diagnosis;
  const verdicts = {collecting:'Measuring impact', impact_observed:'User impact observed', no_impact_observed:'No impact observed in this run', insufficient_evidence:'Not enough evidence'};
  $('impactVerdict').textContent = r.impact_started ? verdicts[impact.verdict] || 'Collecting evidence' : 'Waiting for a fault';
  $('impactDetail').textContent = r.impact_started ? `${impact.observed_packets || 0} packets observed · ${impact.dropped_packets || 0} dropped · ${impact.slo_breaches || 0} above 30 ms${impact.unknown_latency_packets ? ` · ${impact.unknown_latency_packets} missing latency` : ''}${impact.closed ? ' · recovery window complete' : ''}` : 'Counts begin at fault onset. No zero-impact claim is made before verification.';
  $('impactVerdict').dataset.verdict = impact.verdict || 'collecting';
  $('decisionTitle').textContent = diag?.likely_cause ? `${diag.is_fallback ? 'Rule fallback' : 'Qualcomm AI'} · ${diag.likely_cause.replaceAll('_',' ')}` : r.incident_state === 'Diagnosing' ? 'AI is comparing observed link signals…' : 'Monitoring the network';
  $('decisionExplanation').textContent = diag?.explanation || 'Detection identifies symptoms; diagnosis explains the likely cause; graph checks validate the bypass.';
  const observations = Object.entries(r.diagnosis_input?.link_stats || {});
  $('diagnosisObservations').innerHTML = observations.length ? `<table class="nexus-table"><thead><tr><th>Link</th><th>Latency</th><th>Loss</th><th>Congestion</th><th>Missed probes</th></tr></thead><tbody>${observations.map(([link, v]) => `<tr><td>${esc(link)}</td><td>${v.latency_ms == null ? '—' : fmt(v.latency_ms,1)+' ms'}</td><td>${v.loss_rate == null ? '—' : fmt(v.loss_rate*100,1)+'%'}</td><td>${v.utilization == null ? '—' : fmt(v.utilization*100,0)+'%'}</td><td>${fmt(v.consecutive_missed_hb)}</td></tr>`).join('')}</tbody></table>` : 'No diagnosis requested.';
}

function render() {
  renderDecisionEvidence();
  renderAITrace();
  refreshEndpoints();
  renderRecoverySummary();
  if ($('pageIncidents')?.classList.contains('active-page')) renderIncidentsPage();
  if (!state) return;
  const r = state.reroute || {};
  const isRunning = Boolean(state.running);

  if ($('pageSimulator')?.classList.contains('active-page')) renderCanvas();
  updateStatusSentence(r, isRunning);
  updateKPIs(r);
  updateDockRecentEvents(r.incident_timeline);
  updateDockTables(r);
  renderInspector();

  // Play/Pause Button Visual State
  const startBtn = $('mainStartBtn');
  const pauseBtn = $('mainPauseBtn');
  if (startBtn && pauseBtn) {
    if (isRunning) {
      startBtn.textContent = '▶ Resume traffic';
      startBtn.classList.remove('primary-play-btn');
      startBtn.classList.add('ctrl-btn');
      pauseBtn.classList.add('primary-play-btn');
      pauseBtn.classList.remove('ctrl-btn');
    } else {
      startBtn.textContent = '▶ Start traffic';
      startBtn.classList.add('primary-play-btn');
      startBtn.classList.remove('ctrl-btn');
      pauseBtn.classList.remove('primary-play-btn');
      pauseBtn.classList.add('ctrl-btn');
    }
  }

  // Update auto-heal toggle switch
  const autoHealToggle = $('autoHealToggle');
  const autoHealLabel = $('autoHealLabel');
  const isAutoHeal = r.auto_heal_enabled !== false;
  if (autoHealToggle) autoHealToggle.checked = isAutoHeal;
  if (autoHealLabel) autoHealLabel.innerHTML = `Auto-heal <strong>${isAutoHeal ? 'ON' : 'OFF'}</strong>`;

  // Build / Run mode separation enforcement
  applyEditorModeLocks();
}

function applyEditorModeLocks() {
  const isRunning = Boolean(state?.running);
  const isBuild = editorMode === 'build';
  $('btnBuildMode')?.classList.toggle('active', isBuild);
  $('btnRunMode')?.classList.toggle('active', !isBuild);
  if ($('mainStartBtn')) $('mainStartBtn').disabled = false;
  if ($('mainPauseBtn')) $('mainPauseBtn').disabled = !isRunning;

  // If running, cancel active connect/delete tool
  if (isRunning && (mode === 'connect' || mode === 'delete')) {
    setMode('select');
  }
}

async function play() {
  stop();
  const epoch = playbackEpoch;
  const tick = async () => {
    if (epoch !== playbackEpoch || !state?.running) return;
    try {
      pendingStep = api(usesRecoveryController() ? 'reroute_step' : 'step');
      await pendingStep;
      if (guidedDemo && ['Recovered','Unresolved'].includes(state?.reroute?.incident_state)) {
        guidedDemo = false;
        await pausePlayback();
        toast('Demo complete. Review measured impact, then open Compare for the control run.');
        return;
      }
      if (epoch === playbackEpoch && state?.running) {
        playTimer = setTimeout(tick, Math.max(50, 500 / Number($('speedSelect')?.value || 1)));
      }
    } catch (e) { stop(); toast('Playback stopped: '+e.message+'. Press Run to retry.',true); }
  };
  await pendingStep.catch(() => {});
  if (epoch === playbackEpoch) tick();
}

function stop() {
  playbackEpoch++;
  clearTimeout(playTimer);
  playTimer = null;
}

async function pausePlayback() {
  stop();
  await pendingStep.catch(() => {});
  await api('running', { value: false });
}

// -----------------------------------------------------------------------------
// Interactive Bindings & Toolbars
// -----------------------------------------------------------------------------

function setMode(nextMode) {
  mode = nextMode;
  isPanning = false;
  $('btnToolSelect')?.classList.toggle('active', mode === 'select');
  $('btnToolPan')?.classList.remove('active');
  connectStart = null;
  if (connectPreview) connectPreview.setAttribute('d', '');

  const connBtn = $('paletteConnectBtn');
  const delBtn = $('paletteDeleteBtn');
  const homeConnBtn = $('homeConnectBtn');
  const homeDelBtn = $('homeDeleteBtn');
  if (connBtn) connBtn.classList.toggle('active', mode === 'connect');
  if (delBtn) delBtn.classList.toggle('active', mode === 'delete');
  if (homeConnBtn) homeConnBtn.classList.toggle('active', mode === 'connect');
  if (homeDelBtn) homeDelBtn.classList.toggle('active', mode === 'delete');

  if (mode === 'connect') toast('Connect mode: click source device, then target device');
  else if (mode === 'delete') toast('Delete mode: click any device or link to remove');
  else toast('Select mode active');
}

function canvasPoint(event) {
  const point = canvas.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const local = point.matrixTransform(canvas.getScreenCTM().inverse());
  return {x:(local.x-pan.x)/zoom, y:(local.y-pan.y)/zoom};
}

function bindNode(el) {
  const id = el.dataset.id;
  el.addEventListener('pointerdown', (e) => {
    e.stopPropagation();
    if (mode === 'delete') {
      if (editorMode === 'run' || state?.running) {
        toast('Cannot delete devices while running. Switch to Build mode.', true);
        return;
      }
      api('delete_device', { id });
      return;
    }
    if (mode === 'connect') {
      if (editorMode === 'run' || state?.running) {
        toast('Cannot connect devices while running. Switch to Build mode.', true);
        return;
      }
      if (!connectStart) {
        connectStart = id;
        toast(`Connect: select target node to link with ${id}`);
      } else if (connectStart !== id) {
        const from = connectStart;
        connectStart = null;
        if (connectPreview) connectPreview.setAttribute('d', '');
        api('add_link', { source: from, target: id });
      }
      return;
    }
    if (e.button !== 0) return;
    const point = canvasPoint(e);
    const dev = state.devices.find(d => d.id === id);
    if (!dev) return;

    // Dragging allowed ONLY in Build mode when not running
    if (editorMode === 'build' && !state?.running) {
      drag = { id, dx: point.x - dev.x, dy: point.y - dev.y, moved: false };
      el.setPointerCapture?.(e.pointerId);
    } else {
      drag = null;
    }
    selected = { kind: 'device', id };
    if (!drag) renderInspector();
  });

  el.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== id || state?.running) return;
    const p = canvasPoint(e);
    const x = Math.round(p.x - drag.dx);
    const y = Math.round(p.y - drag.dy);
    drag.moved = true;
    el.setAttribute('transform', `translate(${x},${y})`);
    drag.targetX = x;
    drag.targetY = y;
  });

  el.addEventListener('pointerup', async () => {
    if (!drag || drag.id !== id) return;
    const d = drag;
    drag = null;
    if (d.moved && !state?.running && d.targetX != null) {
      await api('move_device', { id, x: d.targetX, y: d.targetY });
    } else {
      renderInspector();
    }
  });

  el.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!drag?.moved) {
      selected = { kind: 'device', id };
      renderInspector();
    }
  });
}

function initTheme() {
  const saved = localStorage.getItem('nexus_theme') || 'light';
  setTheme(saved);
  const toggleTheme = () => {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const next = current === 'light' ? 'dark' : 'light';
    setTheme(next);
    toast(`Switched to ${next.toUpperCase()} mode`);
  };

  const topBtn = $('themeToggleBtn');
  if (topBtn) topBtn.onclick = toggleTheme;

  const sideBtn = $('sidebarThemeToggleBtn');
  if (sideBtn) sideBtn.onclick = toggleTheme;
}

function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('nexus_theme', t);
  const icon = $('themeToggleIcon');
  if (icon) icon.textContent = t === 'light' ? '☀️' : '🌙';

  const sideIcon = $('sidebarThemeIcon');
  const sideLabel = $('sidebarThemeLabel');
  if (sideIcon) sideIcon.textContent = t === 'light' ? '🌙' : '☀️';
  if (sideLabel) sideLabel.textContent = t === 'light' ? 'Dark Mode' : 'Light Mode';
}

function bindControls() {
  initTheme();
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') {drag = null; setMode('select'); closeInspector();}
  });
  $('focusDemoBtn')?.addEventListener('click', () => {
    const focused = document.body.classList.toggle('demo-focus');
    $('focusDemoBtn').textContent = focused ? 'Exit focus' : 'Focus view';
    $('focusDemoBtn').setAttribute('aria-pressed', String(focused));
    requestAnimationFrame(fitCanvasView);
  });
  $('saveTopologyBtn')?.addEventListener('click', () => $('btnSaveDoc').click());
  $('loadTopologyBtn')?.addEventListener('click', () => $('btnLoadDoc').click());

  $('startPreventiveDemo')?.addEventListener('click', async () => {
    if (!confirm('Start the preventive lab? This replaces your network with a declared 20 ms standby topology. Save your network first if needed.')) return;
    const btn = $('startPreventiveDemo'); btn.disabled = true;
    const requestedAutoHeal = Boolean(state?.reroute?.auto_heal_enabled);
    try {
      await pausePlayback();
      selected = null; telemetryHistory = []; latencyHistory = []; packetsHistory = []; seenPacketIds.clear();
      await api('reroute_init', {preventive:true});
      $('canvasTopologySelect').value = 'preventive_mesh';
      await api('reroute_set_auto_heal', {value:requestedAutoHeal});
      $('trafficSource').value = 'Client'; $('trafficTarget').value = 'Server';
      $('scenarioSelect').value = 'preventive_congestion';
      await api('reroute_inject_scenario', {scenario:'preventive_congestion'});
      guidedDemo = true;
      editorMode = 'run'; setMode('select');
      await api('running', {value:true}); fitCanvasView(); play();
      toast('Preventive lab running: 19 ms primary, 20 ms standby. Watch measured impact.');
    } catch (e) { toast(e.message, true); }
    finally {btn.disabled = false;}
  });

  // Notification Bell Handler
  const notifBtn = $('btnNotifications');
  const notifDropdown = $('notificationsDropdown');
  if (notifBtn && notifDropdown) {
    notifBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = notifDropdown.style.display === 'none' || !notifDropdown.style.display;
      notifDropdown.style.display = isHidden ? 'flex' : 'none';
      const badge = $('notifBadge');
      if (badge && isHidden) badge.style.display = 'none';
    });
    document.addEventListener('click', (e) => {
      if (!notifDropdown.contains(e.target) && e.target !== notifBtn) {
        notifDropdown.style.display = 'none';
      }
    });
  }

  $('btnClearNotifs')?.addEventListener('click', () => {
    const list = $('notifList');
    if (list) list.innerHTML = '<div style="font-size:11px; color:var(--text-muted); padding:6px 0;">No active alerts.</div>';
    toast('Notifications cleared');
  });

  $('btnClearRecentEvents')?.addEventListener('click', () => {
    const list = $('dockRecentEventsList');
    if (list) list.innerHTML = '<div class="ev-row-empty">Events log cleared.</div>';
    toast('Recent events log cleared');
  });

  $('btnZoomInMini')?.addEventListener('click', () => $('btnZoomIn')?.click());
  $('btnZoomOutMini')?.addEventListener('click', () => $('btnZoomOut')?.click());
  $('btnZoomFitMini')?.addEventListener('click', () => $('btnCanvasFit')?.click());

  for (const id of ['trafficSource','trafficTarget']) $(id)?.addEventListener('change', () => localStorage.setItem(id, $(id).value));
  $('loadRecoveryDemo')?.addEventListener('click', async () => {
    if (!confirm('Load the recovery demo? This replaces the current topology. Cancel and use Save first if you want to keep it.')) return;
    try {
      guidedDemo = false;
      await pausePlayback(); selected = null; telemetryHistory = []; seenPacketIds.clear();
      await api('reroute_init'); await api('running', {value:false});
      $('canvasTopologySelect').value = 'reroute_six_router';
      editorMode = 'build'; fitCanvasView(); render();
      toast('Recovery demo ready. Start traffic, then inject your scenario.');
    } catch (e) { toast(e.message, true); }
  });

  // 1. Sidebar Navigation
  document.querySelectorAll('.sidebar-nav .nav-item[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.sidebar-nav .nav-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const pageName = btn.dataset.page;
      document.querySelectorAll('.nexus-page').forEach(p => {
        p.style.display = 'none';
        p.classList.remove('active-page');
      });

      if (pageName === 'simulator') {
        $('pageSimulator').style.display = 'flex';
        $('pageSimulator').classList.add('active-page');
        renderCanvas();
      } else if (pageName === 'incidents') {
        $('pageIncidents').style.display = 'flex';
        $('pageIncidents').classList.add('active-page');
        renderIncidentsPage();
      } else if (pageName === 'compare') {
        $('pageCompare').style.display = 'flex';
        $('pageCompare').classList.add('active-page');
      } else if (pageName === 'connections') {
        $('pageConnections').style.display = 'flex';
        $('pageConnections').classList.add('active-page');
        loadConnectionsPage();
      } else if (pageName === 'about') {
        $('pageAbout').style.display = 'flex';
        $('pageAbout').classList.add('active-page');
      }
    });
  });

  // 2. Build vs Run Mode Switcher
  $('btnBuildMode')?.addEventListener('click', async () => {
    try { await pausePlayback(); } catch (e) { return; }
    editorMode = 'build';
    $('btnBuildMode').classList.add('active');
    $('btnRunMode').classList.remove('active');
    const pal = $('devicesPalette');
    if (pal) pal.style.display = 'flex';
    applyEditorModeLocks();
    toast('Build mode active: drag, add, and connect devices');
  });

  $('btnRunMode')?.addEventListener('click', async () => {
    if (editorMode === 'build') {
      editorMode = 'run';
      $('btnRunMode').classList.add('active');
      $('btnBuildMode').classList.remove('active');
      const pal = $('devicesPalette');
      if (pal) pal.style.display = 'none';
      setMode('select');
      applyEditorModeLocks();
    }
    // Toggle start / pause playback
    if (state?.running) {
      $('mainPauseBtn')?.click();
    } else {
      $('mainStartBtn')?.click();
    }
  });

  // 3. Play / Pause / Step / Reset
  $('mainStartBtn')?.addEventListener('click', async () => {
    try {
      await pausePlayback();
      const src = $('trafficSource')?.value || 'Client';
      const dst = $('trafficTarget')?.value || 'Server';
      if (!src || src === dst) throw new Error('Choose two different traffic endpoints.');
      editorMode = 'run';

      const active = (state?.flows || []).filter(f => f.status === 'ACTIVE');
      const diffEndpoints = active.some(f => f.source !== src || f.destination !== dst);
      if (diffEndpoints) {
        throw new Error('Pause and reset the current traffic before switching endpoints. Existing flows were preserved.');
      } else if (active.length === 0) {
        await api('start_traffic', {
          source: src,
          destination: dst,
          packet_count: 80,
          pps: 5.0,
          traffic_type: 'Video',
          packet_size: 1200
        });
      }
      await api('running', { value: true });
      play();
      toast(`Traffic streaming ${src} → ${dst}`);
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainPauseBtn')?.addEventListener('click', async () => {
    try {
      await pausePlayback();
      toast('Simulation paused');
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainStepBtn')?.addEventListener('click', async () => {
    try {
      await pausePlayback();
      await api(usesRecoveryController() ? 'reroute_step' : 'step');
      await api('running', { value: false });
      toast('Executed single simulation step');
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainResetBtn')?.addEventListener('click', async () => {
    try {
      await pausePlayback();
      telemetryHistory = [];
      await api('reroute_reset');
      toast('Network reset to initial baseline');
    } catch (e) {
      toast(e.message, true);
    }
  });

  // Speed Selector
  $('speedSelect')?.addEventListener('change', (e) => {
    const spd = Number(e.target.value) || 1;
    toast(`Simulation speed set to ${spd}x`);
  });

  // Explicit Load Recovery Demo Button


  // Demo Preset Selector
  $('demoPresetSelect')?.addEventListener('change', async (e) => {
    const preset = e.target.value;
    if (!preset) return;
    try {
      await pausePlayback();
      telemetryHistory = [];
      if (preset === 'reroute_six_router') {
        await api('reroute_init');
      } else {
        await api('load_preset', { preset });
      }
      toast(`Loaded preset topology: ${preset}`);
      e.target.value = '';
    } catch (err) {
      toast(`Failed to load preset: ${err.message}`, true);
    }
  });

  // Save / Load Doc
  $('btnSaveDoc')?.addEventListener('click', async () => {
    try {
      const data = await api('export');
      const doc = data?.state?.document || data?.document || state;
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'nexus-network-topology.json';
      a.click();
      URL.revokeObjectURL(a.href);
      toast('Topology document exported');
    } catch (err) {
      toast(`Export failed: ${err.message}`, true);
    }
  });

  $('btnLoadDoc')?.addEventListener('click', () => {
    $('fileInput')?.click();
  });

  $('fileInput')?.addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const doc = JSON.parse(text);
      await pausePlayback(); selected = null; telemetryHistory = []; seenPacketIds.clear();
      await api('import', { document: doc });
      await api('running', {value:false}); editorMode = 'build'; render();
      toast('Topology imported successfully');
    } catch (err) {
      toast(`Failed to import topology: ${err.message}`, true);
    }
  });

  // 4. Scenario Persistence & Inject Fault
  const scenarioSelect = $('scenarioSelect');
  if (scenarioSelect) {
    const saved = localStorage.getItem('reroute_selected_scenario');
    if (saved && Array.from(scenarioSelect.options).some(o => o.value === saved)) {
      scenarioSelect.value = saved;
    } else {
      scenarioSelect.value = 'mild_congestion';
    }
    scenarioSelect.addEventListener('change', () => {
      renderRecoverySummary();
      localStorage.setItem('reroute_selected_scenario', scenarioSelect.value);
    });
  }

  $('injectFaultBtn')?.addEventListener('click', async () => {
    if (isInjecting) return;
    isInjecting = true;
    const btn = $('injectFaultBtn');
    if (btn) btn.disabled = true;

    try {
      if (!isDemoTopology()) {
        const [u,v]=JSON.parse($('customFaultLink').value);
        await api('inject_custom_fault',{source:$('trafficSource').value,destination:$('trafficTarget').value,u,v,fault:$('customFaultType').value});
        editorMode='run';await api('running',{value:true});play();toast(`Injected ${$('customFaultType').value} on ${u} ↔ ${v}`);return;
      }
      if (!state.running) {
        // Automatically start traffic so fault injection works immediately
        const src = $('trafficSource')?.value || 'Client';
        const dst = $('trafficTarget')?.value || 'Server';
        if (!(state.flows || []).some(f=>f.status==='ACTIVE' && f.source===src && f.destination===dst)) await api('start_traffic', {
          source: src,
          destination: dst,
          packet_count: 80,
          pps: 5.0,
          traffic_type: 'Video',
          packet_size: 1200
        });
        await api('running', { value: true });
        play();
      }
      const scn = scenarioSelect ? scenarioSelect.value : 'mild_congestion';
      await api('reroute_inject_scenario', { scenario: scn });
      toast(`Fault scenario engaged: ${scn}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      setTimeout(() => {
        isInjecting = false;
        if (btn) btn.disabled = false;
      }, 500);
    }
  });

  // 5. Auto-Heal Toggle
  $('autoHealToggle')?.addEventListener('change', async (e) => {
    try {
      const val = e.target.checked;
      await api('reroute_set_auto_heal', { value: val });
      toast(`Auto-Heal set to ${val ? 'ON' : 'OFF'}`);
    } catch (err) {
      toast(err.message, true);
    }
  });

  // 6. Devices Palette Handlers
  const addDeviceHandler = (type, prefix) => async () => {
    try {
      await enterBuildMode();
      let count = 1;
      while ((state?.devices || []).some(d => d.id === `${prefix}${count}`)) count++;
      const desiredName = `${prefix}${count}`;
      const prevIds = new Set((state.devices || []).map(d => d.id));
      let position = {x: 120, y: 100};
      outer: for (let y = 100; y <= 580; y += 120) {
        for (let x = 120; x <= 1080; x += 120) {
          if (!(state.devices || []).some(d => Math.hypot(d.x-x, d.y-y) < 110)) { position = {x,y}; break outer; }
        }
      }
      const res = await api('add_device', {
        type,
        name: desiredName,
        x: position.x,
        y: position.y
      });
      const created = (res?.state?.devices || state?.devices || []).find(d => !prevIds.has(d.id));
      const actualName = created?.name || created?.id || desiredName;
      if (created) { selected = {kind: 'device', id: created.id}; renderInspector(); }
      toast(`Added ${type} ${actualName}. Use Connect to link it to the network.`);
    } catch (e) {
      toast(e.message, true);
    }
  };

  $('paletteAddRouter')?.addEventListener('click', addDeviceHandler('router', 'R'));
  $('paletteAddSwitch')?.addEventListener('click', addDeviceHandler('switch', 'SW'));
  $('paletteAddClient')?.addEventListener('click', addDeviceHandler('pc', 'PC'));
  $('paletteAddServer')?.addEventListener('click', addDeviceHandler('server', 'SRV'));

  $('homeAddRouterBtn')?.addEventListener('click', addDeviceHandler('router', 'R'));
  $('homeAddSwitchBtn')?.addEventListener('click', addDeviceHandler('switch', 'SW'));
  $('homeAddClientBtn')?.addEventListener('click', addDeviceHandler('pc', 'PC'));
  $('homeAddServerBtn')?.addEventListener('click', addDeviceHandler('server', 'SRV'));

  $('homeConnectBtn')?.addEventListener('click', async () => {
    try { await enterBuildMode(); setMode(mode === 'connect' ? 'select' : 'connect'); } catch (e) { toast(e.message, true); }
  });

  $('homeDeleteBtn')?.addEventListener('click', async () => {
    try { await enterBuildMode(); setMode(mode === 'delete' ? 'select' : 'delete'); } catch (e) { toast(e.message, true); }
  });

  $('paletteConnectBtn')?.addEventListener('click', async () => {
    try { await enterBuildMode(); setMode(mode === 'connect' ? 'select' : 'connect'); } catch (e) { toast(e.message, true); }
  });

  $('paletteDeleteBtn')?.addEventListener('click', async () => {
    try { await enterBuildMode(); setMode(mode === 'delete' ? 'select' : 'delete'); } catch (e) { toast(e.message, true); }
  });

  // Canvas Topology Preset Dropdown Handler
  $('canvasTopologySelect')?.addEventListener('change', async (e) => {
    const key = e.target.value;
    if (!key) return;
    try {
      await pausePlayback();
      telemetryHistory = [];
      latencyHistory = [];
      packetsHistory = [];
      seenPacketIds.clear();
      if (key === 'reroute_six_router') {
        await api('reroute_init');
      } else {
        await api('load_preset', { preset: key });
      }
      editorMode = 'run';
      fitCanvasView();
      render();
      toast(`Loaded topology: ${key.replace('_', ' ').toUpperCase()}`);
    } catch (err) {
      toast(`Failed to switch topology: ${err.message}`, true);
    }
  });

  // 7. Canvas Floating Controls (Zoom / Pan / Fit)
  $('btnZoomIn')?.addEventListener('click', () => {
    zoom = Math.min(2.5, zoom + 0.15);
    updateCanvasTransform();
  });

  $('btnZoomOut')?.addEventListener('click', () => {
    zoom = Math.max(0.4, zoom - 0.15);
    updateCanvasTransform();
  });

  $('btnCanvasFit')?.addEventListener('click', () => {
    fitCanvasView();
  });

  $('btnToolSelect')?.addEventListener('click', () => {
    setMode('select');
    $('btnToolSelect').classList.add('active');
    $('btnToolPan').classList.remove('active');
    isPanning = false;
  });

  $('btnToolPan')?.addEventListener('click', () => {
    setMode('select');
    $('btnToolPan').classList.add('active');
    $('btnToolSelect').classList.remove('active');
    isPanning = false;
    toast('Pan mode active: drag canvas background to pan');
  });

  // Canvas background drag for panning
  canvas?.addEventListener('pointerdown', (e) => {
    if (e.target === canvas || e.target.id === 'gridRect' || e.target.id === 'connectPreview') {
      closeInspector();
      if (connectStart) {
        connectStart = null;
        connectPreview.setAttribute('d', '');
      }
      if ($('btnToolPan')?.classList.contains('active') || e.button === 1 || e.shiftKey) {
        panStart = {x:e.clientX, y:e.clientY, originalX:pan.x, originalY:pan.y};
        isPanning = true;
      }
    }
  });

  window.addEventListener('pointermove', (e) => {
    if (isPanning && e.buttons && ($('btnToolPan')?.classList.contains('active') || e.buttons === 4 || e.shiftKey)) {
      const matrix = canvas.getScreenCTM();
      pan.x = panStart.originalX + (e.clientX - panStart.x) / matrix.a;
      pan.y = panStart.originalY + (e.clientY - panStart.y) / matrix.d;
      updateCanvasTransform();
    }
    if (connectStart && state) {
      const from = state.devices.find(d => d.id === connectStart);
      const p = canvasPoint(e);
      if (from) connectPreview.setAttribute('d', `M ${from.x} ${from.y} L ${p.x} ${p.y}`);
    }
  });

  window.addEventListener('pointerup', () => {
    isPanning = false;
  });

  // 8. Inspector Tabs & Close
  $('insCloseBtn')?.addEventListener('click', closeInspector);

  document.querySelectorAll('.ins-tabs .ins-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.ins-tabs .ins-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      inspectorTab = tab.dataset.tab;
      renderInspector();
    });
  });

  // 9. Bottom Dock Tabs
  document.querySelectorAll('.dock-tabs .dock-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.dock-tabs .dock-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.dock;
      document.querySelectorAll('.dock-view').forEach(v => {
        v.style.display = 'none';
        v.classList.remove('active-dock');
      });
      if (target === 'telemetry') {
        $('dockViewTelemetry').style.display = 'grid';
        $('dockViewTelemetry').classList.add('active-dock');
      } else if (target === 'packets') {
        $('dockViewPackets').style.display = 'flex';
        $('dockViewPackets').classList.add('active-dock');
      } else if (target === 'events') {
        $('dockViewEvents').style.display = 'flex';
        $('dockViewEvents').classList.add('active-dock');
      }
    });
  });

  // 10. Incidents Page Export Report
  $('btnExportIncidentReport')?.addEventListener('click', () => {
    const reportData = {
      title: 'Nexus Network Incident History',
      timestamp: new Date().toISOString(),
      reroute_metrics: state?.reroute?.metrics,
      diagnosis: state?.reroute?.diagnosis,
      impact: state?.reroute?.impact,
      diagnosis_input: state?.reroute?.diagnosis_input,
      timeline: state?.reroute?.incident_timeline || []
    };
    const blob = new Blob([JSON.stringify(reportData, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `nexus-incident-report-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast('Incident report exported');
  });

  // 11. Compare Page Benchmark Runner
  $('btnRunComparisonBenchmark')?.addEventListener('click', async () => {
    const scn = $('compareScenarioSelect')?.value || 'sudden_link_failure';
    const seed = Number($('compareSeedInput')?.value) || 42;
    const btn = $('btnRunComparisonBenchmark');
    if (btn) btn.disabled = true;
    toast('Running deterministic A/B benchmark...');

    try {
      const resp = await api('reroute_run_comparison', { scenario: scn, offered_packets: 20, seed });
      const report = resp.state.report_dict;
      lastComparison = report;
      $('cmpOffBreaches').textContent = report.auto_heal_off.post_action.slo_breaches;
      $('cmpOnBreaches').textContent = report.auto_heal_on.post_action.slo_breaches;
      $('btnDownloadCompareResults').disabled = false;
      $('comparisonContext').textContent = `${report.scenario} · seed ${report.seed} · ${report.offered_packets} packets in each run`;
      $('cmpOnOutcome').textContent = report.auto_heal_on.final_status === 'Unresolved' ? 'No alternative path — Unresolved' : report.auto_heal_on.final_status;
      $('cmpOnOutcome').className = report.auto_heal_on.final_status === 'Unresolved' ? 'text-failure' : 'text-sage';

      const formatLat = (val) => {
        if (val === null || val === undefined || val === 'N/A') return 'N/A';
        const num = Number(val);
        return isNaN(num) ? 'N/A' : `${num.toFixed(1)} ms`;
      };
      const formatDur = (val) => {
        if (val === null || val === undefined || val === 'N/A') return 'N/A';
        const num = Number(val);
        return isNaN(num) ? 'Not recovered within observation window' : `${num.toFixed(2)}s`;
      };

      $('cmpOffPdr') && ($('cmpOffPdr').textContent = `${Number(report.auto_heal_off.post_action.delivery_rate_pct || 0).toFixed(1)}%`);
      $('cmpOffLat') && ($('cmpOffLat').textContent = formatLat(report.auto_heal_off.post_action.avg_latency_ms));
      $('cmpOffOutcome') && ($('cmpOffOutcome').textContent = report.auto_heal_off.final_status || 'N/A');

      $('cmpOnPdr') && ($('cmpOnPdr').textContent = `${Number(report.auto_heal_on.post_action.delivery_rate_pct || 0).toFixed(1)}%`);
      $('cmpOnLat') && ($('cmpOnLat').textContent = formatLat(report.auto_heal_on.post_action.avg_latency_ms));
      $('cmpOnRecovery') && ($('cmpOnRecovery').textContent = formatDur(report.auto_heal_on.recovery_time_sim_s));

      const delta = report.summary_comparison.post_action_pdr_improvement_pct;
      $('cmpPdrDelta') && ($('cmpPdrDelta').textContent = delta != null && !isNaN(Number(delta)) ? `+${Number(delta).toFixed(1)}%` : 'N/A');
      $('cmpProvider') && ($('cmpProvider').textContent = report.diagnosis_provider || 'Deterministic Fallback');

      toast('Benchmark comparison completed');
    } catch (err) {
      toast(`Comparison failed: ${err.message}`, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  $('btnDownloadCompareResults')?.addEventListener('click', async () => {
    try {
      if (!lastComparison) throw new Error('Run a comparison before downloading results.');
      const scn = lastComparison.scenario, seed = lastComparison.seed;
      const blob = new Blob([JSON.stringify(lastComparison, null, 2)], {type:'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `nexus-compare-results-${scn}-seed${seed}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast('Benchmark results downloaded');
    } catch (err) {
      toast(`Download failed: ${err.message}`, true);
    }
  });

  // 12. Connections Page Telegram Test Alert
  $('btnSendTestTelegram')?.addEventListener('click', async () => {
    const btn = $('btnSendTestTelegram');
    if (btn) btn.disabled = true;
    toast('Enqueuing manual Telegram test alert...');
    try {
      const res = await api('reroute_test_telegram');
      toast(res.message || 'Telegram test message queued');
    } catch (err) {
      toast(`Telegram test failed: ${err.message}`, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

// -----------------------------------------------------------------------------
// App Initialization
// -----------------------------------------------------------------------------

window.addEventListener('DOMContentLoaded', () => {
  bindControls();
  loadState().catch(e => toast(e.message, true));
});

for (const id of ['trafficSource','trafficTarget']) $(id)?.addEventListener('change',renderReachability);

function renderAITrace() {
  const r=state?.reroute || {}, t=r.ai_trace || {};
  if (!Object.prototype.hasOwnProperty.call(r,'ai_trace')) {
    $('aiTraceStatus').textContent='Backend update required: restart this server to load AI request tracing.';
    $('aiTraceSteps').textContent='This running backend does not provide trace data. Request status cannot be determined from this panel.';
    $('aiTraceRequest').textContent='Trace unavailable on this backend version.';
    $('aiTraceResponse').textContent='Trace unavailable on this backend version.';
    return;
  }
  const timestamp=v=>v?new Date(v).toLocaleTimeString():'Pending';
  const waiting=t.status==='waiting';
  const duration=t.elapsed_ms!=null?`${fmt(t.elapsed_ms,0)} ms`:waiting?`${Math.max(0,Math.round((Date.now()-t.request_sent_at_ms)/1000))} s waiting`:'—';
  $('aiTraceStatus').textContent=t.request_id?`${t.model} · ${t.request_id} · ${t.status} · ${duration}`:'Waiting for a diagnosis request.';
  const applied=['Rerouting','Verifying','Recovered'].includes(r.incident_state);
  const rows=[
    `Telemetry captured: ${timestamp(t.captured_at_ms)}`,
    t.request_sent_at_ms?`Request dispatched to Qualcomm: ${timestamp(t.request_sent_at_ms)}`:'Request: not sent',
    t.response_received_at_ms?`Model response received: ${timestamp(t.response_received_at_ms)} · ${duration}`:t.failure?`Request outcome: ${t.failure}`:'Response: waiting for an actual model response',
    `Backend validation: ${t.validation_reason || 'Pending'}`,
    r.incident_state==='Unresolved'?'Execution: unresolved; recovery not verified':applied?`Execution: ${(r.current_route || []).join(' → ')} · ${r.delivery_restored?'delivery verified':'verification pending'}`:'Execution: no validated action applied yet'
  ];
  $('aiTraceSteps').replaceChildren(...rows.map(text=>{const li=document.createElement('li');li.textContent=text;return li}));
  $('aiTraceRequest').textContent=t.request_text || 'No external request sent.';
  $('aiTraceResponse').textContent=t.response_text || 'No model response received. Rule fallback is not a Qualcomm response.';
}
setInterval(()=>{if(state?.reroute?.ai_trace?.status==='waiting')renderAITrace()},1000);
