import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const output = result => result.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
const delay = () => new Promise(resolve => setTimeout(resolve, 100));

async function desktopEnvironment() {
  const environment = { ...process.env, NO_AT_BRIDGE: '0' };
  if (!environment.DBUS_SESSION_BUS_ADDRESS) {
    for (const pid of await readdir('/proc')) {
      if (!/^\d+$/.test(pid)) continue;
      try {
        const command = await readFile(`/proc/${pid}/cmdline`, 'utf8');
        if (!command.startsWith('xfce4-session\0')) continue;
        const values = Object.fromEntries((await readFile(`/proc/${pid}/environ`, 'utf8')).split('\0').filter(v => v.includes('=')).map(v => [v.slice(0, v.indexOf('=')), v.slice(v.indexOf('=') + 1)]));
        if (values.DISPLAY === environment.DISPLAY && values.DBUS_SESSION_BUS_ADDRESS) {
          environment.DBUS_SESSION_BUS_ADDRESS = values.DBUS_SESSION_BUS_ADDRESS;
          break;
        }
      } catch {}
    }
  }
  // The fixed desktop session does this before starting any user application.
  // Do it independently of the MCP driver to cover apps opened before the agent.
  for (const property of ['IsEnabled', 'ScreenReaderEnabled']) {
    execFileSync('gdbus', ['call', '--session', '--dest', 'org.a11y.Bus', '--object-path', '/org/a11y/bus', '--method', 'org.freedesktop.DBus.Properties.Set', 'org.a11y.Status', property, '<true>'], { env: environment, timeout: 5000 });
  }
  return environment;
}

async function connect(environment, title) {
  const client = new Client({ name: 'accessibility-desktop-regression', version: '1' });
  await client.connect(new StdioClientTransport({ command: process.env.CUA_TEST_NODE ?? process.execPath, args: [process.env.CUA_TEST_SERVER ?? fileURLToPath(new URL('../dist/server.js', import.meta.url))], env: {
    DISPLAY: environment.DISPLAY, XAUTHORITY: environment.XAUTHORITY ?? '/root/.Xauthority', DBUS_SESSION_BUS_ADDRESS: environment.DBUS_SESSION_BUS_ADDRESS,
    OPENCURIA_CUA_SESSION_ID: title, OPENCURIA_CUA_TURN_ID: title,
  }, stderr: 'pipe' }));
  const initial = await client.callTool({ name: 'js', arguments: { code: 'await cua.listWindows({emit:false});' } });
  assert.equal(!!initial.isError, false, output(initial));
  return {
    client,
    async js(code) {
      const result = await client.callTool({ name: 'js', arguments: { code } });
      assert.equal(!!result.isError, false, output(result));
      return result;
    },
  };
}

async function bindWindow(js, title) {
  let inventory = [];
  for (let tries = 0; tries < 100; tries++) {
    inventory = JSON.parse(output(await js('nodeRepl.write(JSON.stringify(await cua.listWindows({emit:false})));')));
    if (inventory.some(window => window.title?.includes(title))) break;
    await delay();
  }
  const matching = inventory.filter(window => window.title?.includes(title));
  assert.equal(matching.length, 1, `Expected exactly one fixture window: ${JSON.stringify(inventory)}`);
  const initial = output(await js(`var app = await cua.getApp({windowId:${matching[0].id}});`));
  assert.match(initial, /\d+ (?:entry|text) \([^\n)]*settable[^\n)]*\) Verification text/, 'The initial getApp observation must expose the editable field');
  assert.match(initial, /\d+ push button Save verification/);
  let state = '';
  for (let tries = 0; tries < 50; tries++) {
    state = output(await js('nodeRepl.write(await app.getAXState({emit:false}));'));
    if (/\d+ (?:entry|text) \([^\n)]*settable[^\n)]*\) Verification text/.test(state) && /\d+ push button Save verification/.test(state)) break;
    await delay();
  }
  assert.match(state, /Accessibility source: at_spi/);
  assert.match(state, /\d+ (?:entry|text) \([^\n)]*settable[^\n)]*\) Verification text/);
  assert.match(state, /\d+ push button Save verification/);
  return state;
}

