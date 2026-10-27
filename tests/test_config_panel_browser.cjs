// Device-panel routing with a real Chromium DOM and simulated HA only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../custom_components/vega/www/vega');

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://vega.test/**', route => {
      const name = path.basename(new URL(route.request().url()).pathname);
      if (name.endsWith('.js')) return route.fulfill({contentType: 'application/javascript', body: fs.readFileSync(path.join(root, name))});
      return route.fulfill({contentType: 'text/html', body: '<style>body{margin:0;font-family:sans-serif;--primary-color:#03a9f4;--primary-text-color:#eee;--primary-background-color:#151515;--card-background-color:#222;--secondary-text-color:#aaa;--divider-color:#444}ha-card{display:block}</style>'});
    });
    await page.goto('http://vega.test/');
    await page.evaluate(() => {
      window.calls = [];
      window.registry = ['one', 'two'].map(entry => ({platform: 'vega', config_entry_id: entry,
        entity_id: `sensor.renamed_${entry}`, unique_id: `vega_${entry}_can_scripts`, device_id: `device-${entry}`, disabled_by: null}));
      const state = entry => ({state: '1', attributes: {entry_id: entry, scenarios_read_at: 'today',
        command_bindings: {preheat: {setting_id: 1347, value: 1, scenario_routes: [{scenario: 3}]}},
        scenarios: [{index: 3, editable: true, slot: 4, raw: 'aa'.repeat(50), conditions: [{sensor: 1347}],
          document: {conditions: [{sensor: 1347, comparison: 'greater', value: 0}, null], match: 'any', actions: {
            external_output_on: 1, external_output_on_seconds: 0, external_output_off: 7, external_output_off_seconds: 0, can_script: 4}}}],
        slots: [{slot: 4, name: entry, repeat: 1, frames: [], raw: 'aa'.repeat(82), editable: true}]}});
      window.hass = {states: {'sensor.renamed_one': state('one'), 'sensor.renamed_two': state('two')}, callWS: async msg => {
        calls.push(msg);
        if (msg.type === 'config/entity_registry/list') return registry;
        return {response: {verified: true, written: ['4001/3'], slots: 8, scenarios_read: true}};
      }};
      window.panel = document.createElement('vega-config-panel');
      panel.route = {path: '/one'};
      panel.hass = hass;
      document.body.appendChild(panel);
    });
    // Match HA's lazy loading: route/hass are assigned before customElements.define.
    await page.addScriptTag({type: 'module', url: 'http://vega.test/vega-config-panel.js?v=test'});
    await page.evaluate(() => customElements.whenDefined('vega-config-panel'));
    await page.locator('vega-can-editor').waitFor();
    assert.equal(await page.locator('vega-can-editor').evaluate(e => e._config.entity), 'sensor.renamed_one');
    assert.equal(await page.locator('#back').getAttribute('href'), '/config/devices/device/device-one');
    assert.equal(await page.evaluate(() => calls.filter(c => c.type === 'call_service').length), 0);
    await page.locator('#read').click();
    assert.equal(await page.evaluate(() => calls.at(-1).service_data.entry_id), 'one');
    await page.locator('.command-row button').click();
    const draft = (await page.locator('#slot-json').inputValue()).replace('"repeat": 1', '"repeat": 2');
    await page.locator('#slot-json').fill(draft);
    await page.evaluate(() => { panel.hass = {...hass}; });
    assert.equal(await page.locator('#slot-json').inputValue(), draft);
    await page.locator('#save-btn').click();
    await page.locator('#slot-json').waitFor({state: 'detached'});
    assert.equal(await page.evaluate(() => calls.at(-1).service), 'write_commands');
    assert.equal(await page.evaluate(() => calls.at(-1).service_data.entry_id), 'one');
    // Loading the same editor from a later dashboard/module version is harmless.
    await page.evaluate(async () => { await import('http://vega.test/vega-can-editor.js?v=another'); });
    assert.equal(await page.evaluate(() => customCards.filter(c => c.type === 'vega-can-editor').length), 1);
    // Responses from an old route must not overwrite the selected device.
    await page.evaluate(() => {
      window.pending = [];
      panel.hass = {...hass, callWS: () => new Promise(resolve => pending.push(resolve))};
      panel.route = {path: '/missing'};
      panel.route = {path: '/two'};
      pending[1](registry);
      pending[0]([]);
    });
    await page.waitForFunction(() => panel.shadowRoot.querySelector('vega-can-editor')?._config.entity === 'sensor.renamed_two');
    assert.equal(await page.locator('#back').getAttribute('href'), '/config/devices/device/device-two');
    await page.evaluate(() => { panel.hass = hass; panel.route = {path: '/missing'}; });
    await page.waitForFunction(() => panel.shadowRoot.querySelector('#status').textContent.includes('не найден'));
    assert.equal(await page.locator('vega-can-editor:visible').count(), 0);
    await page.evaluate(() => { registry[0].disabled_by = 'user'; panel.route = {path: '/one'}; });
    await page.waitForFunction(() => panel.shadowRoot.querySelector('#status').textContent.includes('отключён'));
    assert.equal(await page.locator('vega-can-editor:visible').count(), 0);
    await page.evaluate(() => { registry[0].disabled_by = null; delete hass.states['sensor.renamed_one']; });
    await page.locator('#retry').click();
    await page.waitForFunction(() => panel.shadowRoot.querySelector('#status').textContent.includes('недоступны'));
    await page.evaluate(() => { hass.states['sensor.renamed_one'] = structuredClone(hass.states['sensor.renamed_two']); panel.hass = hass; });
    assert.equal(await page.locator('vega-can-editor:visible').count(), 0); // Wrong entry in state is never accepted.
    await page.evaluate(() => { hass.states['sensor.renamed_one'].attributes.entry_id = 'one'; panel.hass = hass; });
    await page.locator('vega-can-editor:visible').waitFor();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('.command-row button').click();
    const box = await page.locator('.modal-box').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.height <= 844);
    if (process.env.SCREENSHOT) await page.screenshot({path: process.env.SCREENSHOT, animations: 'disabled'});
    await page.locator('#close-btn').click();
    assert.deepEqual(errors, []);
    console.log('PASS panel Chromium: lazy upgrade, direct load, renamed entity, correct tracker read/write, draft, duplicate imports, stale routes, missing/disabled entity, mobile modal');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
