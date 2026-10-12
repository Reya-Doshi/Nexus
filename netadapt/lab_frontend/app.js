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
let editorMode = 'run'; // 'build' | 'run'
let selected = null; // { kind: 'device'|'link', id: string, other?: string }
let inspectorTab = 'overview'; // 'overview' | 'routes' | 'events'
let connectStart = null;
let drag = null;
let seenPacketIds = new Set();
let playTimer = null;
let zoom = 1.0;
let pan = { x: 0, y: 0 };
let isPanning = false;
let panStart = { x: 0, y: 0 };
let isInjecting = false; // Debounce flag
let telemetryHistory = []; // Buffer of { time, pdr } for SVG sparkline

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
  if (!state?.devices?.some(d => d.id === 'Client')) {
    await api('reroute_init');
  } else {
    render();
    if (state.running) {
      play();
    }
  }
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
    <rect class="selection" x="-34" y="-38" width="68" height="82" rx="7"/>
    <rect class="device-body" x="-30" y="-33" width="60" height="60" rx="8"/>
    ${icon(device.type)}
    <circle class="port" cx="-25" cy="30" r="2.5"/>
    <circle class="port" cx="25" cy="30" r="2.5"/>
    <text class="node-label" y="45">${esc(device.name)}</text>
    <text class="node-type" y="57">${esc(TYPES[device.type] || device.type)}</text>
    <text class="node-status ${down ? 'down' : 'up'}" y="68">${down ? 'DOWN' : 'ONLINE'}</text>
  </g>`;
}

function routeEdges() {
  const edges = new Set();
  const currentRoute = state?.reroute?.current_route;
  if (Array.isArray(currentRoute) && currentRoute.length > 1) {
    for (let i = 0; i < currentRoute.length - 1; i++) {
      edges.add(key(currentRoute[i], currentRoute[i + 1]));
    }
  } else {
    for (const flow of state?.flows || []) {
      if (Array.isArray(flow.route) && flow.route.length > 1) {
        for (let i = 0; i < flow.route.length - 1; i++) {
          edges.add(key(flow.route[i], flow.route[i + 1]));
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

  const linkKey = `${link.source}-${link.target}`;
  const revKey = `${link.target}-${link.source}`;
  const failedComps = state?.reroute?.failed_components || [];
  const isFailedInEngine = failedComps.includes(linkKey) || failedComps.includes(revKey);
  const isDown = link.status === 'DOWN' || isFailedInEngine;

  let cls = 'healthy';
  if (isDown) cls = 'failed';
  else if (active) cls = 'active';
  else if (link.congestion > 0.3) cls = 'warning';

  const isSel = selected?.kind === 'link' && key(selected.id, selected.other) === key(link.source, link.target);

  return `<g data-link="${esc(link.id)}" data-source="${esc(link.source)}" data-target="${esc(link.target)}" class="${isSel ? 'link-selected' : ''}">
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
  const color = packet.status === 'DROPPED' ? '#f26674' : '#68d391';

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

// -----------------------------------------------------------------------------
// Canvas Pan & Zoom
// -----------------------------------------------------------------------------

