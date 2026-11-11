import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

const fixture = spawn(process.env.PYTHON || 'python3', ['tests/browser_fixture.py'], { stdio: ['pipe', 'pipe', 'pipe'] });
let fixtureError = '';
fixture.stderr.on('data', data => { fixtureError += data; });
const lines = createInterface({ input: fixture.stdout });
const connection = await Promise.race([
  once(lines, 'line').then(([line]) => JSON.parse(line)),
  once(fixture, 'exit').then(([code]) => { throw new Error(`Fixture exited ${code}: ${fixtureError}`); }),
  new Promise((_, reject) => setTimeout(() => reject(new Error(`Fixture startup timeout: ${fixtureError}`)), 15000).unref()),
]);
const browser = await chromium.launch({ headless: true });
const errors = [];
let context;
const control = async mode => {
  assert.equal((await fetch(connection.control, { method: 'POST', body: JSON.stringify({ mode }) })).status, 200);
};
const state = async () => (await fetch(`${connection.url}/api/state`)).json();
const config = async () => (await fetch(`${connection.url}/api/config`)).json();
const waitUntil = async (fn, description) => {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (await fn()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${description}`);
};
try {
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    context = await browser.newContext({ viewport, acceptDownloads: true });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(connection.url);
    await waitUntil(async () => (await state()).internet.status === 'online', 'initial online');
    await page.getByTestId('internet-banner').waitFor();
    await page.getByTestId('backend-disconnected').waitFor({ state: 'hidden' });
    await waitUntil(async () => (await page.title()) === 'Есть интернет Services Ping', 'initial online title');
    assert.equal(await page.locator('#empty-targets').isVisible(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'no horizontal overflow');

    await page.getByTestId('add-target').click();
    const targetName = `<img src=x onerror=alert(1)> ${viewport.width}`;
    await page.getByTestId('target-name').fill(targetName);
    await page.getByTestId('target-address').fill('https://fixture.example/health');
    await page.getByTestId('target-group').fill('Мои VPS');
    await page.getByTestId('target-save').click();
    await waitUntil(async () => (await state()).targets.some(target => target.name === targetName), 'created');
    await page.getByText(targetName, { exact: true }).waitFor();
    assert.equal(await page.locator('img[src="x"]').count(), 0, 'safe stored name');
    const target = (await config()).targets.find(item => item.name === targetName);
    const card = page.locator(`[data-target-id="${target.id}"]`);

    await card.getByRole('button', { name: /Изменить/i }).click();
    await page.getByTestId('target-kind').selectOption('tcp');
    await page.getByTestId('target-address').fill('fixture.example:443');
    await page.getByTestId('target-save').click();
    await waitUntil(async () => (await config()).targets.find(item => item.id === target.id)?.kind === 'tcp', 'edited TCP target');
    await card.getByRole('button', { name: /Изменить/i }).click();
    await page.locator('#target-enabled').uncheck();
    await page.getByTestId('target-save').click();
    await waitUntil(async () => (await state()).targets.find(item => item.id === target.id)?.status === 'disabled', 'disabled');
    await card.getByRole('button', { name: /Изменить/i }).click();
    await page.locator('#target-enabled').check();
    await page.getByTestId('target-save').click();

    await control('offline');
    await waitUntil(async () => (await state()).internet.status === 'offline', 'offline');
    await waitUntil(async () => /недоступ|нет интернет/i.test(await page.getByTestId('internet-banner').textContent()), 'offline banner');
    await waitUntil(async () => (await page.title()) === 'Нет интернета Services Ping', 'offline title');
    assert.doesNotMatch(await page.locator('#connectivity-latency').textContent(), /0 мс/);
    await control('partial');
    await waitUntil(async () => (await state()).internet.status === 'partial', 'partial');
    await waitUntil(async () => /частич/i.test(await page.getByTestId('internet-banner').textContent()), 'partial banner');
    await waitUntil(async () => (await page.title()) === 'Частичный доступ Services Ping', 'partial title');
    await control('online');
    await waitUntil(async () => (await state()).internet.status === 'online', 'recovery');
    await waitUntil(async () => (await page.title()) === 'Есть интернет Services Ping', 'online title');

    await page.getByTestId('pause-button').click();
    await waitUntil(async () => (await state()).paused, 'pause');
    await page.getByTestId('check-button').click();
    assert.equal((await state()).paused, true, 'manual preserves pause');
    await page.getByTestId('pause-button').click();
    await waitUntil(async () => !(await state()).paused, 'resume');

    const historyRoute = '**/api/history?*';
    await page.route(historyRoute, route => route.fulfill({ json: { samples: [
      { timestamp: 100, status: 'down', latency_ms: null },
      { timestamp: 101, status: 'up', latency_ms: 7.4 },
      { timestamp: 102, status: 'unsupported' },
    ] } }));
    await card.getByRole('button', { name: /История/i }).click();
    await page.getByTestId('history-chart').waitFor();
    await page.locator('#history-chart circle').waitFor();
    assert.equal(await page.locator('#history-chart circle').count(), 1, 'missing RTT creates no graph point');
    assert.equal(await page.locator('#history-chart circle').getAttribute('data-latency-ms'), '7.4');
    await page.locator('#history-dialog [data-close="history-dialog"]').first().click();
    await page.unroute(historyRoute);

    await page.getByTestId('settings-button').click();
    const settings = page.getByTestId('settings-dialog');
    await settings.locator('[name="history_limit"]').fill('40');
    await page.getByTestId('settings-save').click();
    await waitUntil(async () => (await config()).settings.history_limit === 40, 'saved settings');
    await page.getByTestId('settings-button').click();
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByTestId('export-button').click()]);
    const chunks = await (await download.createReadStream()).toArray();
    const exported = JSON.parse(Buffer.concat(chunks).toString());
    assert.equal(exported.version, 1);
    assert.ok(exported.targets.some(item => item.id === target.id));
    assert.equal('csrf_token' in exported, false);
    await page.locator('#settings-dialog [data-close="settings-dialog"]').first().click();

    await page.getByTestId('settings-button').click();
    await page.getByTestId('settings-dialog').waitFor({ state: 'visible' });
    await page.getByTestId('import-input').setInputFiles({ name: 'config.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(exported)) });
    await page.locator('#import-dialog').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#apply-import').isEnabled(), false, 'explicit import confirmation');
    await page.locator('#confirm-import').check();
    await page.locator('#apply-import').click();
    await page.locator('#import-dialog').waitFor({ state: 'hidden' });
    await page.getByTestId('settings-dialog').waitFor({ state: 'hidden' });

    await card.getByRole('button', { name: /Удалить/i }).click();
    await waitUntil(async () => !(await config()).targets.some(item => item.id === target.id), 'deleted');

    await mkdir('.work', { recursive: true });
    await page.waitForFunction(() => document.getElementById('toast-region').childElementCount === 0);
    await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: `.work/dashboard-${viewport.width}.png`, fullPage: true });
    if (viewport.width === 390) {
      await control('stop_backend');
      await page.getByTestId('backend-disconnected').waitFor({ state: 'visible', timeout: 12000 });
      await waitUntil(async () => (await page.title()) === 'Services Ping', 'title drops internet claim when monitor is down');
    }
    await context.close(); context = null;
  }
  assert.deepEqual(errors, [], 'no JavaScript errors');
  console.log('Browser smoke passed: desktop/mobile, CRUD, safe names, offline/partial/recovery titles, pause/manual, history, settings, export/import, backend-disconnected.');
} finally {
  await context?.close();
  await browser.close();
  fixture.stdin.end();
  const finished = once(fixture, 'exit');
  const timer = setTimeout(() => fixture.kill('SIGKILL'), 5000);
  await finished; clearTimeout(timer); lines.close();
}
