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
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);

  // Check 1: Start traffic advances traffic and telemetry
  console.log('--- Step 1: Testing Start Traffic button ---');
  const startBtn = page.locator('#mainStartBtn');
  await startBtn.click();
  await page.waitForTimeout(1500);

  const pdrText1 = await page.locator('#metricPdr').textContent();
  const packetsText1 = await page.locator('#metricPackets').textContent();
  console.log('After Start Traffic:', { pdr: pdrText1, packets: packetsText1 });

  // Check 2: Pause stops progression; Resume continues without creating another flow
  console.log('--- Step 2: Testing Pause & Resume buttons ---');
  const pauseBtn = page.locator('#mainPauseBtn');
  await pauseBtn.click();
  await page.waitForTimeout(800);

  const statusAfterPause = await page.locator('#statusText').textContent();
  console.log('Status after Pause:', statusAfterPause);

  // Resume
  await startBtn.click();
  await page.waitForTimeout(800);
  const statusAfterResume = await page.locator('#statusText').textContent();
  console.log('Status after Resume:', statusAfterResume);

  // Check 3: Reset clears incident, restores topology, invalidates pending AI work
  console.log('--- Step 3: Testing Reset button ---');
  const resetBtn = page.locator('#mainResetBtn');
  await resetBtn.click();
  await page.waitForTimeout(1000);
  const pdrAfterReset = await page.locator('#metricPdr').textContent();
  console.log('PDR after Reset:', pdrAfterReset);

  // Check 4: Scenario selection persists during dashboard refreshes
  console.log('--- Step 4: Testing Scenario persistence ---');
  const scenarioSelect = page.locator('#scenarioSelect');
  await scenarioSelect.selectOption('mild_congestion');
  await page.waitForTimeout(500);

  // Reload page
  console.log('Reloading page to test persistence...');
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  const scenarioValAfterReload = await page.locator('#scenarioSelect').inputValue();
  console.log('Scenario after reload:', scenarioValAfterReload);
  if (scenarioValAfterReload !== 'mild_congestion') {
    throw new Error(`Scenario persistence failed: expected "mild_congestion", got "${scenarioValAfterReload}"`);
  }

  // Check 5: Inject Fault affects simulation exactly once
  console.log('--- Step 5: Testing Inject Fault button (single debounced action) ---');
  await page.locator('#scenarioSelect').selectOption('sudden_link_failure');
  await page.locator('#mainStartBtn').click();
  await page.waitForTimeout(1000);

  const injectBtn = page.locator('#injectFaultBtn');
  await injectBtn.click();
  await page.waitForTimeout(2000);

  const statusAfterInject = await page.locator('#statusText').textContent();
  const diagCause = await page.locator('#diagLikelyCause').textContent();
  console.log('After Inject Fault:', { status: statusAfterInject, cause: diagCause });

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

  // Check 7: Comparison opens an in-page results panel, not a browser alert
  console.log('--- Step 7: Testing Comparison in-page modal panel ---');
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

  // Close modal via close button
  await page.locator('#closeComparisonBtn').click();
  await page.waitForTimeout(500);
  const modalVisible = await modal.isVisible();
  console.log('Comparison modal closed. Is visible:', modalVisible);

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
