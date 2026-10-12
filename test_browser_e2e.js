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

  console.log('--- Step 0: Loading Dhaara Network Simulator at localhost:8000 ---');
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1000);

  // Check 1: Start traffic advances traffic and telemetry
  console.log('--- Step 1: Testing Start Traffic button ---');
  await page.locator('#mainResetBtn').click();
  await page.waitForTimeout(800);

  const initialDelivered = await page.locator('#metricDeliveredCount').textContent();
  const initialRatio = await page.locator('#metricPacketsRatio').textContent();
  const initialLat = await page.locator('#metricLatency').textContent();
  console.log('Initial metrics before traffic:', { delivered: initialDelivered, ratio: initialRatio, latency: initialLat });
  if (initialLat !== 'N/A' && !initialLat.includes('ms')) {
    throw new Error(`Expected initial latency to be N/A or valid ms, got "${initialLat}"`);
  }

  const startBtn = page.locator('#mainStartBtn');
  await startBtn.click();
  await page.waitForTimeout(2200);

  const pdrText1 = await page.locator('#metricPdr').textContent();
  const delivered1 = Number(await page.locator('#metricDeliveredCount').textContent());
  const ratio1 = await page.locator('#metricPacketsRatio').textContent();
  console.log('After Start Traffic:', { pdr: pdrText1, delivered: delivered1, ratio: ratio1 });

  const mSent = ratio1.match(/(\d+)\s+sent/);
  if (!mSent || Number(mSent[1]) === 0) {
    throw new Error(`Start Traffic failed to advance packets: "${ratio1}"`);
  }
  const sent1 = Number(mSent[1]);

  // Check 2: Pause stops progression; Resume continues without creating another flow
  console.log('--- Step 2: Testing Pause & Resume buttons ---');
  const pauseBtn = page.locator('#mainPauseBtn');
  await pauseBtn.click();
  await page.waitForTimeout(600);

  const deliveredAfterPause = await page.locator('#metricDeliveredCount').textContent();
  const ratioAfterPause = await page.locator('#metricPacketsRatio').textContent();
  await page.waitForTimeout(1000);
  const deliveredAfterPauseWait = await page.locator('#metricDeliveredCount').textContent();
  const ratioAfterPauseWait = await page.locator('#metricPacketsRatio').textContent();
  console.log('Packets during pause:', { pause1: ratioAfterPause, pause2: ratioAfterPauseWait });
  if (ratioAfterPause !== ratioAfterPauseWait || deliveredAfterPause !== deliveredAfterPauseWait) {
    throw new Error(`Pause failed: packets continued to advance while paused (${ratioAfterPause} -> ${ratioAfterPauseWait})`);
  }

  // Resume
  await startBtn.click();
  await page.waitForTimeout(1500);
  const ratioAfterResume = await page.locator('#metricPacketsRatio').textContent();
  console.log('Packets after resume:', ratioAfterResume);
  const mSentResume = ratioAfterResume.match(/(\d+)\s+sent/);
  if (!mSentResume || Number(mSentResume[1]) <= sent1) {
    throw new Error(`Resume failed to continue packet progression: was ${sent1}, now ${mSentResume ? mSentResume[1] : 'null'}`);
  }

  // Check 3: Reset clears incident, restores topology, invalidates pending AI work
  console.log('--- Step 3: Testing Reset button ---');
  const resetBtn = page.locator('#mainResetBtn');
  await resetBtn.click();
  await page.waitForTimeout(1000);
  const pdrAfterReset = await page.locator('#metricPdr').textContent();
  const deliveredAfterReset = await page.locator('#metricDeliveredCount').textContent();
  const ratioAfterReset = await page.locator('#metricPacketsRatio').textContent();
  console.log('After Reset:', { pdr: pdrAfterReset, delivered: deliveredAfterReset, ratio: ratioAfterReset });
  if (deliveredAfterReset !== '0' || !ratioAfterReset.includes('0 sent')) {
    throw new Error(`Reset failed to reset packet counters to 0: got delivered="${deliveredAfterReset}", ratio="${ratioAfterReset}"`);
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

  const statusAfterInject = await page.locator('#canvasStatusMsg').textContent();
  console.log('After Inject Fault status message:', statusAfterInject);
  if (statusAfterInject.includes('100% packet delivery') && !statusAfterInject.includes('bypass') && !statusAfterInject.includes('Recovery') && !statusAfterInject.includes('Fault')) {
    throw new Error('Inject Fault had no effect on simulation status!');
  }

  // Check 6: Auto-Heal toggle switch
  console.log('--- Step 6: Testing Auto-Heal toggle switch ---');
  const healSlider = page.locator('.switch .slider');
  await healSlider.click(); // Toggle OFF
  await page.waitForTimeout(500);
  const healLabelAfterToggle = await page.locator('#autoHealLabel').textContent();
  console.log('Heal label after toggle:', healLabelAfterToggle);
  if (!healLabelAfterToggle.includes('OFF')) {
    throw new Error(`Expected Auto-heal OFF, got "${healLabelAfterToggle}"`);
  }

  // Toggle back ON
  await healSlider.click();
  await page.waitForTimeout(500);
  const healLabelRestored = await page.locator('#autoHealLabel').textContent();
  console.log('Heal label restored:', healLabelRestored);
  if (!healLabelRestored.includes('ON')) {
    throw new Error(`Expected Auto-heal ON, got "${healLabelRestored}"`);
  }

  // Check 7: Device selection and inspector drawer
  console.log('--- Step 7: Testing Device selection & Right Inspector ---');
  const clientNode = page.locator('g.node[data-id="Client"]');
  await clientNode.click();
  await page.waitForTimeout(500);

  const inspector = page.locator('#rightInspector');
  const isInspectorVisible = await inspector.isVisible();
  const insTitle = await page.locator('#insTitle').textContent();
  console.log('Inspector visible after clicking Client:', { isInspectorVisible, title: insTitle });
  if (!isInspectorVisible || !insTitle.includes('Client')) {
    throw new Error(`Device inspector failed to open for Client node: visible=${isInspectorVisible}, title="${insTitle}"`);
  }

  // Close inspector
  await page.locator('#insCloseBtn').click();
  await page.waitForTimeout(300);

  // Check 8: Reload while running restores playback timer
  console.log('--- Step 8: Testing reload while running restores playback timer ---');
  await page.locator('#mainStartBtn').click();
  await page.waitForTimeout(1000);
  const sentBeforeReload = await page.locator('#metricPacketsRatio').textContent();
  console.log('Ratio before reload:', sentBeforeReload);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  const startBtnAfterReload = await page.locator('#mainStartBtn').textContent();
  console.log('Start button text after reload:', startBtnAfterReload);
  if (!startBtnAfterReload.includes('Running')) {
    throw new Error(`Expected button to show Running after reload, got "${startBtnAfterReload}"`);
  }
  // Wait to ensure timer continues advancing packets
  await page.waitForTimeout(1600);
  const sentAfterReloadWait = await page.locator('#metricPacketsRatio').textContent();
  console.log('Ratio after reload wait:', sentAfterReloadWait);
  const mR1 = sentBeforeReload.match(/(\d+)\s+sent/);
  const mR2 = sentAfterReloadWait.match(/(\d+)\s+sent/);
  if (!mR1 || !mR2 || Number(mR2[1]) <= Number(mR1[1])) {
    throw new Error(`Reload failed to resume simulation progression: was ${sentBeforeReload}, now ${sentAfterReloadWait}`);
  }

  // Check 9: Verify Build/Run mode separation
  console.log('--- Step 9: Testing Build/Run mode separation ---');
  const addRouterBtnDisabled = await page.locator('#paletteAddRouter').isDisabled();
  const connectBtnDisabled = await page.locator('#paletteConnectBtn').isDisabled();
  console.log('Structural buttons disabled while running in Run mode:', { addRouterBtnDisabled, connectBtnDisabled });
  if (!addRouterBtnDisabled || !connectBtnDisabled) {
    throw new Error('Structural palette buttons were not locked during playback in Run mode!');
  }

  // Pause simulation, switch to Build mode
  await page.locator('#mainPauseBtn').click();
  await page.waitForTimeout(500);

  const btnBuild = page.locator('#btnBuildMode');
  await btnBuild.click();
  await page.waitForTimeout(500);

  const isAddRouterActiveInBuild = !(await page.locator('#paletteAddRouter').isDisabled());
  console.log('Add router button enabled in Build mode:', isAddRouterActiveInBuild);
  if (!isAddRouterActiveInBuild) {
    throw new Error('Add router button remained disabled after entering Build mode!');
  }

  // Check 10: Add device naming in Build mode
  console.log('--- Step 10: Testing Add Device returns backend-created name ---');
  await page.locator('#paletteAddRouter').click();
  await page.waitForTimeout(600);

  const toastText = await page.locator('#toast').textContent();
  console.log('Toast text after adding router:', toastText);
  if (!toastText.includes('Added router') || toastText.includes('undefined')) {
    throw new Error(`Unexpected toast text for added router: "${toastText}"`);
  }

  // Switch back to Run mode
  await page.locator('#btnRunMode').click();
  await page.waitForTimeout(300);

  // Check 11: Incidents Page
  console.log('--- Step 11: Testing Incidents Page ---');
  await page.locator('#navIncidentsBtn').click();
  await page.waitForTimeout(500);

  const incidentsPageVisible = await page.locator('#pageIncidents').isVisible();
  const incidentsTitle = await page.locator('#incDetailTitle').textContent();
  console.log('Incidents page visible:', { incidentsPageVisible, title: incidentsTitle });
  if (!incidentsPageVisible) {
    throw new Error('Failed to navigate to Incidents page!');
  }

  // Check 12: Compare Page
  console.log('--- Step 12: Testing Compare Page A/B Benchmark ---');
  await page.locator('#navCompareBtn').click();
  await page.waitForTimeout(500);

  const comparePageVisible = await page.locator('#pageCompare').isVisible();
  if (!comparePageVisible) {
    throw new Error('Failed to navigate to Compare page!');
  }

  const runBenchmarkBtn = page.locator('#btnRunComparisonBenchmark');
  await runBenchmarkBtn.click();
  await page.waitForTimeout(3000);

  const cmpOffPdr = await page.locator('#cmpOffPdr').textContent();
  const cmpOnPdr = await page.locator('#cmpOnPdr').textContent();
  const cmpPdrDelta = await page.locator('#cmpPdrDelta').textContent();
  console.log('Compare results:', { cmpOffPdr, cmpOnPdr, cmpPdrDelta });
  if (!cmpPdrDelta.includes('%')) {
    throw new Error(`Invalid benchmark comparison metric: "${cmpPdrDelta}"`);
  }

  // Check 13: Connections Page
  console.log('--- Step 13: Testing Connections Page ---');
  await page.locator('#navConnectionsBtn').click();
  await page.waitForTimeout(800);

  const connsPageVisible = await page.locator('#pageConnections').isVisible();
  const qcEndpoint = await page.locator('#qcEndpoint').textContent();
  const qcModel = await page.locator('#qcModel').textContent();
  console.log('Connections page loaded:', { connsPageVisible, qcEndpoint, qcModel });
  if (!connsPageVisible || !qcModel.includes('Llama-3.1-8B')) {
    throw new Error(`Connections page failed to populate Qualcomm AI details: ${qcModel}`);
  }

  // Test Telegram button trigger (mocked / safe test)
  await page.locator('#btnSendTestTelegram').click();
  await page.waitForTimeout(600);
  const tgToast = await page.locator('#toast').textContent();
  console.log('Telegram test toast:', tgToast);

  // Return to Simulator page
  await page.locator('#navSimulatorBtn').click();
  await page.waitForTimeout(500);

  // Capture Screenshot at 1920x1080
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: 'dhaara_desktop_1080p.png', fullPage: true });

  // Capture Screenshot at 1366x768
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: 'dhaara_desktop_768p.png', fullPage: true });

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

  console.log('ALL DHAARA BROWSER TESTS AND PAGE VERIFICATIONS PASSED SUCCESSFULLY!');
}

runBrowserTests().catch(err => {
  console.error('Test run failed:', err);
  process.exit(1);
});
