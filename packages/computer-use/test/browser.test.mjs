import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const chromePath = process.env.CUA_TEST_CHROME ?? '/opt/google/chrome/chrome';
const output = result => result.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
test('real Chrome: navigate with native keys and paste, without activation or browser automation', {
  skip: !process.env.OPENCURIA_CUA_DESKTOP_TEST || !existsSync(chromePath),
}, async t => {
  const directory = await mkdtemp('/tmp/opencuria-cua-chrome-');
  const windowClass = `OpenCuriaBrowserTest${process.pid}`;
  const server = createServer((_, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>OpenCuria native browser verified</title><h1>Computer Use browser verification</h1><p>Navigation succeeded through native keyboard input.</p>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const chrome = spawn(chromePath, [
    '--no-sandbox', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--password-store=basic', '--use-mock-keychain',
    '--disable-dev-shm-usage', `--user-data-dir=${directory}`, `--class=${windowClass}`,
    '--window-position=20,20', '--window-size=1000,700', 'about:blank',
  ], { stdio: 'ignore' });
  const client = new Client({ name: 'native-browser-contract', version: '1' });
  t.after(async () => {
    await client.close(); chrome.kill();
    await new Promise(resolve => chrome.exitCode !== null ? resolve() : chrome.once('exit', resolve));
    await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  await client.connect(new StdioClientTransport({
    command: process.execPath, args: [fileURLToPath(new URL('../dist/server.js', import.meta.url))],
    env: {
      PATH: process.env.PATH, DISPLAY: process.env.DISPLAY, XAUTHORITY: process.env.XAUTHORITY,
      DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS,
      OPENCURIA_CUA_SESSION_ID: windowClass, OPENCURIA_CUA_TURN_ID: windowClass,
    }, stderr: 'pipe',
  }));
  async function js(code) {
    const result = await client.callTool({ name: 'js', arguments: { code } });
    assert.equal(result.isError, false, output(result));
    return result;
  }
  let windows;
  for (let attempt = 0; attempt < 50; attempt++) {
    windows = output(await js('nodeRepl.write(await cua.listWindows({emit:false}));'));
    if (windows.includes(windowClass)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(windows.includes(windowClass));
  const url = `http://127.0.0.1:${server.address().port}/`;
  await js(`var window = (await cua.listWindows({emit:false})).find(w=>w.app===${JSON.stringify(windowClass)}); var app = await cua.getApp({windowId:window.id});`);
  await js(`await app.pressKey('ctrl+l'); await app.typeText(${JSON.stringify(url)}); await app.pressKey('Return');`);
  let observed;
  for (let attempt = 0; attempt < 30; attempt++) {
    observed = output(await js('await cua.listWindows();'));
    if (observed.includes('OpenCuria native browser verified')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(observed.includes('OpenCuria native browser verified'), observed);
  const screenshot = await js('await app.getAXStateAndScreenshot();');
  assert.equal(screenshot.content.filter(block => block.type === 'image').length, 1);
});
