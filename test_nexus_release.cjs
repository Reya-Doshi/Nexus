const { chromium } = require('playwright');
const assert = require('node:assert/strict');
// Run only against a disposable local server: this suite replaces its topology.
const url = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8017';
(async () => {
 const browser = await chromium.launch({headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message)); page.on('dialog',d=>d.accept());
 const click = id => page.locator('#'+id).click();
 const snapshot = async () => (await page.request.get(url+'/api/state')).json();
 const reset = async () => {await click('navSimulatorBtn');await click('loadRecoveryDemo');await page.waitForFunction(()=>state?.metrics?.sent===0 && !state.running && state.devices.length===8);};
 const download = async id => {const wait=page.waitForEvent('download');await click(id);const d=await wait;assert.equal(await d.failure(),null);return d;};
 try {
 await page.goto(url);await page.waitForFunction(()=>state?.devices?.length>0);
 for(const theme of ['dark','light']) {
  if(await page.locator('html').getAttribute('data-theme')!==theme) await click('sidebarThemeToggleBtn');
  assert(await page.locator('#pageSimulator').isVisible(),'theme must not hide current page');
  await page.reload();await page.waitForFunction(()=>state?.devices?.length>0);
  assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
 }
 console.log('PASS themes and visible page persistence');
 await reset();
 const routes = await page.evaluate(()=>{
  const saved=state;
  try {
   state={devices:['A','B','C','D'].map(id=>({id,status:'UP'})),links:[{source:'A',target:'B',status:'UP'},{source:'C',target:'D',status:'UP'}],flows:[{status:'ACTIVE',route:['A','B']},{status:'ACTIVE',route:['C','D']}],reroute:{incident_state:'Unresolved'}};
   const both=[...routeEdges()];state.links[1].status='DOWN';const broken=[...routeEdges()];state.flows[0].status='COMPLETED';const ended=[...routeEdges()];return {both,broken,ended};
  } finally {state=saved;}
 });
 assert.equal(routes.both.length,2);assert.deepEqual(routes.broken,['A--B']);assert.deepEqual(routes.ended,[]);
 console.log('PASS multi-flow, broken-route and completed-flow rendering');
 const added=[];
 for(const id of ['homeAddRouterBtn','homeAddSwitchBtn','homeAddClientBtn','homeAddServerBtn']) {
  const before=(await snapshot()).devices.map(d=>d.id);await click(id);
  await page.waitForFunction(n=>state.devices.length===n,before.length+1);
  const created=(await snapshot()).devices.find(d=>!before.includes(d.id));assert(created);added.push(created.id);
 }
 console.log('PASS all four device types added to backend');
 await click('homeConnectBtn');await page.waitForFunction(()=>mode==='connect');await click('btnToolSelect');await page.waitForFunction(()=>mode==='select');
 await click('homeConnectBtn');await page.waitForFunction(()=>mode==='connect');
 await page.locator(`.node[data-id="${added[0]}"]`).click();await page.locator(`.node[data-id="${added[1]}"]`).click();
 await page.waitForFunction(ids=>state.links.some(l=>l.source===ids[0]&&l.target===ids[1]||l.source===ids[1]&&l.target===ids[0]),added);
 await click('btnToolSelect');
 await page.locator(`.node[data-id="${added[0]}"]`).click();
 assert.match(await page.locator('#btnToggleDevPower').innerText(),/Shutdown/);
 await click('btnToggleDevPower');await page.waitForFunction(id=>state.devices.find(d=>d.id===id).status==='DOWN',added[0]);
 assert.match(await page.locator('#inspectorBody').innerText(),/0 active/);
 await click('btnToggleDevPower');await page.waitForFunction(id=>state.devices.find(d=>d.id===id).status==='UP',added[0]);
 await click('btnDeleteDev');await page.waitForFunction(id=>!state.devices.some(d=>d.id===id),added[0]);
 console.log('PASS connect/select, create link, shutdown/start and delete');
 const saved=await download('saveTopologyBtn');const savedPath=await saved.path();
 await reset();await page.locator('#fileInput').setInputFiles(savedPath);await page.waitForFunction(id=>state.devices.some(d=>d.id===id),added[1]);
 console.log('PASS topology export/import round trip');
 await reset();await click('btnRunMode');await page.waitForFunction(()=>state.metrics.sent>2);
 await click('btnRunMode');await page.waitForFunction(()=>!state.running);const paused=(await snapshot()).metrics.sent;await page.waitForTimeout(500);assert.equal((await snapshot()).metrics.sent,paused);
 await click('btnRunMode');await page.waitForFunction(n=>state.metrics.sent>n,paused);
 console.log('PASS run, pause and resume');
 for(const scenario of ['sudden_link_failure','mild_congestion','gradual_congestion','no_alternate_path']) {
  await reset();await click('btnRunMode');await page.locator('#scenarioSelect').selectOption(scenario);await click('injectFaultBtn');
  await page.waitForFunction(()=>/Recovery verified|No alternative path/.test(document.querySelector('#recoveryHeading').textContent),null,{timeout:60000});
  const heading=await page.locator('#recoveryHeading').innerText();assert.match(heading,scenario==='no_alternate_path'?/No alternative path/:/Recovery verified/);
  const evidence=(await snapshot()).reroute; assert(evidence.impact.observed_packets > 0); assert(evidence.diagnosis_input.link_stats); console.log('PASS scenario',scenario,heading,evidence.impact.verdict);
 }
 await reset();await click('startPreventiveDemo');
 await page.waitForFunction(()=>state?.reroute?.incident_state==='Recovered' && !state.running,null,{timeout:60000});
 assert.equal((await snapshot()).reroute.impact.verdict,'no_impact_observed');
 assert.equal((await snapshot()).reroute.service_target_met,true);
 console.log('PASS one-click preventive demo, auto-pause and zero observed impact');
 await click('navIncidentsBtn');await download('btnExportIncidentReport');
 await click('navCompareBtn');await page.locator('#compareScenarioSelect').selectOption('preventive_congestion');await click('btnRunComparisonBenchmark');await page.waitForFunction(()=>lastComparison!==null,{},{timeout:60000});await download('btnDownloadCompareResults');
 assert(Number(await page.locator('#cmpOffBreaches').innerText())>0);assert.equal(await page.locator('#cmpOnBreaches').innerText(),'0');console.log('PASS comparison proves latency breaches avoided; exports work');
 await click('navConnectionsBtn');assert(await page.locator('#pageConnections').isVisible());
 await click('navAboutBtn');assert(await page.locator('#pageAbout').isVisible());
 await reset();assert.equal(await page.locator('#chartLatencyLine').getAttribute('d'),'');assert.equal(await page.locator('#subChartPacketsStat').innerText(),'0 / 0');
 for(const width of [1440,1024]) {await page.setViewportSize({width,height:900});assert(await page.locator('#btnRunMode').isVisible());await page.screenshot({path:`nexus-verified-${width}.png`,fullPage:true});}
 assert.deepEqual(errors,[]);console.log('PASS reset, navigation, layouts; no page errors');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

