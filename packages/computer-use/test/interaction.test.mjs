import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const desktop = !!process.env.OPENCURIA_CUA_DESKTOP_TEST;
const chrome = process.env.CUA_TEST_CHROME ?? '/opt/google/chrome/chrome';
const output = (result) => result.content.filter((block) => block.type === 'text').map((block) => block.text).join('\n');
const fixture = (name) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

async function environment() {
  const values = { ...process.env, NO_AT_BRIDGE: '0' };
  if (!values.DBUS_SESSION_BUS_ADDRESS) {
    for (const pid of await readdir('/proc')) {
      if (!/^\d+$/.test(pid)) continue;
      try {
        if (!(await readFile(`/proc/${pid}/cmdline`, 'utf8')).startsWith('xfce4-session\0')) continue;
        const session = Object.fromEntries((await readFile(`/proc/${pid}/environ`, 'utf8')).split('\0').filter((v) => v.includes('=')).map((v) => [v.slice(0, v.indexOf('=')), v.slice(v.indexOf('=') + 1)]));
        if (session.DISPLAY === values.DISPLAY && session.DBUS_SESSION_BUS_ADDRESS) { values.DBUS_SESSION_BUS_ADDRESS = session.DBUS_SESSION_BUS_ADDRESS; break; }
      } catch {}
    }
  }
  for (const property of ['IsEnabled', 'ScreenReaderEnabled']) {
    execFileSync('gdbus', ['call', '--session', '--dest', 'org.a11y.Bus', '--object-path', '/org/a11y/bus', '--method', 'org.freedesktop.DBus.Properties.Set', 'org.a11y.Status', property, '<true>'], { env: values, timeout: 5000 });
  }
  return values;
}

// Deployed servers inherit the runner's session bus, not the desktop's; the
// driver must find the desktop session and its accessibility bus itself.
const serverBus = (values) => {
  const runnerBus = `/run/user/${process.getuid()}/bus`;
  return existsSync(runnerBus) ? `unix:path=${runnerBus}` : values.DBUS_SESSION_BUS_ADDRESS;
};

async function session(t, values) {
  const client = new Client({ name: 'cua-interaction', version: '1' });
  await client.connect(new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL('../dist/server.js', import.meta.url))],
    env: { PATH: process.env.PATH, DISPLAY: values.DISPLAY, XAUTHORITY: values.XAUTHORITY ?? '/root/.Xauthority', DBUS_SESSION_BUS_ADDRESS: serverBus(values), OPENCURIA_CUA_SESSION_ID: 'interaction', OPENCURIA_CUA_TURN_ID: 'interaction' },
    stderr: 'pipe',
  }));
  t.after(() => client.close());
  return async (code, { error = false } = {}) => {
    const result = await client.callTool({ name: 'js', arguments: { code, timeout_ms: 90000 } });
    assert.equal(!!result.isError, error, output(result));
    return output(result);
  };
}

function launch(t, command, args, values) {
  const child = spawn(command, args, { env: values, detached: true, stdio: 'ignore' });
  t.after(() => { try { process.kill(-child.pid, 'SIGTERM'); } catch {} });
  return child;
}

