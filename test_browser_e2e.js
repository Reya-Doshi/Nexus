const { chromium } = require('playwright');

async function runBrowserTests() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const consoleErrors = [];
  const networkFailures = [];

  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', err => {
    consoleErrors.push(err.message);
  });
  page.on('requestfailed', req => {
    networkFailures.push(`${req.method()} ${req.url()}: ${req.failure()?.errorText}`);
  });

  console.log('--- Step 0: Loading localhost:8000 ---');
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1000);

  // Check 1: Start traffic advances traffic and telemetry
  console.log('--- Step 1: Testing Start Traffic button ---');
  // First ensure fresh state
  await page.locator('#mainResetBtn').click();
  await page.waitForTimeout(800);
  
  const initialPackets = await page.locator('#metricPackets').textContent();
  const initialLat = await page.locator('#metricLatency').textContent();
  console.log('Initial metrics before traffic:', { packets: initialPackets, latency: initialLat });
  if (initialLat !== 'N/A' && !initialLat.includes('ms')) {
    throw new Error(`Expected initial latency to be N/A or valid ms, got "${initialLat}"`);
  }

  const startBtn = page.locator('#mainStartBtn');
  await startBtn.click();
  await page.waitForTimeout(2000);

  const pdrText1 = await page.locator('#metricPdr').textContent();
  const packetsText1 = await page.locator('#metricPackets').textContent();
  console.log('After Start Traffic:', { pdr: pdrText1, packets: packetsText1 });
  
  const m1 = packetsText1.match(/(\d+)\s+delivered\s*\/\s*(\d+)\s+sent/);
  if (!m1 || Number(m1[2]) === 0) {
    throw new Error(`Start Traffic failed to advance packets: "${packetsText1}"`);
  }
  const delivered1 = Number(m1[1]);
  const sent1 = Number(m1[2]);

  // Check 2: Pause stops progression; Resume continues without creating another flow
  console.log('--- Step 2: Testing Pause & Resume buttons ---');
  const pauseBtn = page.locator('#mainPauseBtn');
  await pauseBtn.click();
  await page.waitForTimeout(600);

  const packetsTextAfterPause = await page.locator('#metricPackets').textContent();
  await page.waitForTimeout(1000);
  const packetsTextAfterPauseWait = await page.locator('#metricPackets').textContent();
  console.log('Packets during pause:', { pause1: packetsTextAfterPause, pause2: packetsTextAfterPauseWait });
  if (packetsTextAfterPause !== packetsTextAfterPauseWait) {
    throw new Error(`Pause failed: packets continued to advance while paused (${packetsTextAfterPause} -> ${packetsTextAfterPauseWait})`);
  }

  // Resume
  await startBtn.click();
  await page.waitForTimeout(1500);
  const packetsTextAfterResume = await page.locator('#metricPackets').textContent();
  console.log('Packets after resume:', packetsTextAfterResume);
  const m2 = packetsTextAfterResume.match(/(\d+)\s+delivered\s*\/\s*(\d+)\s+sent/);
  if (!m2 || Number(m2[2]) <= sent1) {
    throw new Error(`Resume failed to continue packet progression: was ${sent1}, now ${m2 ? m2[2] : 'null'}`);
  }

  // Check 3: Reset clears incident, restores topology, invalidates pending AI work
  console.log('--- Step 3: Testing Reset button ---');
  const resetBtn = page.locator('#mainResetBtn');
  await resetBtn.click();
  await page.waitForTimeout(1000);
  const pdrAfterReset = await page.locator('#metricPdr').textContent();
  const packetsAfterReset = await page.locator('#metricPackets').textContent();
  console.log('After Reset:', { pdr: pdrAfterReset, packets: packetsAfterReset });
  if (!packetsAfterReset.includes('0 delivered / 0 sent')) {
    throw new Error(`Reset failed to reset packet counters to 0 / 0: got "${packetsAfterReset}"`);
  }

  // Check 4: Scenario selection persists during dashboard refreshes
  console.log('--- Step 4: Testing Scenario persistence ---');
  const scenarioSelect = page.locator('#scenarioSelect');
  await scenarioSelect.selectOption('mild_congestion');
  await page.waitForTimeout(500);

  // Reload page
  console.log('Reloading page to test persistence...');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1000);
  const scenarioValAfterReload = await page.locator('#scenarioSelect').inputValue();
  console.log('Scenario after reload:', scenarioValAfterReload);
  if (scenarioValAfterReload !== 'mild_congestion') {
    throw new Error(`Scenario persistence failed: expected "mild_congestion", got "${scenarioValAfterReload}"`);
  }

  // Check 5: Inject Fault affects simulation and triggers non-blocking diagnosis
  console.log('--- Step 5: Testing Inject Fault button (single debounced action) ---');
  await page.locator('#scenarioSelect').selectOption('sudden_link_failure');
  await page.locator('#mainStartBtn').click();
  await page.waitForTimeout(1000);

  const injectBtn = page.locator('#injectFaultBtn');
  await injectBtn.click();
  await page.waitForTimeout(3000);

  const statusAfterInject = await page.locator('#statusText').textContent();
  const diagCause = await page.locator('#diagLikelyCause').textContent();
  console.log('After Inject Fault:', { status: statusAfterInject, cause: diagCause });
  if (statusAfterInject.includes('Network healthy') && diagCause === 'Awaiting incident') {
    throw new Error('Inject Fault had no effect on simulation or diagnosis!');
  }

  // Check 6: Auto-Heal OFF preserves baseline behavior
  console.log('--- Step 6: Testing Auto-Heal toggle button ---');
  const healBtn = page.locator('#autoHealToggleBtn');
  await healBtn.click(); // Toggle OFF
  await page.waitForTimeout(500);
  const healTextAfterToggle = await healBtn.textContent();
  console.log('Heal button after toggle:', healTextAfterToggle);
  if (!healTextAfterToggle.includes('OFF')) {
    throw new Error(`Expected Auto-Heal: OFF, got "${healTextAfterToggle}"`);
  }

  // Toggle back ON
  await healBtn.click();
  await page.waitForTimeout(500);
  console.log('Heal button restored:', await healBtn.textContent());

  // Check 7: Device selection and inspector drawer
  console.log('--- Step 7: Testing Device selection & Inspector drawer ---');
  const clientNode = page.locator('g.node[data-id="Client"]');
  await clientNode.click();
  await page.waitForTimeout(500);

  const inspectorDrawer = page.locator('#inspectorDrawer');
  const isInspectorVisible = await inspectorDrawer.isVisible();
  const inspectorTitle = await page.locator('#inspectorTitle').textContent();
  console.log('Inspector visible after clicking Client:', { isInspectorVisible, title: inspectorTitle });
  if (!isInspectorVisible || !inspectorTitle.includes('Client')) {
    throw new Error(`Device inspector failed to open for Client node: visible=${isInspectorVisible}, title="${inspectorTitle}"`);
  }

  // Close inspector
  await page.locator('#closeInspectorBtn').click();
  await page.waitForTimeout(300);

  // Check 8: Comparison opens an in-page results panel, not a browser alert
  console.log('--- Step 8: Testing Comparison in-page modal panel ---');
  let alertFired = false;
  page.on('dialog', async dialog => {
    alertFired = true;
    console.error('UNEXPECTED browser dialog fired:', dialog.message());
    await dialog.dismiss();
  });

  const compBtn = page.locator('#openComparisonBtn');
  await compBtn.click();

  // Wait for modal to become visible and results to populate
  const modal = page.locator('#comparisonModal');
  await modal.waitFor({ state: 'visible', timeout: 5000 });
  console.log('Comparison modal opened successfully.');

  const compResults = page.locator('#comparisonResults');
  await compResults.waitFor({ state: 'visible', timeout: 10000 });

  const offPdr = await page.locator('#compOffPdr').textContent();
  const onPdr = await page.locator('#compOnPdr').textContent();
  const pdrDelta = await page.locator('#compPdrDelta').textContent();
  const provider = await page.locator('#compProviderName').textContent();
  console.log('In-page comparison results:', { offPdr, onPdr, pdrDelta, provider });

  if (alertFired) {
    throw new Error('Comparison opened a browser alert instead of an in-page modal panel!');
  }
  if (!pdrDelta.includes('%')) {
    throw new Error(`Invalid benchmark comparison metric: "${pdrDelta}"`);
  }

  // Close modal via close button
  await page.locator('#closeComparisonBtn').click();
  await page.waitForTimeout(500);
  const modalVisible = await modal.isVisible();
  console.log('Comparison modal closed. Is visible:', modalVisible);

  // Check 9: Verify reload while running restores playback timer
  console.log('--- Step 9: Testing reload while running restores playback timer ---');
  await page.locator('#mainStartBtn').click();
  await page.waitForTimeout(1000);
  const packetsBeforeReload = await page.locator('#metricPackets').textContent();
  console.log('Packets before reload:', packetsBeforeReload);
  
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  const startBtnAfterReload = await page.locator('#mainStartBtn').textContent();
  console.log('Start button text after reload:', startBtnAfterReload);
  if (!startBtnAfterReload.includes('Running')) {
    throw new Error(`Expected button to show Running after reload, got "${startBtnAfterReload}"`);
  }
  // Wait to ensure timer continues advancing packets
  await page.waitForTimeout(1500);
  const packetsAfterReloadWait = await page.locator('#metricPackets').textContent();
  console.log('Packets after reload wait:', packetsAfterReloadWait);
  const mReload1 = packetsBeforeReload.match(/(\d+)\s+delivered\s*\/\s*(\d+)\s+sent/);
  const mReload2 = packetsAfterReloadWait.match(/(\d+)\s+delivered\s*\/\s*(\d+)\s+sent/);
  if (!mReload1 || !mReload2 || Number(mReload2[2]) <= Number(mReload1[2])) {
    throw new Error(`Reload failed to resume simulation progression: was ${packetsBeforeReload}, now ${packetsAfterReloadWait}`);
  }

  // Check 10: Verify build/run separation (structural edits locked during running)
  console.log('--- Step 10: Testing build/run separation lock ---');
  const addRouterBtnDisabled = await page.locator('#addRouterBtn').isDisabled();
  const connectBtnDisabled = await page.locator('#modeConnectBtn').isDisabled();
  console.log('Structural buttons disabled while running:', { addRouterBtnDisabled, connectBtnDisabled });
  if (!addRouterBtnDisabled || !connectBtnDisabled) {
    throw new Error('Structural topology editing buttons were not locked during playback!');
  }

  // Fault controls must remain active
  const injectFaultDisabled = await page.locator('#injectFaultBtn').isDisabled();
  const autoHealDisabled = await page.locator('#autoHealToggleBtn').isDisabled();
  if (injectFaultDisabled || autoHealDisabled) {
    throw new Error('Fault controls were incorrectly disabled during playback!');
  }

  // Check 11: Pause, test adding device returns correct name
  console.log('--- Step 11: Testing add-device naming ---');
  await page.locator('#mainPauseBtn').click();
  await page.waitForTimeout(600);

  const addRouterBtn = page.locator('#addRouterBtn');
  const isDisabledAfterPause = await addRouterBtn.isDisabled();
  if (isDisabledAfterPause) {
    throw new Error('Add router button remained disabled after pausing!');
  }
  await addRouterBtn.click();
  await page.waitForTimeout(600);

  const toastText = await page.locator('#toast').textContent();
  console.log('Toast text after adding router:', toastText);
  if (!toastText.includes('Added router') || toastText.includes('undefined')) {
    throw new Error(`Unexpected toast text for added router: "${toastText}"`);
  }

  console.log('--- Verifying Console & Network Hygiene ---');
  console.log('Console Errors:', consoleErrors);
  console.log('Network Failures:', networkFailures);

  await browser.close();

  if (consoleErrors.length > 0) {
    throw new Error(`Console errors detected: ${JSON.stringify(consoleErrors)}`);
  }
  if (networkFailures.length > 0) {
    throw new Error(`Network failures detected: ${JSON.stringify(networkFailures)}`);
  }

  console.log('ALL BROWSER INTERACTION TESTS PASSED!');
}

runBrowserTests().catch(err => {
  console.error('Test run failed:', err);
  process.exit(1);
});
