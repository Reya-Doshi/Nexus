const SVG_NS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);
const canvas = $('networkCanvas');
const linkLayer = $('linkLayer');
const routeLayer = $('routeLayer');
const nodeLayer = $('nodeLayer');
const packetLayer = $('packetLayer');
const connectPreview = $('connectPreview');
const inspectorBody = $('inspectorBody');
let state = null;
let mode = 'select';
let selected = null;
let connectStart = null;
let drag = null;
let seenPacketIds = new Set();
let diagnosticView = false;
let transportView = false;
let servicesView = false;
let studyView = null;   // 'learning' | 'challenges' | 'quiz' | 'demos' | 'overview'
let transportProtocol = 'TCP';
let packetFilter = {status:'all', protocol:'all', traffic_class:'all'};
let playTimer = null;
let zoom = 1;
let origin = { x: 0, y: 0 };

const TYPES = {pc:'PC',laptop:'Laptop',server:'Server',printer:'Printer',router:'Router',switch:'Switch',hub:'Hub',access_point:'Access Point',cloud:'Cloud',internet:'Internet'};
const TYPE_CLASS = {pc:'host',laptop:'host',server:'server',printer:'host',router:'router',switch:'switch',hub:'switch',access_point:'switch',cloud:'cloud',internet:'cloud'};
const COLORS = {Emergency:'#f16c7a',VoIP:'#56d9e8',Video:'#bb9cff',HTTP:'#5ca9ff',FTP:'#f3bf5b'};

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function key(a, b) { return [a,b].sort().join('--'); }
function fmt(n, digits=0) { return Number(n || 0).toFixed(digits); }
function clock(seconds) { const s=Math.max(0,Number(seconds)||0); return `${String(Math.floor(s/60)).padStart(2,'0')}:${(s%60).toFixed(1).padStart(4,'0')}`; }
function toast(message, error=false) { const el=$('toast'); el.textContent=message; el.className=`toast show${error?' error':''}`; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.className='toast',2600); }

async function api(action, payload={}) {
  try {
    const response = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action,...payload})});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Backend request failed');
    if (data.state && Array.isArray(data.state.devices)) { state=data.state; render(); }
    return data;
  } catch (error) { toast(error.message, true); throw error; }
}
async function loadState() { const response=await fetch('/api/state'); state=await response.json(); render(); }