function updateCanvasTransform() {
  const gLayers = [linkLayer, routeLayer, nodeLayer, packetLayer];
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
  const ch = 620;
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
  let dotClass = 'text-sage';

  if (!isRunning && (!state.flows || !state.flows.some(f => f.status === 'ACTIVE'))) {
    text = 'Simulation ready. Click "▶ Start" to stream traffic through the topology.';
  } else if (incident === 'Healthy') {
    text = `Traffic streaming along ${routeStr}. 100% packet delivery.`;
  } else if (incident === 'Warning') {
    text = `Telemetry anomaly detected on ${failed.join(', ') || 'trunk link'}. Evaluating persistence...`;
    dotClass = 'text-copper';
  } else if (incident === 'Detected') {
    text = `Fault verified on ${failed.join(', ') || 'link'}. Engaging non-blocking AI diagnostic engine...`;
    dotClass = 'text-copper';
  } else if (incident === 'Diagnosing') {
    text = `Qualcomm Cloud AI analyzing metrics. Diagnosing root cause...`;
    dotClass = 'text-copper';
  } else if (incident === 'Action selected') {
    if (!autoHeal) {
      text = `Fault confirmed. Auto-Heal is OFF: baseline preserved, packets dropping permanently.`;
      dotClass = 'text-copper';
    } else {
      text = `Action approved: Rerouting traffic to deterministic alternative bypass route.`;
    }
  } else if (incident === 'Rerouting') {
    text = `Applying bypass path (${routeStr}). Updating flow tables...`;
  } else if (incident === 'Verifying') {
    text = `Traffic switched to bypass route (${routeStr}). Verifying 5 consecutive successful packet deliveries...`;
    dotClass = 'text-copper';
  } else if (incident === 'Recovered') {
    const dur = rerouteState?.metrics?.recovery_time_sim_time;
    text = `Recovery verified! 5 consecutive packets delivered on bypass route${dur != null ? ` in ${dur.toFixed(2)}s` : ''}. Service restored.`;
  } else if (incident === 'Unresolved') {
    text = `Unresolved: All alternate paths severed or exhausted. Traffic dropping permanently on blocked routes.`;
    dotClass = 'text-copper';
  }

  statusMsg.textContent = text;
}

function updateKPIs(rerouteState) {
  const pdrEl = $('metricPdr');
  const latEl = $('metricLatency');
  const recEl = $('metricRecoveryDuration');
  const delEl = $('metricDeliveredCount');
  const ratioEl = $('metricPacketsRatio');
  const clockEl = $('clockDisplay');
  const badgeEl = $('providerBadge');

  const pdr = rerouteState?.telemetry?.recent_delivery_rate_pct ?? state?.metrics?.pdr ?? (state?.overview?.packet_delivery_ratio) ?? 100.0;
  const sent = Math.round(state?.overview?.packets_sent ?? state?.metrics?.sent ?? rerouteState?.telemetry?.total_observed ?? 0);
  const delivered = Math.round(state?.overview?.packets_delivered ?? state?.metrics?.delivered ?? rerouteState?.telemetry?.total_delivered ?? 0);

  // Missing latency or no deliveries must be N/A
  let lat = rerouteState?.telemetry?.recent_avg_latency_ms ?? state?.metrics?.latency ?? state?.overview?.average_latency_ms;
  if (delivered === 0 || lat == null || isNaN(Number(lat))) {
    lat = null;
  }

  const recoveryDur = rerouteState?.metrics?.recovery_time_sim_time;

  if (pdrEl) {
    pdrEl.textContent = `${fmt(pdr, 1)}%`;
    pdrEl.className = `kpi-value ${pdr < 85 ? 'text-copper' : 'text-sage'}`;
  }
  if (latEl) latEl.textContent = lat != null ? `${fmt(lat, 1)} ms` : 'N/A';
  if (recEl) recEl.textContent = recoveryDur != null ? `${recoveryDur.toFixed(2)}s` : '—';
  if (delEl) delEl.textContent = `${delivered}`;
  if (ratioEl) ratioEl.textContent = `${sent} sent`;

  if (clockEl) {
    clockEl.innerHTML = `Simulation time <strong>${clock(state?.time || 0)}</strong>`;
  }

  // Provider badge
  const diag = rerouteState?.diagnosis;
  if (badgeEl) {
    if (!diag || diag.is_fallback !== false) {
      badgeEl.className = 'provider-pill provider-fallback';
      badgeEl.textContent = '● Rules fallback Active';
    } else {
      badgeEl.className = 'provider-pill provider-live';
      badgeEl.textContent = '● Qualcomm Cloud AI 100 Active';
    }
  }

  // Update telemetry history sparkline buffer
  if (state?.running || telemetryHistory.length === 0) {
    telemetryHistory.push({ time: state?.time || 0, pdr: pdr });
    if (telemetryHistory.length > 50) telemetryHistory.shift();
    renderTelemetryChart();
  }
}