async function saveByAccessibility(js, state, value) {
  const entry = Number(state.match(/(\d+) (?:entry|text) \([^\n)]*settable[^\n)]*\) Verification text/)[1]);
  await js(`await app.typeText(${JSON.stringify(value)},${entry}); await app.typeText(' fertig',${entry});`);
  value += ' fertig';
  // Re-read after editing: never carry an element ID across an observation.
  state = output(await js('nodeRepl.write(await app.getAXState({emit:false}));'));
  const button = Number(state.match(/(\d+) push button Save verification/)[1]);
  await js(`await app.click(${button});`);
  let saved = '';
  for (let tries = 0; tries < 30; tries++) {
    saved = output(await js('nodeRepl.write(await app.getAXState({emit:false}));'));
    if (saved.includes(`Saved: ${value}`)) break;
    await delay();
  }
  assert.ok(saved.includes(`Saved: ${value}`), saved);
  return saved;
}

test('real Chrome: manually opened and agent-launched windows expose working web AX controls without CDP or force flags', { skip: !process.env.OPENCURIA_CUA_DESKTOP_TEST, timeout: 90000 }, async t => {
  const directory = await mkdtemp(`${tmpdir()}/opencuria-ax-chrome-`);
  const environment = await desktopEnvironment();
  const chrome = process.env.CUA_TEST_CHROME ?? '/usr/bin/google-chrome-stable';
  t.after(() => rm(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  for (const mode of ['manual', 'agent']) {
    await t.test(mode, async t => {
      const title = `OpenCuria AX ${mode} ${process.pid}`;
      const page = `${directory}/${mode}.html`;
      await writeFile(page, `<!doctype html><title>${title}</title><body><h1>${title}</h1><label>Verification text<input aria-label="Verification text" id="entry"></label><button onclick="document.getElementById('result').textContent='Saved: '+document.getElementById('entry').value">Save verification</button><p id="result">Not saved</p>`);
      const args = ['--no-sandbox', '--no-first-run', '--disable-dev-shm-usage', `--user-data-dir=${directory}/${mode}-profile`, '--new-window', pathToFileURL(page).href];
      let processId;
      let child;
      let client;
      t.after(async () => {
        if (client) await client.close();
        if (!processId) {
          try { processId = Number(await readFile(`${directory}/agent.pid`, 'utf8')); } catch {}
        }
        if (processId) {
          try { process.kill(-processId, 'SIGTERM'); } catch {}
        }
      });
      if (mode === 'manual') {
        // Start before the MCP client: this is the user's already-open browser.
        child = spawn(chrome, args, { env: environment, detached: true, stdio: 'ignore' });
        processId = child.pid;
        let opened = false;
        for (let tries = 0; tries < 100; tries++) {
          try {
            execFileSync('/usr/bin/xdotool', ['search', '--onlyvisible', '--name', title], { env: environment, stdio: 'ignore' });
            opened = true; break;
          } catch {}
          await delay();
        }
        assert.ok(opened, 'The manual browser must be visible before the MCP client starts');
      }
      const connection = await connect(environment, title);
      client = connection.client;
      const { js } = connection;
      if (mode === 'agent') {
        const launcher = `${directory}/launch-chrome`;
        await writeFile(launcher, `#!/bin/sh\necho $$ > '${directory}/agent.pid'\nexec '${chrome}' ${args.map(arg => `'${arg.replaceAll("'", "'\\''")}'`).join(' ')}\n`, { mode: 0o755 });
        const desktop = `${directory}/chrome.desktop`;
        await writeFile(desktop, `[Desktop Entry]\nType=Application\nName=${title}\nExec=/usr/bin/setsid ${launcher}\nTerminal=false\n`);
        await js(`await cua.computer.launch_app({app:${JSON.stringify(desktop)}});`);
      }
      const state = await bindWindow(js, title);
      const saved = await saveByAccessibility(js, state, `${mode} äöü ✅ 🧪`);
      if (process.env.CUA_TEST_CHROME_SCREENSHOT && mode === 'manual') {
        const screenshot = await js('await app.getAXStateAndScreenshot();');
        await writeFile(`${process.env.CUA_TEST_CHROME_SCREENSHOT}.ax.txt`, saved);
        await writeFile(process.env.CUA_TEST_CHROME_SCREENSHOT, Buffer.from(screenshot.content.find(block => block.type === 'image').data, 'base64'));
      }
    });
  }
});

test('real Qt: pre-existing native window exposes actionable AX controls', { skip: !process.env.OPENCURIA_CUA_DESKTOP_TEST, timeout: 45000 }, async t => {
  try { execFileSync('/usr/bin/python3', ['-c', 'import PyQt5'], { stdio: 'ignore' }); }
  catch { t.skip('Install python3-pyqt5 to verify the Qt accessibility bridge'); return; }
  const directory = await mkdtemp(`${tmpdir()}/opencuria-ax-qt-`);
  const environment = await desktopEnvironment();
  const title = `OpenCuria Qt AX ${process.pid}`;
  const resultFile = `${directory}/saved.txt`;
  const fixture = spawn('/usr/bin/python3', [fileURLToPath(new URL('./fixtures/qt.py', import.meta.url))], { env: { ...environment, CUA_TEST_TITLE: title, CUA_TEST_RESULT: resultFile }, stdio: 'ignore' });
  const { client, js } = await connect(environment, title);
  t.after(async () => { await client.close(); fixture.kill(); await rm(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); });
  const state = await bindWindow(js, title);
  await saveByAccessibility(js, state, 'Qt äöü ✅ 🧪');
  assert.equal(await readFile(resultFile, 'utf8'), 'Qt äöü ✅ 🧪 fertig');
});

test('real Thunar: attach to an existing file manager and open a folder by AX index', { skip: !process.env.OPENCURIA_CUA_DESKTOP_TEST, timeout: 45000 }, async t => {
  const { mkdir } = await import('node:fs/promises');
  const directory = await mkdtemp(`${tmpdir()}/opencuria-ax-thunar-`);
  await mkdir(`${directory}/Semantic folder`);
  const environment = await desktopEnvironment();
  const title = directory.slice(directory.lastIndexOf('/') + 1);
  let windowId;
  let client;
  const fixture = spawn('/usr/bin/thunar', [directory], { env: environment, stdio: 'ignore' });
  t.after(async () => {
    if (client) await client.close();
    if (windowId) {
      try { execFileSync('/usr/bin/xdotool', ['windowclose', String(windowId)], { env: environment, stdio: 'ignore' }); } catch {}
    }
    fixture.kill();
    await rm(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  });
  let visible = false;
  for (let tries = 0; tries < 100; tries++) {
    try {
      execFileSync('/usr/bin/xdotool', ['search', '--onlyvisible', '--name', title], { env: environment, stdio: 'ignore' });
      visible = true; break;
    } catch {}
    await delay();
  }
  assert.ok(visible, 'Thunar must be visible before the MCP client starts');
  const connection = await connect(environment, title);
  client = connection.client;
  const { js } = connection;
  const windows = JSON.parse(output(await js('nodeRepl.write(JSON.stringify(await cua.listWindows({emit:false})));')));
  const window = windows.find(value => value.title?.includes(title));
  assert.ok(window, JSON.stringify(windows));
  windowId = window.id;
  let state = output(await js(`var app = await cua.getApp({windowId:${windowId}});`));
  assert.match(state, /Accessibility source: at_spi/);
  assert.match(state, /menu bar/);
  // Thunar's icon view does not expose its items through AT-SPI. Its standard
  // list view does, and the view switch itself is a semantic button action.
  const listView = state.match(/(\d+) push button List View/);
  assert.ok(listView, state);
  await js(`await app.click(${Number(listView[1])});`);
  let folder;
  for (let tries = 0; tries < 30; tries++) {
    state = output(await js('nodeRepl.write(await app.getAXState({emit:false}));'));
    folder = state.match(/(\d+) (?:icon|table cell) (?:\([^\n)]*\) )?Semantic folder/);
    if (folder) break;
    await delay();
  }
  assert.ok(folder, state);
  await js(`await app.click(${Number(folder[1])}); await app.pressKey('Return');`);
  state = output(await js('nodeRepl.write(await app.getAXState({emit:false}));'));
  assert.match(state, /Semantic folder.*(?:Thunar|File Manager)/);
  if (process.env.CUA_TEST_THUNAR_SCREENSHOT) {
    const screenshot = await js('await app.getAXStateAndScreenshot();');
    await writeFile(`${process.env.CUA_TEST_THUNAR_SCREENSHOT}.ax.txt`, state);
    await writeFile(process.env.CUA_TEST_THUNAR_SCREENSHOT, Buffer.from(screenshot.content.find(block => block.type === 'image').data, 'base64'));
  }
  const iconView = state.match(/(\d+) push button Icon View/);
  if (iconView) await js(`await app.click(${Number(iconView[1])});`);
});