function icon(type) {
  if (type==='router') return '<circle class="device-glyph" cx="0" cy="-2" r="10"/><path class="device-glyph" d="M-10 10h20M-5 6l5 4 5-4M-5 14l5-4 5 4"/>';
  if (type==='switch' || type==='hub') return '<rect class="device-glyph" x="-16" y="-10" width="32" height="20" rx="2"/><path class="device-glyph" d="M-11-3h22M-11 3h22M-12 10v4M-4 10v4M4 10v4M12 10v4"/>';
  if (type==='server') return '<rect class="device-glyph" x="-13" y="-17" width="26" height="34" rx="2"/><path class="device-glyph" d="M-8-9H8M-8 0H8M-8 9H8"/><circle cx="7" cy="-9" r="1.5" fill="#65e6a6"/><circle cx="7" cy="0" r="1.5" fill="#56d9e8"/>';
  if (type==='printer') return '<rect class="device-glyph" x="-15" y="-8" width="30" height="18" rx="2"/><path class="device-glyph" d="M-9-8v-8h18v8M-9 10v7h18v-7"/><path class="device-glyph" d="M-6 2H6"/>';
  if (type==='laptop') return '<rect class="device-glyph" x="-14" y="-14" width="28" height="20" rx="2"/><path class="device-glyph" d="M-19 10h38l-4 5H-15z"/>';
  if (type==='cloud') return '<path class="device-glyph" d="M-22 11c-8 0-10-11-3-14 2-10 15-11 20-4 10-4 20 3 17 12 0 4-4 6-8 6z"/>';
  if (type==='access_point') return '<path class="device-glyph" d="M-18-2c8-9 28-9 36 0M-12 4c5-5 19-5 24 0"/><circle cx="0" cy="11" r="2" fill="#56d9e8"/>';
  return '<rect class="device-glyph" x="-15" y="-14" width="30" height="23" rx="2"/><path class="device-glyph" d="M-8 16h16M-4 9l4 4 4-4"/>';
}
function nodeMarkup(device) {
  const down=device.status==='DOWN';
  return `<g class="node ${TYPE_CLASS[device.type]||'host'} ${down?'down':''} ${selected?.kind==='device'&&selected.id===device.id?'selected':''}" data-id="${esc(device.id)}" transform="translate(${device.x},${device.y})">
    <rect class="selection" x="-34" y="-38" width="68" height="82" rx="7"/><rect class="device-body" x="-30" y="-33" width="60" height="60" rx="8"/>${icon(device.type)}<circle class="port" cx="-25" cy="30" r="2"/><circle class="port" cx="25" cy="30" r="2"/><text class="node-label" y="45">${esc(device.name)}</text><text class="node-type" y="57">${esc(TYPES[device.type]||device.type)}</text><text class="node-status ${down?'down':'up'}" y="68">${down?'DOWN':'ONLINE'}</text></g>`;
}
function linkMarkup(link, active) {
  const source=state.devices.find(d=>d.id===link.source), target=state.devices.find(d=>d.id===link.target);
  if (!source || !target) return '';
  const mx=(source.x+target.x)/2, my=(source.y+target.y)/2;
  const failed=link.status==='DOWN', warning=link.congestion>.3, loss=link.packet_loss>.1;
  const cls=failed?'failed':warning?'warning':loss?'loss':active?'active':'';
  return `<g data-link="${esc(link.id)}" data-source="${esc(link.source)}" data-target="${esc(link.target)}"><line class="link hit" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/><line class="link ${cls}" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/><g class="link-label ${active?'active':''}" transform="translate(${mx},${my})"><rect x="-32" y="-9" width="64" height="18"/><text y="0">${fmt(link.bandwidth)}M · ${Math.round(link.latency)}ms</text></g></g>`;
}
function routeEdges() {
  const edges=new Set();
  for (const flow of state.flows || []) for (let i=0;i<(flow.route||[]).length-1;i++) edges.add(key(flow.route[i],flow.route[i+1]));
  for (const packet of state.packets || []) if (['ACTIVE','PENDING'].includes(packet.status) && packet.route?.length>1) for (let i=0;i<packet.route.length-1;i++) edges.add(key(packet.route[i],packet.route[i+1]));
  return edges;
}
function renderCanvas() {
  const edges=routeEdges();
  linkLayer.innerHTML=(state.links||[]).map(l=>linkMarkup(l,edges.has(key(l.source,l.target)))).join('');
  nodeLayer.innerHTML=(state.devices||[]).map(nodeMarkup).join('');
  $('canvasEmpty').style.display=state.devices?.length?'none':'grid';
  $('gridRect').style.display=$('gridToggle').checked?'block':'none';
  linkLayer.querySelectorAll('[data-link]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (mode === 'delete') {
        api('delete_link', {source: el.dataset.source, target: el.dataset.target});
      } else {
        selected = {kind: 'link', id: el.dataset.source, other: el.dataset.target}; diagnosticView=false; transportView=false; renderInspector();
      }
    });
  });
  nodeLayer.querySelectorAll('[data-id]').forEach(el=>bindNode(el));
  $('backendName').textContent=state.name;
  $('topologyName').textContent=state.name;
  $('clock').textContent=clock(state.time);
  $('statSent').textContent=Math.round(state.metrics.sent); $('statDelivered').textContent=Math.round(state.metrics.delivered); $('statDropped').textContent=Math.round(state.metrics.dropped); $('statPdr').textContent=`${fmt(state.metrics.pdr,1)}%`; $('statLatency').textContent=`${fmt(state.metrics.latency,1)} ms`; $('statRouteChanges').textContent=state.metrics.route_changes;
  const q=state.queue||{}; $('queueLength').textContent=`${Math.round(q.length||0)} packets`; $('queueWait').textContent=`${fmt((q.average_wait||0)*1000,1)} ms average wait`; $('flowCount').textContent=`${state.flows?.length||0} active flows`; const summary=$('simulationSummary'); if(summary) summary.textContent=state.metrics.sent?`${state.running?'SIMULATION RUNNING':'SIMULATION COMPLETE'} · ${Math.round(state.metrics.sent)} sent · ${Math.round(state.metrics.delivered)} delivered · ${Math.round(state.metrics.dropped)} dropped · PDR ${fmt(state.metrics.pdr,1)}% · ${fmt(state.metrics.latency,1)} ms avg`:'Idle · no packets sent'; $('queueBar').style.width=`${Math.min(100,(q.length||0)*10)}%`;
  const packets=state.packets||[];
  if(!packets.length){seenPacketIds.clear();packetLayer.innerHTML='';routeLayer.innerHTML='';}
  packets.forEach(packet=>{const packetKey=String(packet.id);if(!seenPacketIds.has(packetKey)){seenPacketIds.add(packetKey);animatePacket(packet);}});
  renderDiagnosticToolbar();
  $('undoBtn').disabled=!state.can_undo; $('redoBtn').disabled=!state.can_redo;
  $('playBtn').classList.toggle('primary',state.running);
  if($('workspaceSpeed')&&$('workspaceSpeed').value!==String(state.speed??1))$('workspaceSpeed').value=String(state.speed??1);
  if($('speedSelect')&&$('speedSelect').value!==String(state.speed??1))$('speedSelect').value=String(state.speed??1);
}
function renderTimeline() {
  const events=(state.events||[]).slice().reverse();
  $('timeline').innerHTML=events.length?events.map(e=>`<div class="timeline-item" data-kind="${esc(e.event)}" data-event="${esc(JSON.stringify(e))}"><span class="timeline-time">${clock(e.time)}</span><span class="timeline-event">${esc(e.event)}</span><span class="timeline-message">${esc(e.message)}</span></div>`).join(''):'<div class="timeline-empty">No simulation events yet. Start traffic or inject a fault.</div>';
  $('timeline').querySelectorAll('.timeline-item').forEach(el=>el.addEventListener('click',()=>{selected={kind:'event',data:JSON.parse(el.dataset.event)};renderInspector();}));
}
function render() { renderCanvas(); renderTimeline(); renderInspector(); }
function bindNode(el) {
  const id=el.dataset.id;
  el.addEventListener('pointerdown',(e)=>{
    e.stopPropagation();
    if(mode==='delete') { api('delete_device',{id}); return; }
    if(mode==='connect') { if(!connectStart) {connectStart=id;toast(`Connect from ${id}`);} else if(connectStart!==id) { const from=connectStart;connectStart=null;api('add_link',{source:from,target:id}); } return; }
    if(e.button!==0) return;
    const point=canvasPoint(e); drag={id,dx:point.x-state.devices.find(d=>d.id===id).x,dy:point.y-state.devices.find(d=>d.id===id).y,moved:false}; el.setPointerCapture?.(e.pointerId); selected={kind:'device',id}; diagnosticView=false; transportView=false; renderInspector();
  });
  el.addEventListener('pointermove',(e)=>{if(!drag||drag.id!==id)return;const p=canvasPoint(e);let x=p.x-drag.dx,y=p.y-drag.dy;if($('snapToggle').checked){x=Math.round(x/16)*16;y=Math.round(y/16)*16;}drag.moved=true;el.setAttribute('transform',`translate(${x},${y})`);origin={x,y};});
  el.addEventListener('pointerup',async()=>{if(!drag||drag.id!==id)return;const d=drag;drag=null;if(d.moved) await api('move_device',{id,x:origin.x,y:origin.y}); else renderInspector();});
  el.addEventListener('click',(e)=>{e.stopPropagation();if(mode==='select'&&!drag){selected={kind:'device',id};diagnosticView=false;renderInspector();}});
}
function canvasPoint(event) { const point=canvas.createSVGPoint(); point.x=event.clientX;point.y=event.clientY; return point.matrixTransform(canvas.getScreenCTM().inverse()); }
function animatePacket(packet) {
  const route=packet.route||[]; const sourceDevice=state.devices.find(d=>d.id===packet.source); if(!sourceDevice)return; if(packet.status==='DROPPED'||route.length<2){const markerId=`packetDrop-${packet.id}-${Date.now()}`; const reason=packet.drop_reason||packet.reason|| (route.length<2?'NO_ROUTE':'DROPPED'); packetLayer.insertAdjacentHTML('beforeend',`<g id="${markerId}" class="packet dropped" data-packet="${packet.id}" style="color:#f16c7a" transform="translate(${sourceDevice.x},${sourceDevice.y})"><circle class="packet-ring" r="8"/><circle class="packet-core" r="3"/><text class="packet-label" y="-13">P${packet.id} ${esc(reason)}</text></g>`); packetLayer.querySelector(`[data-packet="${CSS.escape(String(packet.id))}"]`)?.addEventListener('click',()=>{selected={kind:'packet',data:packet};diagnosticView=false;transportView=false;renderInspector();}); setTimeout(()=>document.getElementById(markerId)?.remove(),2600); return;} const points=route.map(id=>state.devices.find(d=>d.id===id)).filter(Boolean); if(points.length<2)return;
  const id=`packetPath-${packet.id}-${Date.now()}`; const d=points.map((p,i)=>`${i?'L':'M'} ${p.x} ${p.y}`).join(' '); const trafficClass=packet.traffic_class||packet.traffic_type; const kind=packet.kind||'DATA'; const protocol=packet.protocol||'IP'; const color=packet.status==='DROPPED'?'#f16c7a':protocol==='TCP'?(kind==='ACK'?'#bb9cff':kind==='CONTROL'||kind==='FIN'?'#f3bf5b':'#56d9e8'):protocol==='UDP'?'#5ca9ff':COLORS[trafficClass]||'#56d9e8'; const retransmission=Number(packet.retransmission||0); const transportClass=protocol==='TCP'?`transport tcp-${kind.toLowerCase()}`:protocol==='UDP'?'transport udp-data':''; const stateClass=`${packet.status==='DROPPED'?'dropped ':''}${retransmission?'retransmission':''}`; const label=protocol==='TCP'?`${protocol} ${(packet.flags||[]).join('+')||kind}${retransmission?' ↻'+retransmission:''}`:protocol==='UDP'?`${protocol} DATA`:`P${packet.id}`;
  packetLayer.insertAdjacentHTML('beforeend',`<path id="${id}" d="${d}" fill="none" stroke="none"/><g class="packet ${transportClass} ${stateClass}" data-packet="${packet.id}" style="color:${color}"><circle class="packet-ring" r="8"/><circle class="packet-core" r="3"/><text class="packet-label" y="-13">${esc(label)}</text><animateMotion dur="${Math.max(900,route.length*430)}ms" repeatCount="1" fill="freeze"><mpath href="#${id}"/></animateMotion></g>`);
  packetLayer.querySelector(`[data-packet="${CSS.escape(String(packet.id))}"]`)?.addEventListener('click',()=>{selected={kind:'packet',data:packet};diagnosticView=false;transportView=false;renderInspector();});
  setTimeout(()=>{const el=document.getElementById(id);if(el)el.remove();},Math.max(1000,route.length*430)+300);
}
function renderDiagnosticToolbar(){
  const source=selected?.kind==='device'?selected.id:null;
  $('diagnosticSource').textContent=source||'select a device';
  const select=$('diagnosticDestination');
  const previous=select.value;
  const destinations=(state.devices||[]).filter(device=>device.id!==source);
  select.innerHTML=destinations.length?destinations.map(device=>`<option value="${esc(device.id)}">${esc(device.name)}</option>`).join(''):'<option>select a destination</option>';
  if(destinations.some(device=>device.id===previous))select.value=previous;
}
function eventName(value){return String(value||'').split('.').pop();}
function diagnosticResultInspector(){
  const result=state.diagnostic_result;
  if(!result)return '<div class="inspector-empty"><div>⌁</div><p>Select a device and run a diagnostic quick action.</p></div>';
  if(result.type==='ping'){
    const r=result.rtt_ms||{};
    return `<div class="device-hero"><div class="device-symbol">⌁</div><div><b>${esc(result.title)}</b><span>${result.success?'Reachable':'Unreachable'} · real ICMP simulation</span></div></div><div class="diagnostic-stats">${[['Packets',result.packets],['Sent',result.sent],['Received',result.received],['Lost',result.lost],['Loss',`${fmt(result.loss_percent,1)}%`],['Hops',Math.max(0,(result.path?.length||0)-1)]].map(([label,value])=>`<div class="diagnostic-stat"><b>${esc(value)}</b><span>${label}</span></div>`).join('')}</div><div class="inspector-section"><div class="section-title">RTT</div>${property('Minimum',r.min==null?'—':`${fmt(r.min,2)} ms`)}${property('Average',r.average==null?'—':`${fmt(r.average,2)} ms`)}${property('Maximum',r.max==null?'—':`${fmt(r.max,2)} ms`)}</div><div class="inspector-section"><div class="section-title">Actual path</div><div class="diagnostic-path">${esc((result.path||[]).join(' → ')||'No route')}</div>${result.reason?`<div class="drop-reason">${esc(result.reason)}</div>`:''}<button data-action="open-packets">Inspect ${result.packet_results?.length||0} ping packets</button></div>`;
  }
  if(result.type==='traceroute'){
    return `<div class="device-hero"><div class="device-symbol">↳</div><div><b>${esc(result.title)}</b><span>${result.hops.length} discovered hops</span></div></div><table class="diagnostic-table"><thead><tr><th>#</th><th>Device</th><th>IP</th><th>Response</th></tr></thead><tbody>${result.hops.map(hop=>`<tr><td>${hop.hop}</td><td>${esc(hop.device)}</td><td>${esc(hop.ip||'—')}</td><td>${hop.response_time_ms==null?'—':`${fmt(hop.response_time_ms,2)} ms`}</td></tr>`).join('')}</tbody></table><div class="diagnostic-path">${esc((result.path||[]).join(' → '))}</div><button data-action="open-packets">Inspect traceroute packets</button>`;
  }
  if(result.type==='arp'){
    return `<div class="device-hero"><div class="device-symbol">▤</div><div><b>${esc(result.title)}</b><span>${result.entries.length} dynamic entr${result.entries.length===1?'y':'ies'}</span></div></div>${result.message?`<p class="hint">${esc(result.message)}</p>`:''}<table class="diagnostic-table"><thead><tr><th>IP Address</th><th>MAC Address</th><th>Interface</th><th>State</th></tr></thead><tbody>${result.entries.map(entry=>`<tr><td>${esc(entry.ip_address)}</td><td>${esc(entry.mac_address)}</td><td>${esc(entry.interface_id)}</td><td>${esc(entry.state)}</td></tr>`).join('')||'<tr><td colspan="4">No ARP entries learned.</td></tr>'}</tbody></table><button class="danger" data-action="clear-arp">Clear ARP</button>`;
  }
  if(result.type==='mac'){
    return `<div class="device-hero"><div class="device-symbol">▥</div><div><b>${esc(result.title)}</b><span>${result.entries.length} learned entr${result.entries.length===1?'y':'ies'}</span></div></div>${result.message?`<p class="hint">${esc(result.message)}</p>`:''}<table class="diagnostic-table"><thead><tr><th>MAC Address</th><th>Interface</th><th>Type</th></tr></thead><tbody>${result.entries.map(entry=>`<tr><td>${esc(entry.mac_address)}</td><td>${esc(entry.interface_id)}</td><td>${esc(entry.type)}</td></tr>`).join('')||'<tr><td colspan="3">No MAC addresses learned.</td></tr>'}</tbody></table><button class="danger" data-action="clear-mac">Clear MAC Table</button>`;
  }
  return '<div class="inspector-empty"><div>⌁</div><p>No diagnostic result.</p></div>';
}
function packetListInspector(){
  const packets=state.packets||[];
  const filtered=packets.filter(packet=>{
    const status=packetFilter.status;
    const statusMatch=status==='all'||(status==='active'?['ACTIVE','PENDING'].includes(packet.status):packet.status===status.toUpperCase());
    return statusMatch&&(packetFilter.protocol==='all'||packet.protocol===packetFilter.protocol)&&(packetFilter.traffic_class==='all'||(packet.traffic_class||packet.traffic_type)===packetFilter.traffic_class);
  });
  const statuses=['all','active','delivered','dropped'];
  const classes=[...new Set(packets.map(packet=>packet.traffic_class||packet.traffic_type).filter(Boolean))];
  return `<div class="device-hero"><div class="device-symbol">◎</div><div><b>Packet Inspector</b><span>${filtered.length} of ${packets.length} engine packets</span></div></div><div class="packet-filter-row"><select data-packet-filter="status">${statuses.map(value=>`<option value="${value}" ${packetFilter.status===value?'selected':''}>${value[0].toUpperCase()+value.slice(1)}</option>`).join('')}</select><select data-packet-filter="protocol"><option value="all">All protocols</option><option value="ICMP" ${packetFilter.protocol==='ICMP'?'selected':''}>ICMP</option><option value="IP" ${packetFilter.protocol==='IP'?'selected':''}>IP</option></select></div><select data-packet-filter="traffic_class" style="width:100%;margin-bottom:8px"><option value="all">All traffic classes</option>${classes.map(value=>`<option ${packetFilter.traffic_class===value?'selected':''}>${esc(value)}</option>`).join('')}</select>${filtered.map(packet=>`<div class="packet-row" data-packet-id="${esc(packet.id)}"><b>#${esc(packet.id)} · ${esc(packet.source)} → ${esc(packet.destination)}</b><span>${esc(packet.protocol)} · ${esc(packet.traffic_class||packet.traffic_type)} · ${esc(packet.status)}</span></div>`).join('')||'<p class="hint">No packets match these filters.</p>'}`;
}
function bindPacketList(){
  inspectorBody.querySelectorAll('[data-packet-filter]').forEach(input=>input.addEventListener('change',()=>{packetFilter[input.dataset.packetFilter]=input.value;diagnosticView=true;renderInspector();}));
  inspectorBody.querySelectorAll('[data-packet-id]').forEach(row=>row.addEventListener('click',()=>{const packet=(state.packets||[]).find(item=>String(item.id)===row.dataset.packetId);if(packet){selected={kind:'packet',data:packet};diagnosticView=false;transportView=false;renderInspector();}}));
}
function property(label,value,html=false){return `<div class="property"><label>${esc(label)}</label>${html?value:`<span>${esc(value)}</span>`}</div>`;}
function deviceInspector(device) {
  const down=device.status==='DOWN';
  return `<div class="device-hero"><div class="device-symbol">${device.type==='router'?'◉':device.type==='switch'?'▥':'🖥'}</div><div><b>${esc(device.name)}</b><span>${esc(TYPES[device.type]||device.type)} · ${device.interfaces.length} interface${device.interfaces.length===1?'':'s'}</span></div></div>
  <div class="inspector-section"><div class="section-title">Identity <span class="status-badge ${down?'status-down':'status-up'}">${down?'DOWN':'ONLINE'}</span></div><input id="renameInput" value="${esc(device.name)}" style="width:100%;margin-bottom:8px"/><div class="button-row"><button data-action="rename">Rename</button><button data-action="duplicate">Duplicate</button><button class="danger" data-action="delete-device">Delete</button></div></div>
  <div class="inspector-section"><div class="section-title">Interfaces <span>${device.interfaces.length}</span></div>${device.interfaces.map(i=>`<div class="interface-card"><div class="interface-head"><b>${esc(i.interface_id)}</b><span class="status-badge ${i.status==='UP'?'status-up':'status-down'}">${i.status}</span></div><div class="interface-fields"><input data-field="ip_address" value="${esc(i.ip_address||'')}" placeholder="IP address"/><input data-field="prefix" type="number" min="1" max="32" value="${i.prefix}"/><input data-field="mac_address" value="${esc(i.mac_address||'')}" placeholder="MAC address"/><select data-field="status"><option ${i.status==='UP'?'selected':''}>UP</option><option ${i.status==='DOWN'?'selected':''}>DOWN</option></select></div><div class="interface-actions"><span>${esc(i.network_address||'unassigned')}</span><button data-action="apply-interface" data-interface="${esc(i.interface_id)}">Apply</button></div></div>`).join('')}</div>
  <div class="inspector-section"><div class="section-title">Device actions</div><div class="button-row">${down?'<button data-action="restart">Restart device</button>':'<button class="danger" data-action="shutdown">Shutdown device</button>'}<button data-action="console">Open console</button></div></div>
  <div class="inspector-section"><div class="section-title">Traffic generator</div><label>Protocol<select id="trafficProtocol"><option value="IP">IP traffic flow</option><option value="ICMP">ICMP Echo</option><option value="TCP">TCP data</option><option value="UDP">UDP datagrams</option></select></label><div class="traffic-form"><label>Destination<select id="trafficDestination">${state.devices.filter(d=>d.id!==device.id&&d.status==='UP').map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label><div class="two"><label>Class<select id="trafficType"><option>Emergency</option><option>VoIP</option><option selected>Video</option><option>HTTP</option><option>FTP</option></select></label><label>Packets<input id="trafficCount" type="number" min="1" value="12"/></label></div><div class="two"><label>Rate (pps)<input id="trafficPps" type="number" min=".1" value="5"/></label><label>Size (B)<input id="trafficSize" type="number" min="64" value="1500"/></label></div><button data-action="start-traffic" data-source="${esc(device.id)}">Start traffic</button></div></div>
  ${device.type==='router'?routerTable(device):device.type==='switch'?switchTable(device):serverInfo(device)}`;
}
function routerTable(device){const rows=state.routing_tables?.[device.id]||[];return `<div class="inspector-section"><div class="section-title">Routing table <span>${rows.length} routes</span></div>${rows.length?rows.map(r=>`<div class="interface-card"><b>${esc(r.destination_network)}/${r.prefix}</b><div>via ${esc(r.next_hop||'DIRECT')} · ${esc(r.outgoing_interface)} · metric ${fmt(r.metric,1)}</div></div>`).join(''):'<p class="hint">No route entries available.</p>'}</div>`;}
function switchTable(device){const rows=Object.entries(device.mac_table||{});return `<div class="inspector-section"><div class="section-title">MAC address table <span>${rows.length}</span></div>${rows.length?rows.map(([mac,port])=>`<div class="interface-card"><b>${esc(mac)}</b><div>${esc(port)}</div></div>`).join(''):'<p class="hint">No MACs learned.</p>'}</div>`;}
function serverInfo(device){return `<div class="inspector-section"><div class="section-title">Services</div><p class="hint">No application services configured. This server can carry simulator traffic.</p></div>`;}
function linkInspector(link){return `<div class="device-hero"><div class="device-symbol">⌁</div><div><b>${esc(link.source)} ↔ ${esc(link.target)}</b><span>Link inspector · ${esc(link.duplex)} duplex</span></div></div><div class="inspector-section"><div class="section-title">Link conditions <span class="link-state ${link.status==='UP'?'status-up':'status-down'}">${link.status}</span></div>${property('Latency (ms)',link.latency)}<input id="linkLatency" type="number" value="${link.latency}"/><br>${property('Bandwidth (Mbps)',link.bandwidth)}<input id="linkBandwidth" type="number" value="${link.bandwidth}"/><br>${property('Packet loss (%)',fmt(link.packet_loss*100,1))}<input id="linkLoss" type="number" min="0" max="100" step="1" value="${fmt(link.packet_loss*100,1)}"/><br>${property('Congestion (%)',fmt(link.congestion*100,1))}<input id="linkCongestion" type="number" min="0" max="100" step="1" value="${fmt(link.congestion*100,1)}"/><div class="button-row"><button data-action="apply-link">Apply conditions</button><button class="danger" data-action="toggle-link">${link.status==='UP'?'Fail link':'Recover link'}</button><button data-action="delete-link">Delete</button></div></div><div class="inspector-section"><div class="section-title">Interfaces</div>${property('Interface A',link.interface_a)}${property('Interface B',link.interface_b)}</div>`;}
function packetInspector(packet){
  const trafficClass=packet.traffic_class||packet.traffic_type;
  const journeyData=(state.journey&&state.journey.packet_id===packet.id)?state.journey:null;
  const explanation=state.explanation||null;
  const journey=packet.journey||packet.events||[];
  const dropReason=packet.drop_reason||packet.reason;
  const transport=packet.protocol==='TCP'||packet.protocol==='UDP';
  const queueWait=packet.queue_wait_ms??(packet.queue_wait_time==null?null:packet.queue_wait_time*1000);
  return `<div class="device-hero"><div class="device-symbol" style="color:${COLORS[trafficClass]||'#56d9e8'}">●</div><div><b>Packet #${esc(packet.id)}</b><span>${esc(packet.status)} · ${esc(trafficClass)}</span></div></div><div class="inspector-section">${property('Source',packet.source)}${property('Destination',packet.destination)}${property('Protocol',packet.protocol||'IP')}${property('Size',`${packet.size} bytes`)}${property('TTL',packet.ttl)}${property('Traffic class',trafficClass)}${transport?property('Source port',packet.source_port):''}${transport?property('Destination port',packet.destination_port):''}${transport?property('Sequence number',packet.sequence_number??'—'):''}${transport?property('Acknowledgement number',packet.acknowledgement_number??'—'):''}${transport?property('Flags',(packet.flags||[]).join(' ')||'—'):''}${transport?property('Window size',packet.window_size??'—'):''}${transport?property('Retransmission',packet.retransmission??0):''}${property('Queue wait',queueWait==null?'—':`${fmt(queueWait,2)} ms`)}${property('Current device',packet.current_device||'—')}${property('Next hop',packet.next_hop||'—')}${property('Status',packet.status)}${property('Flow ID',packet.flow_id||'diagnostic')}${property('Route',(packet.route||[]).join(' → ')||'No route')}</div>${packet.service?`<div class="inspector-section"><div class="section-title">SERVICE <span>${esc(packet.service.service||'')}</span></div>${property('Message',packet.service.message||'—')}${packet.service.request?property('Request',packet.service.request):''}${packet.service.method?property('Method',packet.service.method):''}${packet.service.path?property('Path',packet.service.path):''}${packet.service.query?property('Query',packet.service.query):''}${packet.service.response?property('Response',packet.service.response):''}${packet.service.assigned_ip?property('Assigned IP',packet.service.assigned_ip):''}${packet.service.offered_ip?property('Offered IP',packet.service.offered_ip):''}${packet.service.filename?property('File',packet.service.filename):''}${packet.service.bytes?property('Bytes',packet.service.bytes):''}</div>`:''}${packet.security?`<div class="inspector-section"><div class="section-title">SECURITY <span>${esc(packet.security.decision||'ALLOW')}</span></div>${property('Firewall',packet.security.firewall||'—')}${packet.security.firewall_rule?property('Firewall rule',packet.security.firewall_rule):''}${property('ACL',packet.security.acl||'—')}${packet.security.acl_name?property('ACL name',packet.security.acl_name):''}${property('ARP',packet.security.arp||'NORMAL')}${property('Traffic',packet.security.traffic||'NORMAL')}${property('Drop reason',packet.security.reason||'—')}</div>`:''}${journeyData?`<div class="inspector-section"><div class="section-title">PACKET JOURNEY <span>${esc(journeyData.status)}</span></div><div class="diagnostic-path">${esc((journeyData.route||[]).join(' ↓ '))}</div>${(journeyData.hops||[]).map(hop=>`<div class="journey-step"><b>${esc(hop.device)} · ${esc(hop.action)}</b><div>interface ${esc(hop.interface)} · queue ${hop.queue_length==null?'—':hop.queue_length} · ${hop.latency_ms==null?'':fmt(hop.latency_ms,2)+' ms'}</div><span class="hint">${esc((hop.events||[]).join(', ')||hop.result)}</span></div>`).join('')}</div>`:''}${explanation?`<div class="inspector-section"><div class="section-title">WHY?</div>${explanation.failed?`<div class="drop-reason">${esc(explanation.reason)}</div><p>${esc(explanation.explanation)}</p><div class="console-output">${esc(JSON.stringify(explanation.details,null,1))}</div>`:'<p class="hint">This packet was not dropped.</p>'}</div>`:''}${dropReason?`<div class="drop-reason">DROP · ${esc(dropReason)}</div>`:''}<div class="inspector-section"><div class="section-title">PACKET JOURNEY <span>${journey.length} events</span></div>${journey.map((event,index)=>`<div class="journey-step"><b>${index+1}. ${esc(eventName(event.event))}</b><div>${esc(event.message)}</div><span class="hint">${clock(event.time)} · ${esc(event.component||'simulator')}</span></div>`).join('')||'<p class="hint">No packet events recorded.</p>'}</div><button data-action="back-packets">Back to packet viewer</button>`;
}
function eventInspector(event){return `<div class="device-hero"><div class="device-symbol">!</div><div><b>${esc(event.event)}</b><span>${clock(event.time)} simulation time</span></div></div><div class="console-output">${esc(JSON.stringify(event,null,2))}</div>`;}
function transportLabInspector(){
  const flows=state.transport?.flows||[];
  const source=selected?.kind==='device'?selected.id:(flows[0]?.source||state.devices?.[0]?.id||'');
  const connections=state.transport?.connections||[],udp=state.transport?.udp_flows||[];
  const protocol=transportProtocol;
  const active=(protocol==='TCP'?connections[0]:udp[0])||null;
  const stats=state.transport?.statistics||{};
  const protocolStats=active||stats[protocol]||{};
  const destination=active?.destination||(state.devices||[]).find(device=>device.id!==source)?.id||'';
  const destinationOptions=(state.devices||[]).filter(device=>device.id!==source).map(device=>`<option value="${esc(device.id)}" ${device.id===destination?'selected':''}>${esc(device.name)}</option>`).join('');
  const sourceOptions=(state.devices||[]).map(device=>`<option value="${esc(device.id)}" ${device.id===source?'selected':''}>${esc(device.name)}</option>`).join('');
  const stateOrder=['CLOSED','SYN_SENT','SYN_RECEIVED','ESTABLISHED','FIN_WAIT','CLOSE_WAIT','LAST_ACK','TIME_WAIT','CLOSED'];
  const currentState=protocol==='TCP'?(active?.state||'CLOSED'):'CONNECTIONLESS';
  const comparison=state.transport?.result;
  const comparisonHtml=comparison?.results?.length?`<table class="diagnostic-table"><thead><tr><th>Protocol</th><th>Latency</th><th>PDR</th><th>Retrans.</th><th>Setup</th></tr></thead><tbody>${comparison.results.map(row=>`<tr><td>${esc(row.protocol)}</td><td>${fmt((row.average_latency||0)*1000,1)} ms</td><td>${fmt(row.data_packet_delivery_ratio??row.packet_delivery_ratio??0,1)}%</td><td>${fmt(row.retransmissions??row.retransmission_count??0,0)}</td><td>${row.connection_setup_time==null?'—':`${fmt(row.connection_setup_time*1000,1)} ms`}</td></tr>`).join('')}</tbody></table>`:'';
  const loss=100-(protocolStats.data_packet_delivery_ratio??protocolStats.packet_delivery_ratio??0);
  const metrics=[
    ['Data packets',protocolStats.data_packets_sent??protocolStats.packets_sent??0],
    ['Delivered',protocolStats.data_packets_delivered??protocolStats.packets_delivered??0],
    ['Packet loss',`${fmt(loss,1)}%`],
    ['Throughput',`${fmt(protocolStats.throughput||0,1)} B/s`],
    ['RTT',protocol==='TCP'?(active?.average_rtt_ms==null?'—':`${fmt(active.average_rtt_ms,1)} ms`):'N/A'],
    ['CWND',protocol==='TCP'?(active?.cwnd==null?'—':fmt(active.cwnd,1)):'N/A'],
    ['SSTHRESH',protocol==='TCP'?(active?.ssthresh==null?'—':fmt(active.ssthresh,1)):'N/A'],
    ['In flight',protocol==='TCP'?(active?.packets_in_flight??0):'N/A'],
    ['ACKs',protocol==='TCP'?(active?.ack_count??0):'N/A'],
    ['Retransmissions',protocol==='TCP'?(active?.retransmission_count??0):0],
    ['Timeouts',protocol==='TCP'?(active?.timeout_count??0):0],
    ['Bytes',protocolStats.bytes_transferred??0],
  ];
  return `<div class="device-hero"><div class="device-symbol">⇄</div><div><b>Transport Lab</b><span>TCP / UDP over the live topology</span></div></div><div class="inspector-section"><div class="section-title">Transport setup</div><label>Protocol<select id="transportProtocol"><option ${protocol==='TCP'?'selected':''}>TCP</option><option ${protocol==='UDP'?'selected':''}>UDP</option></select></label><label>Source<select id="transportSource">${sourceOptions}</select></label><label>Destination<select id="transportDestination">${destinationOptions}</select></label><div class="two"><label>Source port<input id="transportSourcePort" type="number" min="1" max="65535" value="${active?.source_port||5000}"/></label><label>Destination port<input id="transportDestinationPort" type="number" min="1" max="65535" value="${active?.destination_port||8080}"/></label></div><label>Payload (KB)<input id="transportPayload" type="number" min="1" value="${Math.max(1,Math.round((active?.packets?.find(p=>p.kind==='DATA')?.payload_size||1000)/1024))}"/></label><div id="tcpTransportSettings" class="${protocol==='UDP'?'hidden':''}"><div class="two"><label>Initial CWND<input id="transportCwnd" type="number" min="1" value="${active?.initial_cwnd||1}"/></label><label>Receiver window<input id="transportReceiverWindow" type="number" min="1" value="${active?.receiver_window||8}"/></label></div><div class="two"><label>SSTHRESH<input id="transportSsthresh" type="number" min="1" value="${active?.ssthresh||16}"/></label><label>Timeout (ms)<input id="transportTimeout" type="number" min="1" value="${Math.round((active?.timeout||.1)*1000)}"/></label></div></div><div class="button-row"><button data-transport-action="create">${active?'Start Connection':'Start Flow'}</button><button data-transport-action="send">Send Data</button><button data-transport-action="close" ${protocol==='TCP'?'':'disabled'}>Close</button><button data-transport-action="stop">Stop</button><button data-transport-action="tick">Advance 100 ms</button><button data-transport-action="reset">Reset</button></div></div><div class="inspector-section"><div class="section-title">TCP state</div><div class="transport-state-machine">${stateOrder.map(item=>`<span class="tcp-state ${item===currentState?'current':''}">${item.replaceAll('_',' ')}</span>`).join('<i>→</i>')}</div></div><div class="inspector-section"><div class="section-title">Live ${esc(protocol)} metrics</div><div class="diagnostic-stats">${metrics.map(([label,value])=>`<div class="diagnostic-stat"><b>${esc(value)}</b><span>${label}</span></div>`).join('')}</div></div><div class="inspector-section"><div class="section-title">Active transport flows</div>${(connections.length+udp.length)?[...connections,...udp].map(flow=>`<div class="interface-card"><b>${esc(flow.flow_id)} · ${esc(flow.source)}:${flow.source_port} → ${esc(flow.destination)}:${flow.destination_port}</b><div>${esc(flow.state)} · ${esc(flow.traffic_class)} · ${fmt(flow.packet_delivery_ratio??100,1)}% delivered</div></div>`).join(''):'<p class="hint">No transport flow.</p>'}</div><div class="inspector-section"><div class="section-title">TCP vs UDP comparison</div><button data-transport-action="compare">Run deterministic comparison</button>${comparisonHtml}<div class="hint">Cloned topology, identical workload, network conditions, scheduler, and seed.</div></div>`;
}
function servicesLabInspector(){
  const services=state.services||{},security=state.security||{};
  const registry=services.registry||[],status=services.status||[],result=services.result;
  const firewall=security.firewall||{rules:[],active:false},acls=security.access_lists||[],arp=security.arp||{},flood=security.flood||{},smetrics=security.metrics||{},tmetrics=services.metrics||{};
  const devices=state.devices||[];
  const selectedDevice=selected?.kind==='device'?selected.id:devices[0]?.id||'';
  const options=(exclude)=>devices.filter(d=>d.id!==exclude).map(d=>`<option value="${esc(d.id)}" ${d.id===exclude?'disabled':''}>${esc(d.name)}</option>`).join('');
  const deviceOptions=devices.map(d=>`<option value="${esc(d.id)}" ${d.id===selectedDevice?'selected':''}>${esc(d.name)}</option>`).join('');
  const nameOptions=['DHCP','DNS','HTTP','FTP','SMTP'].map(n=>`<option>${n}</option>`).join('');
  const serviceRows=status.map(row=>`<div class="interface-card"><b>${esc(row.name)} · :${row.port}/${esc(row.protocol)}</b><div>${esc(row.device)} · ${esc(row.state)}${row.available?'':' · '+esc(row.unavailable_reason||'')} · QoS ${esc(row.traffic_class)} · ${row.requests} requests / ${row.failures} failed</div><div class="button-row"><button data-svc-action="start" data-service="${esc(row.name)}" data-id="${esc(row.device)}">Start</button><button data-svc-action="stop" data-service="${esc(row.name)}" data-id="${esc(row.device)}">Stop</button><button data-svc-action="restart" data-service="${esc(row.name)}" data-id="${esc(row.device)}">Restart</button></div></div>`).join('');
  const ruleRows=(firewall.rules||[]).map(rule=>`<div class="interface-card"><b>rule ${rule.rule_id} · ${esc(rule.action)}</b><div>${esc(rule.source_ip)} → ${esc(rule.destination_ip)} · ${esc(rule.protocol||'any')} · dport ${rule.destination_port||'any'} · ${esc(rule.reason)} · matched ${rule.packets_matched}</div><button class="danger" data-svc-action="remove-rule" data-rule="${rule.rule_id}">Remove</button></div>`).join('');
  const aclRows=acls.map(acl=>`<div class="interface-card"><b>${esc(acl.name)} (${esc(acl.type)})</b><div>${acl.entries.map(e=>`${e.sequence} ${e.action} ${e.source_ip} ${e.protocol||'any'} ${e.destination_ip} ${e.destination_port||''}`).join('<br>')||'no entries'}</div><div>applied: ${acl.attachments.map(a=>`${esc(a.device)} ${esc(a.interface_id)}`).join(', ')||'not attached'}</div></div>`).join('');
  const conflictRows=(arp.conflicts||[]).map(c=>`<div class="interface-card"><b>CONFLICT ${esc(c.ip_address)}</b><div>${esc(c.known_mac)} → ${esc(c.new_mac)} on ${esc(c.device)} · severity ${esc(c.severity)}</div></div>`).join('');
  const floodRows=(flood.scenarios||[]).map(s=>`<div class="interface-card"><b>Flood #${s.scenario_id} ${esc(s.protocol)}</b><div>${esc(s.attacker)} → ${esc(s.target)} · ${fmt(s.rate,0)} pps · generated ${s.packets_generated} · received ${s.packets_received} · dropped ${s.packets_dropped} · ${s.detected?'DETECTED':'below threshold'}</div></div>`).join('');
  const serviceStats=Object.entries(tmetrics).map(([name,m])=>`<div class="diagnostic-stat"><b>${fmt(Object.values(m).filter(v=>typeof v==='number').reduce((a,b)=>a+b,0),0)}</b><span>${esc(name)}</span></div>`).join('');
  return `<div class="device-hero"><div class="device-symbol">◈</div><div><b>Services &amp; Security</b><span>Simulated DHCP/DNS/HTTP/FTP/SMTP + firewall, ACL, ARP and flood labs</span></div></div>
<div class="inspector-section"><div class="section-title">Services</div><label>Device<select id="svcDevice">${deviceOptions}</select></label><div class="two"><label>Service<select id="svcName">${nameOptions}</select></label><label>Action<div class="button-row"><button data-svc-action="install">Install</button><button data-svc-action="start-selected">Start</button><button data-svc-action="stop-selected">Stop</button><button data-svc-action="restart-selected">Restart</button></div></label></div>${serviceRows||'<p class="hint">No services installed. Pick a device and service, then Install.</p>'}<button class="danger" data-svc-action="remove-selected">Remove selected</button></div>
<div class="inspector-section"><div class="section-title">DHCP</div><div class="two"><label>Client<select id="dhcpClient">${options()}</select></label><label>Server<select id="dhcpServer">${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label></div><div class="button-row"><button data-svc-action="dhcp-acquire">Discover/Offer/Request/ACK</button><button data-svc-action="dhcp-renew">Renew</button><button data-svc-action="dhcp-release">Release</button></div>${result?.service==='DHCP'?`<div class="console-output">${esc(JSON.stringify({steps:result.steps,address:result.address,gateway:result.gateway,dns:result.dns,lease_time:result.lease_time,reason:result.reason},null,1))}</div>`:''}</div>
<div class="inspector-section"><div class="section-title">DNS</div><div class="two"><label>Client<select id="dnsClient">${options()}</select></label><label>Server<select id="dnsServer">${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label></div><label>Hostname<input id="dnsHostname" value="server.netadapt.local"/></label><div class="button-row"><button data-svc-action="dns-query">nslookup</button><button data-svc-action="dns-record">Add A record</button><button data-svc-action="dns-record-address">Record IP</button></div>${result?.service==='DNS'?`<div class="console-output">${esc(result.hostname)} → ${esc(result.address||result.reason)} (${esc(result.source||'ERROR')})</div>`:''}<div class="hint">${(services.dns?.records||[]).map(r=>`${esc(r.hostname)} → ${esc(r.address)} (TTL ${fmt(r.ttl,0)}s) on ${esc(r.server)}`).join('<br>')||'No A records'}</div></div>
<div class="inspector-section"><div class="section-title">HTTP</div><div class="two"><label>Client<select id="httpClient">${options()}</select></label><label>Server<select id="httpServer">${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label></div><div class="two"><label>Method<select id="httpMethod"><option>GET</option><option>POST</option></select></label><label>Path<input id="httpPath" value="/"/></label></div><label>Body<input id="httpBody" value=""/></label><button data-svc-action="http-request">Send request</button>${result?.service==='HTTP'?`<div class="console-output">${esc(result.response||result.reason)}<br>${fmt((result.latency||0)*1000,2)} ms</div>`:''}</div>
<div class="inspector-section"><div class="section-title">FTP (in-memory file store)</div><div class="two"><label>Client<select id="ftpClient">${options()}</select></label><label>Server<select id="ftpServer">${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label></div><div class="two"><label>Command<select id="ftpCommand"><option>LIST</option><option>GET</option><option>PUT</option><option>CONNECT</option></select></label><label>Filename<input id="ftpFile" value="readme.txt"/></label></div><label>Content<input id="ftpContent" value="uploaded inside the simulator"/></label><button data-svc-action="ftp-command">Run command</button>${result?.service==='FTP'?`<div class="console-output">${esc(result.response||result.reason)}<br>${result.listing?esc(result.listing.join(', ')):''}</div>`:''}</div>
<div class="inspector-section"><div class="section-title">SMTP</div><div class="two"><label>Client<select id="smtpClient">${options()}</select></label><label>Server<select id="smtpServer">${devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('')}</select></label></div><div class="two"><label>MAIL FROM<input id="smtpSender" value="student@netadapt.local"/></label><label>RCPT TO<input id="smtpRecipient" value="server@netadapt.local"/></label></div><button data-svc-action="smtp-send">Send message</button>${result?.service==='SMTP'?`<div class="console-output">${esc(result.transcript_text||result.reason||'sent')}</div>`:''}</div>
<div class="inspector-section"><div class="section-title">Firewall</div><div class="two"><label>Action<select id="fwAction"><option>DENY</option><option>ALLOW</option></select></label><label>Protocol<select id="fwProtocol"><option>TCP</option><option>UDP</option><option value="">any</option></select></label></div><div class="two"><label>Source IP<input id="fwSource" value="any"/></label><label>Destination IP<input id="fwDestination" value="any"/></label></div><div class="two"><label>Source port<input id="fwSourcePort" value=""/></label><label>Destination port<input id="fwDestinationPort" value="80"/></label></div><div class="button-row"><button data-svc-action="firewall-add">Add rule</button><button data-svc-action="port-filter">Add port filter</button><button class="danger" data-svc-action="firewall-clear">Clear</button></div>${ruleRows||'<p class="hint">No firewall rules.</p>'}</div>
<div class="inspector-section"><div class="section-title">Access Control Lists</div><div class="two"><label>Name<input id="aclName" value="BLOCK-WEB"/></label><label>Type<select id="aclType"><option>EXTENDED</option><option>STANDARD</option></select></label></div><div class="two"><label>Action<select id="aclAction"><option>DENY</option><option>ALLOW</option></select></label><label>Destination port<input id="aclPort" value="80"/></label></div><div class="two"><label>Source IP<input id="aclSource" value="192.168.1.1"/></label><label>Attach to<select id="aclDevice">${deviceOptions}</select></label></div><div class="button-row"><button data-svc-action="acl-create">Create + entry</button><button data-svc-action="acl-attach">Attach to interface</button></div>${aclRows||'<p class="hint">No access lists.</p>'}</div>
<div class="inspector-section"><div class="section-title">ARP Protection</div><div class="button-row"><button data-svc-action="toggle-arp-detection">${arp.detection_enabled?'Disable':'Enable'} detection</button><button data-svc-action="toggle-arp-protection">${arp.protection_enabled?'Disable':'Enable'} protection</button></div><div class="two"><label>Attacker<select id="arpAttacker">${options()}</select></label><label>Victim<select id="arpVictim">${options()}</select></label></div><button class="danger" data-svc-action="arp-spoof">Run ARP spoofing scenario</button>${conflictRows||`<p class="hint">${arp.spoof_attempts||0} spoof attempts, ${(arp.conflicts||[]).length} conflicts. Simulation only.</p>`}</div>
<div class="inspector-section"><div class="section-title">Flood Detection</div><div class="two"><label>Threshold (pps)<input id="floodThreshold" type="number" value="${fmt(flood.threshold||100,0)}"/></label><label>Protocol<select id="floodProtocol"><option>UDP</option><option>TCP</option><option>ICMP</option></select></label></div><div class="two"><label>Rate (pps)<input id="floodRate" type="number" value="200"/></label><label>Duration (s)<input id="floodDuration" type="number" step="0.5" value="1"/></label></div><div class="button-row"><button class="danger" data-svc-action="flood-start">Start controlled flood</button><button data-svc-action="toggle-flood-protection">${flood.protection_enabled?'Disable':'Enable'} protection</button></div>${floodRows||'<p class="hint">No flood scenarios run yet.</p>'}</div>
<div class="inspector-section"><div class="section-title">Security metrics</div><div class="diagnostic-stats">${[['Inspected',smetrics.packets_inspected],['Allowed',smetrics.packets_allowed],['Blocked',smetrics.packets_blocked],['Firewall blocks',smetrics.firewall_blocks],['Port blocks',smetrics.port_blocks],['ACL blocks',smetrics.acl_blocks],['ARP conflicts',smetrics.arp_conflicts],['Spoof attempts',smetrics.spoof_attempts],['Floods detected',smetrics.floods_detected],['Attack drops',smetrics.attack_packets_dropped],['Normal',smetrics.normal_packets],['Suspicious',smetrics.suspicious_packets]].map(([label,value])=>`<div class="diagnostic-stat"><b>${fmt(value,0)}</b><span>${label}</span></div>`).join('')}</div></div>
<div class="inspector-section"><div class="section-title">Service activity</div><div class="diagnostic-stats">${serviceStats}</div><div class="hint">All traffic above uses the existing routing, QoS, failure, and event systems. Attacks stay inside the simulator.</div></div>`;
}
function bindServicesLab(){
  inspectorBody.querySelectorAll('[data-svc-action]').forEach(button=>button.addEventListener('click',async()=>{
    const action=button.dataset.svcAction;
    const value=(id,fallback='')=>$(id)?.value||fallback;
    const int=(id)=>Number(value(id,'0'))||null;
    const device=value('svcDevice');
    const serviceName=value('svcName');
    try{
      if(action==='install')await api('service_install',{name:serviceName,id:device,config:{}});
      if(['start','stop','restart'].includes(action))await api('service_control',{control:action,name:button.dataset.service,id:button.dataset.id});
      if(action==='start-selected')await api('service_control',{control:'start',name:serviceName,id:device});
      if(action==='stop-selected')await api('service_control',{control:'stop',name:serviceName,id:device});
      if(action==='restart-selected')await api('service_control',{control:'restart',name:serviceName,id:device});
      if(action==='remove-selected')await api('service_remove',{name:serviceName,id:device});
      if(action==='dhcp-acquire')await api('dhcp_acquire',{client:value('dhcpClient'),server:value('dhcpServer')});
      if(action==='dhcp-renew')await api('dhcp_renew',{client:value('dhcpClient'),server:value('dhcpServer')});
      if(action==='dhcp-release')await api('dhcp_release',{client:value('dhcpClient')});
      if(action==='dns-query')await api('dns_query',{client:value('dnsClient'),hostname:value('dnsHostname'),server:value('dnsServer')});
      if(action==='dns-record'||action==='dns-record-address')await api('dns_record',{id:value('dnsServer'),hostname:value('dnsHostname'),address:value('httpPath')&&(action==='dns-record-address'?value('httpPath'):'192.168.1.3'),ttl:300});
      if(action==='http-request')await api('http_request',{client:value('httpClient'),server:value('httpServer'),method:value('httpMethod','GET'),path:value('httpPath','/'),body:value('httpBody')});
      if(action==='ftp-command')await api('ftp_command',{client:value('ftpClient'),server:value('ftpServer'),command:value('ftpCommand','LIST'),filename:value('ftpFile'),content:value('ftpContent')});
      if(action==='smtp-send')await api('smtp_send',{client:value('smtpClient'),server:value('smtpServer'),sender:value('smtpSender'),recipient:value('smtpRecipient'),subject:'Stage 10 lab message',body:'Sent from the NetAdapt simulated SMTP service.'});
      if(action==='firewall-add'||action==='port-filter')await api('firewall_add_rule',{rule:{action:value('fwAction','DENY'),protocol:value('fwProtocol')||null,source_ip:value('fwSource','any'),destination_ip:value('fwDestination','any'),source_port:int('fwSourcePort'),destination_port:int('fwDestinationPort'),port_filter:action==='port-filter'}});
      if(action==='remove-rule')await api('firewall_remove_rule',{rule_id:Number(button.dataset.rule)});
      if(action==='firewall-clear')await api('firewall_clear',{});
      if(action==='acl-create')await api('acl_create',{name:value('aclName'),type:value('aclType','EXTENDED')}).then(()=>api('acl_add_entry',{name:value('aclName'),entry:{action:value('aclAction','DENY'),source_ip:value('aclSource','any'),protocol:value('fwProtocol')||null,destination_ip:value('fwDestination','any'),destination_port:int('aclPort')}}));
      if(action==='acl-attach')await api('acl_attach',{name:value('aclName'),id:value('aclDevice'),interface_id:'eth0'});
      if(action==='toggle-arp-detection')await api('security_configure',{values:{arp_detection:!(state.security?.arp?.detection_enabled)}});
      if(action==='toggle-arp-protection')await api('security_configure',{values:{arp_protection:!(state.security?.arp?.protection_enabled)}});
      if(action==='toggle-flood-protection')await api('security_configure',{values:{flood_protection:!(state.security?.flood?.protection_enabled)}});
      if(action==='arp-spoof')await api('arp_spoof',{attacker:value('arpAttacker'),victim:value('arpVictim'),detect:true});
      if(action==='flood-start')await api('flood_start',{attacker:value('arpAttacker'),target:value('arpVictim'),protocol:value('floodProtocol','UDP'),rate:Number(value('floodRate','200')),duration:Number(value('floodDuration','1')),threshold:Number(value('floodThreshold','100'))});
      servicesView=true;renderInspector();}catch(error){toast(error.message || 'Services action failed.',true);}}));
}
function bindTransportLab(){
  inspectorBody.querySelectorAll('[data-transport-action]').forEach(button=>button.addEventListener('click',async()=>{
    const action=button.dataset.transportAction;
    const source=$('transportSource')?.value||(selected?.kind==='device'?selected.id:null);
    const destination=$('transportDestination')?.value;
    const protocol=$('transportProtocol')?.value||'TCP';
    const payload=Math.max(1,Number($('transportPayload')?.value||1))*1024;
    try{
      if(action==='create'){if(!source||!destination)throw new Error('Select source and destination devices.');await api('transport_create',{protocol,source,destination,source_port:Number($('transportSourcePort').value),destination_port:Number($('transportDestinationPort').value),payload_size:payload,initial_cwnd:Number($('transportCwnd')?.value||1),receiver_window:Number($('transportReceiverWindow')?.value||8),ssthresh:Number($('transportSsthresh')?.value||16),timeout:Number($('transportTimeout')?.value||100)/1000,traffic_class:protocol==='TCP'?'HTTP':'VoIP'});}
      if(action==='send'){const flow=state.transport?.flows?.find(item=>item.protocol===protocol);if(!flow)throw new Error('Start a transport flow first.');await api('transport_send',{protocol,flow_id:flow.flow_id,packet_count:1,payload_size:payload});}
      if(action==='close'){const flow=state.transport?.connections?.[0];if(!flow)throw new Error('No TCP connection to close.');await api('transport_close',{flow_id:flow.flow_id});}
      if(action==='tick')await api('transport_tick',{delta:0.1});
      if(action==='stop')await api('running',{value:false});
      if(action==='reset')await api('transport_reset');
      if(action==='compare')await api('transport_compare',{source,destination,packet_count:6,payload_size:payload});
      transportView=true;renderInspector();
    }catch(error){toast(error.message || 'Transport action failed.',true);}
  }));
  $('transportProtocol')?.addEventListener('change',()=>{transportProtocol=$('transportProtocol').value;transportView=true;renderInspector();});
  $('transportSource')?.addEventListener('change',()=>{transportView=true;renderInspector();});
  $('transportDestination')?.addEventListener('change',()=>{transportView=true;renderInspector();});
}
function labSettings(){
  const routing=state.routing||{},qos=state.qos||{},weights=routing.weights||{},classes=Object.keys(qos.priorities||{});
  const routingFields=Object.entries(weights).map(([name,value])=>`<label>${esc(name)}<input data-routing-weight="${esc(name)}" type="number" min="0" step="0.1" value="${value}"/></label>`).join('');
  const classFields=classes.map(name=>`<div class="policy-row"><b>${esc(name)}</b><label>Priority<input data-priority="${esc(name)}" type="number" step="0.1" value="${qos.priorities[name]}"/></label><label>WFQ weight<input data-weight="${esc(name)}" type="number" min="0.1" step="0.1" value="${(qos.weights||{})[name]||1}"/></label></div>`).join('');
  return `<div class="device-hero"><div class="device-symbol">⚙</div><div><b>Lab policies</b><span>Live engine configuration</span></div></div><div class="inspector-section"><div class="section-title">Routing</div><label>Algorithm<select id="routingAlgorithm"><option value="dijkstra" ${routing.algorithm==='dijkstra'?'selected':''}>Dijkstra</option><option value="bellman_ford" ${routing.algorithm==='bellman_ford'?'selected':''}>Bellman–Ford</option></select></label><div class="policy-grid">${routingFields}</div><button data-action="apply-routing">Apply routing policy</button></div><div class="inspector-section"><div class="section-title">Quality of Service</div><label>Scheduler<select id="schedulerSelect"><option value="fifo" ${qos.scheduler==='fifo'?'selected':''}>FIFO</option><option value="priority" ${qos.scheduler==='priority'?'selected':''}>Priority Queue</option><option value="wfq" ${qos.scheduler==='wfq'?'selected':''}>Weighted Fair Queuing</option></select></label><div class="policy-list">${classFields}</div><button data-action="apply-qos">Apply QoS policy</button></div>`;
}
function learningInspector(){
  const learning=state.learning||{},categories=learning.categories||[],result=learning.result,mode=learning.step_mode||{};
  const current=mode.current,steps=mode.steps||[];
  const topicRows=categories.map(cat=>`<div class="section-title">${esc(cat.category)}</div><div class="diagnostic-table"><ul>${cat.topics.map(t=>`<li>${esc(t.title)} - <button data-study="run-topic" data-topic="${esc(t.id)}">Run demonstration</button></li>`).join('')}</ul></div>`).join('');
  const stepHtml=result?`<div class="inspector-section"><div class="section-title">${esc(result.topic.title)} - step mode</div><div class="two"><span>Step ${mode.position||0} / ${mode.total||0}</span><span>${current?esc(current.title):'run the demonstration to begin'}</span></div>${current?`<div class="console-output">${esc(current.message||'')}\n${current.source?`${esc(current.source)} → ${esc(current.destination)}`:''}${current.route&&current.route.length?`\nroute: ${esc(current.route.join(' → '))}`:''}</div>`:''}<div class="button-row"><button data-study="step" data-step="previous">◀ Previous</button><button data-study="step" data-step="next">Next ▶</button><button data-study="step" data-step="play">Play</button><button data-study="step" data-step="pause">Pause</button><button data-study="step" data-step="reset">Reset</button></div></div>`:'';
  const detail=result?`<div class="inspector-section"><div class="section-title">Explanation</div><p>${esc(result.topic.concept)}</p><div class="hint"><b>Why:</b> ${esc(result.topic.why)}</div><div class="hint"><b>How:</b> ${esc(result.topic.how)}</div><div class="hint"><b>Observe:</b> ${esc((result.topic.observe||[]).join(' | '))}</div><div class="console-output">${esc(JSON.stringify(result.summary,null,1).slice(0,1400))}</div><div class="diagnostic-stats">${[['Sent',result.metrics.packets_sent],['Delivered',result.metrics.packets_delivered],['PDR',fmt(result.metrics.packet_delivery_ratio,1)+'%'],['Latency',fmt(result.metrics.average_latency*1000,2)+' ms']].map(([l,v])=>`<div class="diagnostic-stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('')}</div></div>`:'';
  return `<div class="device-hero"><div class="device-symbol">✎</div><div><b>Learning Mode</b><span>Every demonstration runs the real simulator</span></div></div>${stepHtml}${detail}<div class="inspector-section"><div class="section-title">Topics</div>${topicRows}</div>`;
}
function challengesInspector(){
  const challenges=state.challenges||{},catalog=challenges.challenges||[],active=challenges.active;
  const list=catalog.map(c=>`<div class="interface-card"><b>${esc(c.title)}</b><div>${esc(c.brief)}</div><div class="hint">${c.criteria.length} success criteria · ${c.hint_count} hints</div><button data-study="start-challenge" data-challenge="${esc(c.id)}">Start challenge</button></div>`).join('');
  let activeHtml='<p class="hint">Start a challenge to evaluate it against live simulator state.</p>';
  if(active){
    const rows=(challenges.criteria||[]).map(c=>`<div class="journey-step"><b>${c.passed?'✓':'✗'} ${esc(c.description)}</b><div>${esc(c.detail)}</div></div>`).join('');
    activeHtml=`<div class="inspector-section"><div class="section-title">${esc(challenges.title)} - <span class="${challenges.passed?'status-up':'status-down'}">${esc(challenges.status)}</span></div><p>${esc(challenges.brief)}</p><div class="hint"><b>Symptoms:</b> ${esc((challenges.symptoms||[]).join(' | '))}</div>${rows}<div class="button-row"><button data-study="evaluate">Evaluate</button><button data-study="hint">Hint (${challenges.hints_used||0}/${challenges.hints_available||0})</button><button data-study="reset-challenge">Reset challenge</button></div>${challenges.failing&&challenges.failing.length?`<div class="drop-reason">Not complete: ${esc(challenges.failing.join(', '))}</div>`:'<div class="status-up">All criteria satisfied - PASS</div>'}<div class="hint">Attempts ${challenges.attempts||0} · hints used ${challenges.hints_used||0} · ${fmt(challenges.elapsed_seconds,1)}s</div>${challenges.requires_answer?`<label>Submit the attacker device<input id="challengeAnswer" value="PC2"/></label><button data-study="answer">Submit answer</button>`:''}${challenges.solution_available?`<div class="console-output">${esc(catalog.find(c=>c.id===active)?.brief||'')}</div>`:''}</div>`;
  }
  return `<div class="device-hero"><div class="device-symbol">⚑</div><div><b>Challenge Mode</b><span>Measured against real simulator state</span></div></div>${activeHtml}<div class="inspector-section"><div class="section-title">Challenges</div>${list}</div>`;
}
function quizInspector(){
  const quiz=state.quiz||{},current=quiz.current,score=quiz.score||{};
  let currentHtml='<p class="hint">Start the quiz to answer concept questions and questions built from the current topology.</p>';
  if(current){
    const answered=quiz.answers&&quiz.answers[current.id];
    currentHtml=`<div class="inspector-section"><div class="section-title">Question ${(quiz.position||0)+1} / ${quiz.total||0} <span>${esc(current.kind)}</span></div><p>${esc(current.prompt)}</p>${current.options.map((o,i)=>`<button data-study="answer-quiz" data-answer="${i}">${esc(o)}</button>`).join('')}${answered?`<div class="console-output">${answered.correct?'Correct.':'Incorrect.'}\nCorrect answer: ${esc(current.options[current.options.indexOf(answered.answer)])||answered.answer}</div>`:''}</div>`;
  }
  return `<div class="device-hero"><div class="device-symbol">?</div><div><b>Quiz Mode</b><span>Concept questions plus live topology questions</span></div></div><div class="inspector-section"><div class="button-row"><button data-study="start-quiz">Start quiz</button><button data-study="next-quiz">Next question</button></div><div class="diagnostic-stats">${[['Answered',score.answered||0],['Correct',score.correct||0],['Score',fmt(score.score_percent,1)+'%']].map(([l,v])=>`<div class="diagnostic-stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('')}</div><div class="hint">${quiz.catalog_size||0} concept questions available; live questions are generated from the running topology.</div></div>${currentHtml}`;
}
function demosInspector(){
  const demos=state.demos||{},catalog=demos.catalog||[],result=demos.result;
  const list=catalog.map(d=>`<div class="interface-card"><b>${esc(d.title)}</b><div>${esc(d.description)}</div><div class="hint">${esc(d.category)} · preset ${esc(d.preset)}</div><div class="button-row"><button data-study="run-demo" data-demo="${esc(d.id)}">Run demonstration</button>${result&&result.id===d.id?'<button data-study="reset-demo">Reset scenario</button>':''}</div></div>`).join('');
  const resultHtml=result?`<div class="inspector-section"><div class="section-title">${esc(result.title)}</div>${(result.steps||[]).map(s=>`<div class="journey-step"><b>${esc(s)}</b></div>`).join('')}<div class="console-output">${esc(result.explanation)}</div><div class="diagnostic-stats">${[['Sent',result.metrics.packets_sent],['Delivered',result.metrics.packets_delivered],['PDR',fmt(result.metrics.packet_delivery_ratio,1)+'%'],['Latency',fmt(result.metrics.average_latency_ms,2)+' ms']].map(([l,v])=>`<div class="diagnostic-stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('')}</div><div class="hint">Relevant events: ${(result.events||[]).slice(-5).map(e=>esc(e.event)).join(', ')||'none'}</div></div>`:'';
  return `<div class="device-hero"><div class="device-symbol">▶</div><div><b>Demonstrations</b><span>One-click scenarios on the real simulator</span></div></div>${resultHtml}<div class="inspector-section"><div class="section-title">Scenarios</div>${list}</div>`;
}
function overviewInspector(){
  const o=state.overview||{},health=state.health||{},categories=health.categories||[];
  const stats=[['Devices',o.devices],['Links',o.links],['Active flows',o.active_flows],['Sent',o.packets_sent],['Delivered',o.packets_delivered],['Lost',o.packets_lost],['PDR',fmt(o.packet_delivery_ratio,1)+'%'],['Latency',fmt(o.average_latency_ms,2)+' ms'],['Throughput',fmt(o.throughput,1)+' B/s'],['Congestion',fmt(o.congestion,2)],['Failures',o.active_failures],['Route changes',o.route_changes],['TCP conns',o.tcp_connections],['UDP flows',o.udp_flows],['Services',`${o.services_running}/${o.services_total}`],['Blocked',o.blocked_packets],['Alerts',o.security_alerts]];
  const healthHtml=categories.map(c=>`<div class="interface-card"><b>${esc(c.category)} - <span class="${c.status==='HEALTHY'?'status-up':c.status==='WARNING'?'status-warning':'status-down'}">${esc(c.status)}</span></b>${c.rules.map(r=>`<div>${esc(r.reason)}</div>`).join('')}</div>`).join('');
  return `<div class="device-hero"><div class="device-symbol">▤</div><div><b>Network Overview</b><span>Live metrics and rule-based health</span></div></div><div class="inspector-section"><div class="section-title">Overview</div><div class="diagnostic-stats">${stats.map(([l,v])=>`<div class="diagnostic-stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('')}</div></div><div class="inspector-section"><div class="section-title">Network health <span>${esc(health.overall||'')}</span></div>${healthHtml}<div class="hint">Health uses published rules only: PDR ${fmt((health.thresholds||{}).pdr_warning_percent,0)}% warning, congestion ${fmt((health.thresholds||{}).congestion_warning,2)} warning.</div></div><div class="inspector-section"><div class="section-title">Report</div><div class="button-row"><button data-study="report" data-format="json">JSON report</button><button data-study="report" data-format="markdown">Markdown</button><button data-study="report" data-format="csv">CSV export</button><button data-study="journey">Packet journey</button><button data-study="explain">Why did this fail?</button></div><div id="reportOutput" class="console-output"></div></div>`;
}
function bindStudyPanels(){
  inspectorBody.querySelectorAll('[data-study]').forEach(button=>button.addEventListener('click',async()=>{
    const action=button.dataset.study;
    const value=(id)=>$(id)?.value;
    try{
      if(action==='run-topic')await api('learning_run',{topic:button.dataset.topic});
      if(action==='step')await api('learning_step',{step:button.dataset.step});
      if(action==='start-challenge')await api('challenge',{mode:'start',challenge:button.dataset.challenge});
      if(action==='evaluate')await api('challenge',{mode:'evaluate'});
      if(action==='hint'){const data=await api('challenge',{mode:'hint'});if(data.state?.challenges?.hint)toast(data.state.challenges.hint.text);}
      if(action==='reset-challenge')await api('challenge',{mode:'reset'});
      if(action==='answer')await api('challenge',{mode:'answer',key:'attacker',value:value('challengeAnswer')});
      if(action==='start-quiz')await api('quiz',{mode:'start'});
      if(action==='answer-quiz')await api('quiz',{mode:'submit',answer:Number(button.dataset.answer)});
      if(action==='next-quiz')await api('quiz',{mode:'next'});
      if(action==='run-demo')await api('demo',{mode:'run',demo:button.dataset.demo});
      if(action==='reset-demo')await api('demo',{mode:'reset'});
      if(action==='report'){
        const data=await api('report',{format:button.dataset.format});
        const output=$('reportOutput');
        if(output)output.textContent=data.state.format==='json'?JSON.stringify(data.state.report.overview)+'\n'+JSON.stringify(data.state.report.health.categories[0]||{}):data.state.text;
      }
      if(action==='journey'){
        const packet=(state.packets||[]).slice().reverse().find(p=>p.status==='DELIVERED'||p.drop_reason);
        if(!packet)throw new Error('No packet to inspect yet.');
        await api('packet_journey',{packet_id:packet.id});
      }
      if(action==='explain'){const data=await api('explain',{});const e=data.state.explanation;toast(e&&e.failed?e.explanation:'No failure recorded yet.');}
      if(studyView)renderInspector();
    }catch(error){}
  }));
}
function renderInspector(){let html=labSettings();if(servicesView)html=servicesLabInspector();else if(studyView)html=studyView==='learning'?learningInspector():studyView==='challenges'?challengesInspector():studyView==='quiz'?quizInspector():studyView==='demos'?demosInspector():overviewInspector();else if(transportView)html=transportLabInspector();else if(diagnosticView)html=state.packetListView?packetListInspector():diagnosticResultInspector();if(!transportView&&!studyView&&!diagnosticView&&selected?.kind==='device'){const d=state.devices.find(x=>x.id===selected.id);if(d)html=deviceInspector(d);}if(!transportView&&!studyView&&!diagnosticView&&selected?.kind==='link'){const l=state.links.find(x=>key(x.source,x.target)===key(selected.id,selected.other));if(l)html=linkInspector(l);}if(!transportView&&!studyView&&!diagnosticView&&selected?.kind==='packet')html=packetInspector(selected.data);if(!transportView&&!studyView&&!diagnosticView&&selected?.kind==='event')html=eventInspector(selected.data);inspectorBody.innerHTML=html;bindInspectorActions();bindTransportLab();bindServicesLab();bindStudyPanels();if(diagnosticView&&state.packetListView)bindPacketList();}
function bindInspectorActions() {
  inspectorBody.querySelectorAll('[data-action]').forEach((el) => {
    el.addEventListener('click', async () => {
      const action = el.dataset.action;
      if (action === 'rename') { const result=await api('rename_device', {id:selected.id, name:$('renameInput').value}); selected={kind:'device',id:result.state.name}; renderInspector(); }
      if (action === 'duplicate') await api('duplicate_device', {id:selected.id});
      if (action === 'delete-device' && confirm('Delete this device and its links?')) { await api('delete_device', {id:selected.id}); selected=null; renderInspector(); }
      if (action === 'shutdown') await api('shutdown_device', {id:selected.id});
      if (action === 'restart') await api('restart_device', {id:selected.id});
      if (action === 'apply-interface') {
        const card = el.closest('.interface-card');
        const values = {};
        card.querySelectorAll('[data-field]').forEach((input) => { values[input.dataset.field] = input.value; });
        await api('configure_interface', {id:selected.id, interface_id:el.dataset.interface, values});
      }
      if (action === 'apply-link') await api('configure_link', {source:selected.id, target:selected.other, values:{latency:Number($('linkLatency').value), bandwidth:Number($('linkBandwidth').value), packet_loss:Number($('linkLoss').value)/100, congestion:Number($('linkCongestion').value)/100}});
      if (action === 'toggle-link') {
        const link = state.links.find((item) => key(item.source,item.target) === key(selected.id,selected.other));
        await api('configure_link', {source:selected.id, target:selected.other, values:{status:link.status === 'UP' ? 'DOWN' : 'UP'}});
      }
      if (action === 'delete-link' && confirm('Delete this link?')) { await api('delete_link', {source:selected.id, target:selected.other}); selected=null; renderInspector(); }
      if (action === 'start-traffic') {
        const source=selected.id, destination=$('trafficDestination').value;
        const protocol=$('trafficProtocol')?.value||'IP';
        const count=Number($('trafficCount').value), pps=Number($('trafficPps').value), size=Number($('trafficSize').value);
        if(protocol==='ICMP'){ await api('ping',{source,destination,count:Math.max(1,Math.min(count,10))}); toast(`ICMP echo sent from ${source} to ${destination}`); }
        else if(protocol==='TCP'||protocol==='UDP'){ await api('transport_create',{protocol,source,destination,source_port:5000,destination_port:protocol==='TCP'?8080:5060,payload_size:size,traffic_class:protocol==='TCP'?'HTTP':'VoIP'}); const flow=state.transport?.flows?.find(item=>item.protocol===protocol); if(!flow) throw new Error(`${protocol} flow was not created.`); await api('transport_send',{protocol,flow_id:flow.flow_id,packet_count:Math.max(1,Math.min(count,10)),payload_size:size}); toast(`${protocol} traffic sent to ${destination}`); }
        else { await api('start_traffic', {source, destination, traffic_type:$('trafficType').value, packet_count:count, pps, packet_size:size}); play(); }
      }
      if (action === 'apply-routing') { const routingWeights={}; inspectorBody.querySelectorAll('[data-routing-weight]').forEach(input=>routingWeights[input.dataset.routingWeight]=Number(input.value)); await api('routing_algorithm',{algorithm:$('routingAlgorithm').value}); await api('routing_weights',{weights:routingWeights}); }
      if (action === 'apply-qos') { const priorities={},weights={}; inspectorBody.querySelectorAll('[data-priority]').forEach(input=>priorities[input.dataset.priority]=Number(input.value)); inspectorBody.querySelectorAll('[data-weight]').forEach(input=>weights[input.dataset.weight]=Number(input.value)); await api('scheduler',{scheduler:$('schedulerSelect').value}); await api('qos_config',{priorities,weights}); }
      if (action === 'clear-arp') await api('clear_arp',{id:state.diagnostic_result.device});
      if (action === 'clear-mac') await api('clear_mac',{id:state.diagnostic_result.device});
      if (action === 'open-packets' || action === 'back-packets') { state.packetListView=true; diagnosticView=true; renderInspector(); }
      if (action === 'console') { selected = {kind:'console', id:selected.id}; renderConsole(); }
    });
  });
}
function renderConsole(){const d=selected.id;inspectorBody.innerHTML=`<div class="device-hero"><div class="device-symbol">›_</div><div><b>${esc(d)} console</b><span>Real backend commands only</span></div></div><div class="console-output" id="consoleOutput">${esc(d)}&gt; _</div><div class="console-form"><input id="consoleInput" value="show interfaces"/><button data-console>Run</button></div><div class="button-row"><button data-action="close-console">Back to device</button></div>`;inspectorBody.querySelector('[data-console]').addEventListener('click',runConsole);inspectorBody.querySelector('[data-action="close-console"]').addEventListener('click',()=>{selected={kind:'device',id:d};renderInspector();});}
async function runConsole(){const command=$('consoleInput').value;const output=$('consoleOutput');output.textContent+=`\n${command}`;try{const result=await api('console',{id:selected.id,command});output.textContent+=`\n${result.state.output}`;}catch(error){output.textContent+=`\n${error.message}`;}}
function setMode(next){mode=next;connectStart=null;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));toast(`${next[0].toUpperCase()+next.slice(1)} tool active`);}
document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.mode)));
document.querySelectorAll('[data-menu]').forEach(button=>button.addEventListener('click',()=>{const action=button.dataset.menu;if(action==='file'){$('newBtn').click();}else if(action==='simulation'){$('playBtn').click();}else if(action==='view'){$('fitBtn').click();$('gridToggle').checked=!$('gridToggle').checked;renderCanvas();}else{toast('Build a topology, connect devices, start traffic, then inject faults to observe self-healing.');}}));
document.querySelectorAll('[data-diagnostic]').forEach(button=>button.addEventListener('click',async()=>{const action=button.dataset.diagnostic;if(action==='transport'){transportView=true;servicesView=false;diagnosticView=false;renderInspector();return;}
if(action==='services'){servicesView=true;studyView=null;transportView=false;diagnosticView=false;renderInspector();return;}
if(['learning','challenges','quiz','demos','overview'].includes(action)){studyView=action;servicesView=false;transportView=false;diagnosticView=false;renderInspector();return;}if(action==='packets'){state.packetListView=true;diagnosticView=true;transportView=false;renderInspector();return;}const source=selected?.kind==='device'?selected.id:null;if(!source){toast('Select a source device first.',true);return;}const destination=$('diagnosticDestination').value;try{if(action==='ping'||action==='traceroute'){if(!destination||!state.devices.some(device=>device.id===destination))throw new Error('Select a destination device.');await api(action,{source,destination,count:4});}else{await api(action==='arp'?'arp_table':'mac_table',{id:source});}state.packetListView=false;diagnosticView=true;transportView=false;renderInspector();}catch(error){}}));
document.querySelectorAll('.palette-item').forEach(item=>item.addEventListener('dragstart',e=>e.dataTransfer.setData('text/plain',item.dataset.type)));
canvas.addEventListener('dragover',e=>e.preventDefault());canvas.addEventListener('drop',e=>{e.preventDefault();const type=e.dataTransfer.getData('text/plain');if(type) {const p=canvasPoint(e);api('add_device',{type,x:p.x,y:p.y});}});
canvas.addEventListener('click',e=>{if(e.target===canvas||e.target.id==='gridRect'||e.target.id==='connectPreview'){selected=null;diagnosticView=false;transportView=false;connectStart=null;connectPreview.setAttribute('d','');renderInspector();}});
canvas.addEventListener('pointermove',e=>{if(connectStart&&state){const from=state.devices.find(d=>d.id===connectStart),p=canvasPoint(e);if(from)connectPreview.setAttribute('d',`M ${from.x} ${from.y} L ${p.x} ${p.y}`);}});
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.65,Math.min(1.5,zoom+(e.deltaY<0?.05:-.05)));canvas.style.transform=`scale(${zoom})`;},{passive:false});
$('gridToggle').addEventListener('change',renderCanvas);$('snapToggle').addEventListener('change',()=>{});
$('clearSelection').addEventListener('click',()=>{selected=null;diagnosticView=false;transportView=false;renderInspector();});
$('newBtn').addEventListener('click',()=>{if(confirm('Start a new blank network?'))api('new').then(()=>{selected=null;diagnosticView=false;transportView=false;state.packetListView=false;connectStart=null;renderInspector();});});
$('presetSelect').addEventListener('change',e=>api('load_preset',{preset:e.target.value}).then(()=>{selected=null;diagnosticView=false;transportView=false;state.packetListView=false;connectStart=null;renderInspector();}));
$('saveBtn').addEventListener('click',async()=>{const data=await api('export');const blob=new Blob([JSON.stringify(data.state.document,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='netadapt-lab.json';a.click();URL.revokeObjectURL(a.href);toast('Topology saved');});
$('openBtn').addEventListener('click',()=>$('fileInput').click());$('fileInput').addEventListener('change',async e=>{const file=e.target.files[0];if(file)api('import',{document:JSON.parse(await file.text())});e.target.value='';});
$('undoBtn').addEventListener('click',()=>api('undo'));$('redoBtn').addEventListener('click',()=>api('redo'));
$('playBtn').addEventListener('click',async()=>{await api('running',{value:true});play();});$('pauseBtn').addEventListener('click',()=>{stop();api('running',{value:false});});$('stopBtn').addEventListener('click',()=>{stop();api('running',{value:false});});$('stepBtn').addEventListener('click',()=>api('step'));$('resetBtn').addEventListener('click',()=>{stop();api('reset');});$('speedSelect').addEventListener('change',e=>{api('speed',{value:Number(e.target.value)});$('workspaceSpeed').value=e.target.value;});
document.querySelectorAll('[data-workspace-control]').forEach(button=>button.addEventListener('click',async()=>{const control=button.dataset.workspaceControl;if(control==='play'){await api('running',{value:true});play();}else if(control==='step'){stop();await api('step');}else{stop();await api('running',{value:false});if(control==='reset')await api('reset');}}));
$('workspaceSpeed').addEventListener('change',e=>{api('speed',{value:Number(e.target.value)});$('speedSelect').value=e.target.value;});
$('fitBtn').addEventListener('click',()=>{if(!state.devices.length)return;const xs=state.devices.map(d=>d.x),ys=state.devices.map(d=>d.y),minX=Math.min(...xs)-100,maxX=Math.max(...xs)+100,minY=Math.min(...ys)-100,maxY=Math.max(...ys)+100;canvas.setAttribute('viewBox',`${minX} ${minY} ${maxX-minX} ${maxY-minY}`);});
async function play(){stop();playTimer=setInterval(async()=>{if(!state?.running){stop();return;}try{await api('step');}catch(e){stop();}},Math.max(120,600/(state?.speed||1)));}function stop(){if(playTimer)clearInterval(playTimer);playTimer=null;}
document.addEventListener('keydown',e=>{if((e.key==='Delete'||e.key==='Backspace')&&selected?.kind==='device'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();api('delete_device',{id:selected.id});}if(e.key==='Escape'){selected=null;diagnosticView=false;transportView=false;connectStart=null;renderInspector();}});
loadState().catch(e=>toast(e.message,true));