async function chromeWindow(t, values, page, windowClass) {
  const profile = await mkdtemp(`${tmpdir()}/opencuria-cua-chrome-`);
  t.after(() => rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  launch(t, chrome, ['--no-sandbox', '--no-first-run', '--no-default-browser-check', '--password-store=basic', '--disable-dev-shm-usage', `--user-data-dir=${profile}`, `--class=${windowClass}`, '--window-position=30,30', '--window-size=1100,820', pathToFileURL(fixture(page)).href], values);
}

test('real Chrome: queries, waits, setValue, contexts, modals and an event-maintained tree', { skip: !desktop || !existsSync(chrome), timeout: 180000 }, async (t) => {
  const values = await environment();
  const windowClass = `CuaForm${process.pid}`;
  await chromeWindow(t, values, 'web/form.html', windowClass);
  const js = await session(t, values);
  await js(`var app; for (let i = 0; i < 100 && !app; i++) { try { app = await cua.getApp({ app: ${JSON.stringify(windowClass)} }, { emit: false }); } catch { await new Promise(r => setTimeout(r, 200)); } } await app.waitFor({ role: 'button', name: 'Load data' }, { timeoutMs: 20000 });`);
  let text = await js(`await app.click({ role: 'button', name: 'Load data' }); await app.waitFor('Processing'); await app.waitFor({ text: 'Processing' }, { gone: true }); await app.find('Loaded item 4');`);
  assert.match(text, /Loaded item 4/);
  text = await js(`await app.setValue({ name: 'Promo text' }, 'x'.repeat(167)); await app.find('remaining');`);
  assert.match(text, /3 remaining/);
  text = await js(`nodeRepl.write(JSON.stringify(await app.setValue({ name: 'Version' }, '1.0.0')));`);
  assert.equal(text, '{"value":"1.0.0"}');
  text = await js(`await app.find({ role: 'radio' });`);
  assert.match(text, /radio button \(unchecked\) Context: Cartoon violence › Frequent/);
  await js(`await app.setValue({ role: 'radio', context: 'Cartoon violence › Frequent' }, true); await app.setValue({ role: 'radio', context: 'Horror themes › None' }, true); await app.setValue({ role: 'checkbox', context: 'Content rights' }, true);`);
  text = await js(`await app.click({ role: 'button', name: 'Save' }); await app.waitFor(/Saved 1\\.0\\.0/);`);
  assert.match(text, /Saved 1\.0\.0 \/ 167 \/ 0110/);
  await js(`await app.getAXState({ root: { role: 'document web' } });`); // the baseline for the change list
  text = await js(`await app.click({ role: 'button', name: 'Open rights' }); await app.waitFor({ role: 'dialog', name: 'Content Rights' }); await app.getAXState({ root: { role: 'document web' } });`);
  assert.match(text, /\+ \d+ dialog Content Rights/);
  assert.match(text, /\+ \d+ radio button \(unchecked\) No/);
  assert.ok(text.length < 4000, 'the change list is much smaller than the page');
  text = await js(`await app.setValue({ role: 'radio', name: 'No', within: { role: 'dialog' } }, true); await app.click({ role: 'button', name: 'Done' }); await app.waitFor('rights done');`);
  assert.match(text, /rights done/);
  text = await js(`await app.focus({ role: 'textbox', name: 'Version' }); await app.pressKey('ctrl+a'); await app.typeCharacters('2.5-ß€'); await app.selectText({ role: 'textbox', name: 'Version' }, '5-ß'); await app.typeText('X'); await app.find({ role: 'textbox', name: 'Version' });`);
  assert.match(text, /Value: 2\.X€/);
  text = await js(`const doc = { role: 'document web' }; const a = await app.getAXState({ root: doc, emit: false }); const b = await app.getAXState({ root: doc, emit: false, disableDiffing: true }); nodeRepl.write(a === b ? 'consistent' : 'differs');`);
  assert.equal(text, 'consistent');
  const image = await js(`var shot = await app.getScreenshot({ region: { role: 'table' }, maxDimension: 400 }); nodeRepl.write(String(shot.length > 100));`);
  assert.match(image, /true$/);
});

test('real Chrome: native file dialogs and file drops', { skip: !desktop || !existsSync(chrome), timeout: 180000 }, async (t) => {
  const values = await environment();
  const folder = await mkdtemp(`${tmpdir()}/opencuria-cua-files-`);
  t.after(() => rm(folder, { recursive: true, force: true }));
  await mkdir(`${folder}/a`); await mkdir(`${folder}/b`);
  await writeFile(`${folder}/a/shot1.png`, 'PNG1'); await writeFile(`${folder}/a/shot2.png`, 'PNG22');
  await writeFile(`${folder}/b/Datei mit Leerzeichen.txt`, 'Ünïcödé'); await writeFile(`${folder}/b/notes.txt`, 'hello!');
  const windowClass = `CuaUpload${process.pid}`;
  await chromeWindow(t, values, 'web/upload.html', windowClass);
  const js = await session(t, values);
  await js(`var app; for (let i = 0; i < 100 && !app; i++) { try { app = await cua.getApp({ app: ${JSON.stringify(windowClass)} }, { emit: false }); } catch { await new Promise(r => setTimeout(r, 200)); } } await app.waitFor({ role: 'button', name: 'Single' }, { timeoutMs: 20000 });`);
  let text = await js(`await app.chooseFiles(${JSON.stringify(`${folder}/b/notes.txt`)}, { trigger: { role: 'button', name: 'Single' } }); await app.waitFor('single =>');`);
  assert.match(text, /single => notes\.txt:6/);
  text = await js(`await app.chooseFiles(${JSON.stringify([`${folder}/a/shot1.png`, `${folder}/a/shot2.png`, `${folder}/b/Datei mit Leerzeichen.txt`])}, { trigger: { role: 'button', name: 'Screenshots' } }); await app.waitFor('multi =>');`);
  assert.match(text, /multi => .*Datei mit Leerzeichen\.txt:7/);
  assert.match(text, /shot1\.png:4/);
  assert.match(text, /shot2\.png:5/);
  text = await js(`await app.dropFiles(${JSON.stringify([`${folder}/a/shot1.png`, `${folder}/b/notes.txt`])}, { name: 'Drop zone' }); await app.waitFor('drop =>');`);
  assert.match(text, /drop => shot1\.png:4, notes\.txt:6/);
});

for (const toolkit of ['GTK', 'Qt']) {
  test(`real ${toolkit}: a whole form by queries, layout contexts and its file dialog`, { skip: !desktop, timeout: 120000 }, async (t) => {
    if (toolkit === 'Qt') {
      try { execFileSync('/usr/bin/python3', ['-c', 'import PyQt5'], { stdio: 'ignore' }); }
      catch { t.skip('Install python3-pyqt5'); return; }
    }
    const values = await environment();
    const folder = await mkdtemp(`${tmpdir()}/opencuria-cua-${toolkit}-`);
    t.after(() => rm(folder, { recursive: true, force: true }));
    await writeFile(`${folder}/one.txt`, '1'); await mkdir(`${folder}/other`); await writeFile(`${folder}/other/two.txt`, '2');
    const title = `OpenCuria ${toolkit} form ${process.pid}`;
    const result = `${folder}/result.json`;
    launch(t, '/usr/bin/python3', [fixture(toolkit === 'GTK' ? 'gtk_form.py' : 'qt_form.py')], { ...values, CUA_TEST_TITLE: title, CUA_TEST_RESULT: result });
    const js = await session(t, values);
    await js(`var app; for (let i = 0; i < 100 && !app; i++) { try { app = await cua.getApp(${JSON.stringify(title)}, { emit: false }); } catch { await new Promise(r => setTimeout(r, 200)); } } await app.waitFor({ role: 'button', name: 'Save form' });`);
    const radios = await js(`await app.find({ role: 'radio' });`);
    assert.match(radios, /Context: Contains ads › Yes/);
    const text = await js(`nodeRepl.write(JSON.stringify([
      await app.setValue({ name: 'App name' }, 'Meine App äöü'),
      await app.setValue({ role: 'combobox' }, 'Deutsch'),
      await app.setValue({ name: 'I agree' }, true),
      await app.setValue({ name: 'Amount' }, 42),
      await app.setValue({ role: 'radio', context: 'Contains ads › Yes' }, true),
      await app.setValue({ name: 'Notes' }, 'Zeile 1\\nZeile 2 ✅'),
    ]));`);
    assert.equal(text, '[{"value":"Meine App äöü"},{"value":"Deutsch"},{"checked":true},{"value":42},{"checked":true},{"value":"Zeile 1\\nZeile 2 ✅"}]');
    await js(`await app.click({ name: 'Load data' }); await app.waitFor('Loaded 3 items', { timeoutMs: 8000 });`);
    if (toolkit === 'GTK') await js(`await app.chooseFiles(${JSON.stringify(`${folder}/one.txt`)}, { trigger: { role: 'push button', name: '(None)' } });`);
    else await js(`await app.chooseFiles(${JSON.stringify([`${folder}/one.txt`, `${folder}/other/two.txt`])}, { trigger: { name: 'Choose files' } }); await app.waitFor('Chose 2');`);
    await js(`await app.click({ name: 'Save form' }); await app.waitFor({ name: 'Saved' });`);
    const saved = JSON.parse(await readFile(result, 'utf8'));
    assert.deepEqual({ ...saved, document: undefined, files: undefined }, { name: 'Meine App äöü', language: 'Deutsch', agree: true, amount: 42, ads: 'yes', notes: 'Zeile 1\nZeile 2 ✅', document: undefined, files: undefined });
    if (toolkit === 'GTK') assert.equal(saved.document, `${folder}/one.txt`);
    else assert.deepEqual([...saved.files].sort(), ['one.txt', 'two.txt']); // the dialog decides the order
  });
}
