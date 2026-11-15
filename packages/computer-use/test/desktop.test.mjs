import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const output = result => result.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
test('real GTK desktop: AX, Unicode typing, clipboard, screenshots, and stale bindings', { skip: !process.env.OPENCURIA_CUA_DESKTOP_TEST }, async t => {
  const directory = await mkdtemp(`${tmpdir()}/opencuria-cua-test-`);
  const title = `OpenCuria desktop contract ${process.pid}`;
  const resultFile = `${directory}/saved.txt`;
  const authority = `${directory}/empty-xauthority`;
  await writeFile(authority, "");
  const fixture = spawn('/usr/bin/python3', [fileURLToPath(new URL('./fixtures/gtk.py', import.meta.url))], {
    env: { ...process.env, CUA_TEST_TITLE: title, CUA_TEST_RESULT: resultFile, NO_AT_BRIDGE: '0' }, stdio: 'ignore',
  });
  const client = new Client({ name: 'real-desktop-contract', version: '1' });
  t.after(async () => { await client.close(); fixture.kill(); await rm(directory, { recursive: true, force: true }); });
  await client.connect(new StdioClientTransport({ command: process.execPath, args: [fileURLToPath(new URL('../dist/server.js', import.meta.url))], env: {
    DISPLAY: process.env.DISPLAY, XAUTHORITY: process.env.XAUTHORITY ?? authority, DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS,
    OPENCURIA_CUA_SESSION_ID: title, OPENCURIA_CUA_TURN_ID: title,
  }, stderr: 'pipe' }));
  async function js(code, expectError = false) {
    const result = await client.callTool({ name: 'js', arguments: { code } });
    assert.equal(!!result.isError, expectError, output(result));
    return result;
  }
  let inventory;
  for (let tries = 0; tries < 50; tries++) {
    inventory = await js('nodeRepl.write(await cua.listWindows({emit:false}));');
    if (output(inventory).includes(title)) break;
    await new Promise(r => setTimeout(r, 100));
  }
  assert.match(output(inventory), new RegExp(title));
  await js(`var window = (await cua.listWindows({emit:false})).find(w=>w.title === ${JSON.stringify(title)}); var app = await cua.getApp({windowId:window.id});`);
  const state = output(await js('var state = await app.getAXState({emit:false}); nodeRepl.write(state);'));
  assert.match(state, /Accessibility source: at_spi/);
  const referenceRoot = process.env.CUA_REFERENCE_ROOT ?? '/usr/lib/chatgpt/resources/cua_node';
  const helperPath = `${referenceRoot}/lib/node_modules/@oai/sky/bin/linux/sky_linux_x64`;
  if (existsSync(helperPath)) {
    const helper = spawn(helperPath, ['server'], { stdio: ['pipe', 'pipe', 'ignore'] });
    const lines = createInterface({ input: helper.stdout })[Symbol.asyncIterator]();
    try {
      const metadata = JSON.parse(output(await js('nodeRepl.write(JSON.stringify(window));')));
      helper.stdin.write(JSON.stringify({ id: 1, command: 'get_window_state', input: { window: metadata, include_screenshot: false } }) + '\n');
      const response = JSON.parse((await lines.next()).value);
      assert.equal(response.ok, true);
      const moduleRoot = `${referenceRoot}/lib/node_modules/@oai/sky/dist/project/cua/sky_js/src/targets/linux`;
      const { accessibility_tree } = await import(`${moduleRoot}/accessibility_tree.js`);
      const { ElementIds } = await import(`${moduleRoot}/element_ids.js`);
      new ElementIds().assign(response.output.ax_tree, metadata, false);
      assert.equal(state, `Accessibility source: ${response.output.ax_tree_source}\n${accessibility_tree(response.output.ax_tree).to_string()}`);
      helper.stdin.write(JSON.stringify({ id: 2, command: 'list_apps', input: {} }) + '\n');
      const apps = JSON.parse((await lines.next()).value).output.map(app => ({ id: app.id, displayName: app.name, isRunning: app.windows.length > 0, windows: app.windows }));
      assert.deepEqual(JSON.parse(output(await js('nodeRepl.write(JSON.stringify(await cua.listApps({emit:false})));'))), apps);
    } finally {
      helper.kill();
      await lines.return();
    }
  }
  const entry = Number(state.match(/(\d+) text .*Verification text/)[1]);
  const button = Number(state.match(/(\d+) push button Save verification/)[1]);
  await js(`await app.typeText('Semantik äöü', ${entry}); await app.click(${button});`);
  assert.equal(await readFile(resultFile, 'utf8'), 'Semantik äöü');
  await js(`await app.click(${entry}).catch(()=>{}); await app.pressKey('Control_L+a'); await app.typeText('Clipboard ✅ äöü'); await app.click(${button});`);
  assert.equal(await readFile(resultFile, 'utf8'), 'Clipboard ✅ äöü');
  await js(`await app.pressKey('Control_L+a'); await app.typeText('Semantic replacement äöü', ${entry}); await app.click(${button});`);
  assert.equal(await readFile(resultFile, 'utf8'), 'Semantic replacement äöü');
  await js(`await app.pressKey('Control_L+a'); await app.typeText('Coordinates correct', ${entry}); await app.getScreenshot({emit:false}); await app.click([300,90]);`);
  assert.equal(await readFile(resultFile, 'utf8'), 'Coordinates correct');
  await js('await app.click([300,90]);');
  const screenshot = await js('await app.getAXStateAndScreenshot();');
  assert.equal(screenshot.content.filter(b => b.type === 'image').length, 1);
  assert.match(output(screenshot), /Saved: Coordinates correct/);
  if (process.env.CUA_TEST_SCREENSHOT) {
    await writeFile(process.env.CUA_TEST_SCREENSHOT, Buffer.from(screenshot.content.find(b => b.type === 'image').data, 'base64'));
  }
  await js('var old = app; var app = await cua.getApp({windowId:window.id});');
  assert.match(output(await js('await old.getAXState();')), /Accessibility source: at_spi/);
  await client.callTool({ name: 'js_reset', arguments: {} });
  assert.match(output(await js('nodeRepl.write(typeof app);')), /undefined/);
});