function renderTelemetryChart() {
  const path = $('chartPath');
  if (!path || !telemetryHistory.length) return;
  const w = 650;
  const h = 80;
  const points = telemetryHistory.map((pt, idx) => {
    const x = (idx / Math.max(1, telemetryHistory.length - 1)) * w;
    const y = h - (Math.max(0, Math.min(100, pt.pdr)) / 100) * (h - 10) - 5;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  path.setAttribute('d', `M ${points.join(' L ')}`);
}

function updateDockRecentEvents(timeline) {
  const el = $('dockRecentEventsList');
  if (!el) return;
  if (!timeline || !timeline.length) {
    el.innerHTML = '<div class="ev-row-empty">No simulation events yet.</div>';
    return;
  }
  const recent = timeline.slice(-4).reverse();
  el.innerHTML = recent.map(e => `
    <div class="ev-row">
      <span class="ev-time">${fmt(e.sim_time, 1)}s</span>
      <span class="ev-tag">[${esc(e.state)}]</span>
      <span class="ev-msg">${esc(e.message)}</span>
    </div>
  `).join('');
}

function updateDockTables(rerouteState) {
  // Packet Log Table
  const pBody = $('dockPacketTableBody');
  if (pBody) {
    const packets = (state?.packets || []).slice(-15).reverse();
    if (!packets.length) {
      pBody.innerHTML = '<tr><td colspan="8" class="text-muted">No packets recorded yet.</td></tr>';
    } else {
      pBody.innerHTML = packets.map(p => `
        <tr>
          <td>P${p.id}</td>
          <td>${esc(p.source)}</td>
          <td>${esc(p.destination)}</td>
          <td>${esc(p.protocol || 'TCP')}</td>
          <td>${esc(p.traffic_class || 'Video')}</td>
          <td><span class="status-badge ${p.status === 'DELIVERED' ? 'status-up' : 'status-down'}">${esc(p.status)}</span></td>
          <td>${p.latency != null ? `${fmt(p.latency, 1)} ms` : '—'}</td>
          <td>${(p.route || []).join(' ➔ ') || '—'}</td>
        </tr>
      `).join('');
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

    if (inspectorTab === 'overview') {
      body.innerHTML = `
        <div class="prop-row"><label>Device ID:</label><span>${esc(dev.id)}</span></div>
        <div class="prop-row"><label>Type:</label><span>${esc(TYPES[dev.type] || dev.type)}</span></div>
        <div class="prop-row"><label>Status:</label><span class="status-badge ${down ? 'status-down' : 'status-up'}">${down ? 'DOWN' : 'ONLINE'}</span></div>
        <div class="prop-row"><label>Position:</label><span>X: ${Math.round(dev.x)}, Y: ${Math.round(dev.y)}</span></div>
        <div class="prop-row"><label>Interfaces:</label><span>${dev.interfaces?.length || 0} active</span></div>
        <div style="margin-top:14px; display:flex; gap:8px;">
          <button class="adv-btn" id="btnToggleDevPower">${down ? '▶ Start Device' : '⏹ Shutdown Device'}</button>
          <button class="adv-btn danger" id="btnDeleteDev" ${editorMode === 'run' ? 'disabled title="Cannot delete devices in Run mode"' : ''}>🗑 Delete</button>
        </div>
      `;
    } else if (inspectorTab === 'routes') {
      const routes = dev.routing_table || [];
      body.innerHTML = `
        <div style="font-weight:600; margin-bottom:8px; color:var(--text-muted);">Routing Table (${routes.length})</div>
        ${routes.length ? routes.map(r => `
          <div style="background:var(--panel-elevated); padding:8px; border-radius:4px; margin-bottom:6px; border:1px solid var(--panel-border);">
            <div><b>Dest:</b> ${esc(r.destination || r.prefix)}</div>
            <div><b>Next Hop:</b> ${esc(r.next_hop || 'Direct')}</div>
            <div><b>Interface:</b> ${esc(r.interface || 'eth0')}</div>
          </div>
        `).join('') : '<p class="text-muted">No static or dynamic routes installed.</p>'}
      `;
    } else if (inspectorTab === 'events') {
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
      <div class="prop-row"><label>Status:</label><span class="status-badge ${isDegraded ? 'status-down' : 'status-up'}">${isDegraded ? 'FAULTED / DOWN' : 'UP'}</span></div>
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
        values: { status: isDegraded ? 'UP' : 'DOWN', congestion: isDegraded ? 0.0 : 0.95 }
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
          <td>INC-${1000 + idx}</td>
          <td>${esc(e.component || 'Trunk R2-R4')}</td>
          <td>${esc(e.failure_type || 'Link Disruption')}</td>
          <td>${esc(r.diagnosis?.is_fallback === false ? 'Qualcomm AI 100' : 'Rules Fallback')}</td>
          <td><span class="status-badge ${e.state === 'Recovered' ? 'status-up' : 'status-down'}">${esc(e.state)}</span></td>
          <td>${r.metrics?.recovery_time_sim_time != null ? `${r.metrics.recovery_time_sim_time.toFixed(2)}s` : '—'}</td>
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

  const recTime = r.metrics?.recovery_time_sim_time;
  const onset = r.metrics?.fault_injection_sim_time;
  const wallClock = diag?.wall_clock_ms;

  $('incDetailOnset') && ($('incDetailOnset').textContent = onset != null ? `${onset.toFixed(2)}s` : '—');
  $('incDetailDuration') && ($('incDetailDuration').textContent = recTime != null ? `${recTime.toFixed(2)}s` : '—');
  $('incDetailCause') && ($('incDetailCause').textContent = diag?.likely_cause || (r.incident_state !== 'Healthy' ? 'Trunk link failure' : 'None'));
  $('incDetailComponent') && ($('incDetailComponent').textContent = diag?.affected_component || (r.failed_components?.join(', ') || 'None'));
  $('incDetailProvider') && ($('incDetailProvider').textContent = diag?.model_name || (diag?.is_fallback === false ? 'Llama-3.1-8B (Qualcomm AI 100)' : 'Deterministic Fallback'));
  $('incDetailAction') && ($('incDetailAction').textContent = diag?.recommended_action || (r.incident_state !== 'Healthy' ? 'Reroute to alternate path' : 'None'));
  $('incDetailRouteAfter') && ($('incDetailRouteAfter').textContent = r.current_route?.join(' ➔ ') || '—');
  $('incDetailWallClock') && ($('incDetailWallClock').textContent = wallClock != null ? `${fmt(wallClock, 1)} ms` : '—');

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
      qcBadge.textContent = qc.mode === 'live' ? 'Online' : 'Fallback Ready';
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

function render() {
  if (!state) return;
  const r = state.reroute || {};
  const isRunning = Boolean(state.running);

  renderCanvas();
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
      startBtn.textContent = '▶ Running';
      startBtn.classList.remove('primary-play-btn');
      startBtn.classList.add('ctrl-btn');
      pauseBtn.classList.add('primary-play-btn');
      pauseBtn.classList.remove('ctrl-btn');
    } else {
      startBtn.textContent = '▶ Start';
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

  // Build Palette buttons
  const paletteBtns = ['paletteAddRouter', 'paletteAddSwitch', 'paletteAddClient', 'paletteAddServer', 'paletteConnectBtn', 'paletteDeleteBtn'];
  paletteBtns.forEach(id => {
    const btn = $(id);
    if (btn) btn.disabled = !isBuild || isRunning;
  });

  const tip = $('paletteTip');
  if (tip) {
    if (isRunning) tip.textContent = 'Editing locked while running';
    else if (!isBuild) tip.textContent = 'Switch to Build mode to edit';
    else tip.textContent = 'Build mode active: add or connect';
  }

  const modeHint = $('modeHint');
  if (modeHint) {
    modeHint.style.display = isRunning ? 'inline' : 'none';
  }

  // If running, cancel active connect/delete tool
  if (isRunning && (mode === 'connect' || mode === 'delete')) {
    setMode('select');
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
// Interactive Bindings & Toolbars
// -----------------------------------------------------------------------------

function setMode(nextMode) {
  mode = nextMode;
  connectStart = null;
  if (connectPreview) connectPreview.setAttribute('d', '');

  const connBtn = $('paletteConnectBtn');
  const delBtn = $('paletteDeleteBtn');
  if (connBtn) connBtn.classList.toggle('active', mode === 'connect');
  if (delBtn) delBtn.classList.toggle('active', mode === 'delete');

  if (mode === 'connect') toast('Connect mode: click source device, then target device');
  else if (mode === 'delete') toast('Delete mode: click any device or link to remove');
  else toast('Select mode active');
}

function canvasPoint(event) {
  const point = canvas.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(canvas.getScreenCTM().inverse());
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
    renderInspector();
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

function bindControls() {
  // 1. Sidebar Navigation
  document.querySelectorAll('.sidebar-nav .nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.sidebar-nav .nav-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const pageName = btn.dataset.page;
      document.querySelectorAll('.dhaara-page').forEach(p => {
        p.style.display = 'none';
        p.classList.remove('active-page');
      });

      if (pageName === 'simulator') {
        $('pageSimulator').style.display = 'flex';
        $('pageSimulator').classList.add('active-page');
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
      }
    });
  });

  // 2. Build vs Run Mode Switcher
  $('btnBuildMode')?.addEventListener('click', async () => {
    if (state?.running) {
      toast('Cannot switch to Build mode while simulation is running. Pause first.', true);
      return;
    }
    editorMode = 'build';
    $('btnBuildMode').classList.add('active');
    $('btnRunMode').classList.remove('active');
    $('devicesPalette')?.classList.add('build-mode-active');
    applyEditorModeLocks();
    toast('Build mode active: drag, add, and connect devices');
  });

  $('btnRunMode')?.addEventListener('click', () => {
    editorMode = 'run';
    $('btnRunMode').classList.add('active');
    $('btnBuildMode').classList.remove('active');
    $('devicesPalette')?.classList.remove('build-mode-active');
    setMode('select');
    applyEditorModeLocks();
    toast('Run mode active: simulation traffic and self-healing');
  });

  // 3. Play / Pause / Step / Reset
  $('mainStartBtn')?.addEventListener('click', async () => {
    try {
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
      await api('running', { value: true });
      play();
      toast('Traffic streaming Client ➔ Server');
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainPauseBtn')?.addEventListener('click', async () => {
    try {
      stop();
      await api('running', { value: false });
      toast('Simulation paused');
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainStepBtn')?.addEventListener('click', async () => {
    try {
      stop();
      await api('reroute_step');
      toast('Executed single simulation step');
    } catch (e) {
      toast(e.message, true);
    }
  });

  $('mainResetBtn')?.addEventListener('click', async () => {
    try {
      stop();
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

  // Demo Preset Selector
  $('demoPresetSelect')?.addEventListener('change', async (e) => {
    const preset = e.target.value;
    try {
      stop();
      if (preset === 'reroute_six_router') {
        await api('reroute_init');
      } else {
        await api('load_preset', { preset });
      }
      toast(`Loaded preset: ${preset}`);
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
      a.download = 'dhaara-network-topology.json';
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
      await api('import', { document: doc });
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
    }
    scenarioSelect.addEventListener('change', () => {
      localStorage.setItem('reroute_selected_scenario', scenarioSelect.value);
    });
  }

  $('injectFaultBtn')?.addEventListener('click', async () => {
    if (isInjecting) return;
    isInjecting = true;
    const btn = $('injectFaultBtn');
    if (btn) btn.disabled = true;

    try {
      const scn = scenarioSelect ? scenarioSelect.value : 'sudden_link_failure';
      await api('reroute_inject_scenario', { scenario: scn });
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
    if (editorMode === 'run' || state?.running) {
      toast('Cannot add devices in Run mode or while running. Switch to Build mode.', true);
      return;
    }
    try {
      const count = (state?.devices || []).filter(d => d.type === type).length + 1;
      const desiredName = `${prefix}${count}`;
      const prevIds = new Set((state.devices || []).map(d => d.id));
      const res = await api('add_device', {
        type,
        name: desiredName,
        x: 450 + Math.floor(Math.random() * 120),
        y: 250 + Math.floor(Math.random() * 100)
      });
      const created = (res?.state?.devices || state?.devices || []).find(d => !prevIds.has(d.id));
      const actualName = created?.name || created?.id || desiredName;
      toast(`Added ${type} ${actualName}`);
    } catch (e) {
      toast(e.message, true);
    }
  };

  $('paletteAddRouter')?.addEventListener('click', addDeviceHandler('router', 'R'));
  $('paletteAddSwitch')?.addEventListener('click', addDeviceHandler('switch', 'SW'));
  $('paletteAddClient')?.addEventListener('click', addDeviceHandler('pc', 'PC'));
  $('paletteAddServer')?.addEventListener('click', addDeviceHandler('server', 'SRV'));

  $('paletteConnectBtn')?.addEventListener('click', () => {
    if (editorMode === 'run' || state?.running) {
      toast('Connect tool locked in Run mode. Switch to Build mode.', true);
      return;
    }
    setMode(mode === 'connect' ? 'select' : 'connect');
  });

  $('paletteDeleteBtn')?.addEventListener('click', () => {
    if (editorMode === 'run' || state?.running) {
      toast('Delete tool locked in Run mode. Switch to Build mode.', true);
      return;
    }
    setMode(mode === 'delete' ? 'select' : 'delete');
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
    $('btnToolSelect').classList.add('active');
    $('btnToolPan').classList.remove('active');
    isPanning = false;
  });

  $('btnToolPan')?.addEventListener('click', () => {
    $('btnToolPan').classList.add('active');
    $('btnToolSelect').classList.remove('active');
    isPanning = true;
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
        panStart = { x: e.clientX - pan.x, y: e.clientY - pan.y };
        isPanning = true;
      }
    }
  });

  window.addEventListener('pointermove', (e) => {
    if (isPanning && ($('btnToolPan')?.classList.contains('active') || e.buttons === 4 || e.shiftKey)) {
      pan.x = e.clientX - panStart.x;
      pan.y = e.clientY - panStart.y;
      updateCanvasTransform();
    }
    if (connectStart && state) {
      const from = state.devices.find(d => d.id === connectStart);
      const p = canvasPoint(e);
      if (from) connectPreview.setAttribute('d', `M ${from.x} ${from.y} L ${p.x} ${p.y}`);
    }
  });

  window.addEventListener('pointerup', () => {
    if (!$('btnToolPan')?.classList.contains('active')) {
      isPanning = false;
    }
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
        $('dockViewPackets').style.display = 'block';
        $('dockViewPackets').classList.add('active-dock');
      } else if (target === 'events') {
        $('dockViewEvents').style.display = 'block';
        $('dockViewEvents').classList.add('active-dock');
      }
    });
  });

  // 10. Incidents Page Export Report
  $('btnExportIncidentReport')?.addEventListener('click', () => {
    const reportData = {
      title: 'Dhaara Network Incident History',
      timestamp: new Date().toISOString(),
      reroute_metrics: state?.reroute?.metrics,
      diagnosis: state?.reroute?.diagnosis,
      timeline: state?.reroute?.incident_timeline || []
    };
    const blob = new Blob([JSON.stringify(reportData, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `dhaara-incident-report-${Date.now()}.json`;
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
      const resp = await api('reroute_run_comparison', { scenario: scn, offered_packets: 30, seed });
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

      $('cmpOffPdr') && ($('cmpOffPdr').textContent = `${Number(report.auto_heal_off.post_action.delivery_rate_pct || 0).toFixed(1)}%`);
      $('cmpOffLat') && ($('cmpOffLat').textContent = formatLat(report.auto_heal_off.post_action.avg_latency_ms));
      $('cmpOffOutcome') && ($('cmpOffOutcome').textContent = report.auto_heal_off.outcome_label || 'Traffic dropped permanently');

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
    const scn = $('compareScenarioSelect')?.value || 'sudden_link_failure';
    const seed = Number($('compareSeedInput')?.value) || 42;
    try {
      const resp = await api('reroute_run_comparison', { scenario: scn, offered_packets: 30, seed });
      const blob = new Blob([JSON.stringify(resp.state.report_dict, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `dhaara-compare-results-${scn}-seed${seed}.json`;
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
