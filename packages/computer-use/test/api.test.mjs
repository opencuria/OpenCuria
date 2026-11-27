import { test } from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const source = await build({ entryPoints: [fileURLToPath(new URL('../src/api.ts', import.meta.url))], bundle: true, write: false, format: 'esm', platform: 'node' });
const { createCua } = await import(`data:text/javascript;base64,${Buffer.from(source.outputFiles[0].text).toString('base64')}`);

const ROLES = ['push button', 'menu item', 'combo box', 'radio button', 'check box', 'entry', 'frame', 'dialog', 'text'];
const node = (id, line, extra = {}) => {
  const role = extra.role ?? ROLES.find((candidate) => line.startsWith(`${candidate} `)) ?? line.split(' ')[0];
  const name = extra.name ?? line.slice(role.length).replace(/^ \([^)]*\)/, '').replace(/,.*$/, '').trim();
  return { id, parent: extra.parent ?? null, depth: extra.depth ?? 1, role, name, value: extra.value ?? '', description: '', context: extra.context ?? '', states: extra.states ?? ['enabled'], actions: extra.actions ?? [], line };
};

function fake({ windows = [{ id: 5, app: 'Chrome', title: 'Form - Chrome' }], apps = [], nodes } = {}) {
  const calls = [];
  const output = [];
  const state = {
    windows,
    nodes: nodes ?? [
      node(1, 'frame Form', { depth: 0, name: 'Form' }),
      node(2, 'text Keywords', { parent: 1, role: 'static', name: 'Keywords' }),
      node(3, 'entry (settable) Keywords', { parent: 1, states: ['enabled', 'editable', 'settable', 'focusable'] }),
      node(4, 'push button Save', { parent: 1, actions: ['press'] }),
      node(5, 'push button Save draft', { parent: 1, actions: ['press'] }),
    ],
    reads: 0,
    failures: [],
  };
  const native = async (op, args = {}) => {
    calls.push([op, args]);
    if (state.failures.length && state.failures[0][0] === op) throw new Error(state.failures.shift()[1]);
    if (op === 'windows') return { items: state.windows };
    if (op === 'apps') return { items: apps };
    if (op === 'launch_app') { state.windows = [...state.windows, { id: 9, app: 'Mousepad', title: 'Untitled - Mousepad' }]; return {}; }
    if (op === 'bind') return { identity: `bound-${args.windowId}` };
    if (op === 'state') {
      state.reads++;
      if (state.onRead) state.onRead(state);
      const result = { source: 'at_spi', nodes: state.nodes };
      if (args.image) Object.assign(result, { image: Buffer.from([1, 2]).toString('base64'), width: 2, height: 1, scale: 1, region: null });
      return result;
    }
    if (op === 'set_value') return { value: args.value };
    if (op === 'choose_files') return { files: [].concat(args.paths), dialog: 'Open Files' };
    if (op === 'drop_files') return { files: [].concat(args.paths) };
    if (op === 'bounds') return { x: 1, y: 2, width: 3, height: 4 };
    return {};
  };
  const cua = createCua(native, (value) => output.push(String(value)), async () => { output.push('[image]'); });
  return { cua, calls, output, state, printed: () => output.filter((text) => !text.startsWith('## Computer Use')).join('') };
}

test('getApp finds windows by name, explains ambiguity and launches installed apps', async () => {
  const one = fake();
  const app = await one.cua.getApp('chrome', { emit: false });
  assert.equal(app.windowId, 5);
  assert.equal(one.printed(), '');
  const two = fake({ windows: [{ id: 5, app: 'Chrome', title: 'A' }, { id: 6, app: 'Chrome', title: 'B' }] });
  await assert.rejects(two.cua.getApp('Chrome'), /Several windows match "Chrome"[\s\S]*5 Chrome “A”[\s\S]*6 Chrome “B”/);
  assert.equal((await two.cua.getApp({ title: 'B' }, { emit: false })).windowId, 6);
  const launch = fake({ windows: [], apps: [{ id: 'org.xfce.mousepad.desktop', displayName: 'Mousepad' }] });
  const editor = await launch.cua.getApp('Mousepad', { emit: false });
  assert.equal(editor.windowId, 9);
  assert.ok(launch.calls.some(([op, args]) => op === 'launch_app' && args.app === 'org.xfce.mousepad.desktop'));
  await assert.rejects(fake({ windows: [] }).cua.getApp('Nothing'), /No open window matches "Nothing"/);
  const own = fake({ windows: [{ id: 7, app: 'Xfce4-terminal', title: 'Terminal - root' }], apps: [{ id: 'xfce4-terminal.desktop', displayName: 'Xfce Terminal', windows: [{ id: 7, app: 'xfce4-terminal.desktop', title: 'Terminal - root' }] }] });
  assert.equal((await own.cua.getApp('Xfce Terminal', { emit: false })).windowId, 7);
  assert.ok(!own.calls.some(([op]) => op === 'launch_app'));
  const several = fake({ windows: [], apps: [{ id: 'a.desktop', displayName: 'Xfce Terminal' }, { id: 'b.desktop', displayName: 'Terminal Emulator' }] });
  await assert.rejects(several.cua.getApp('Terminal'), /Several installed apps match "Terminal"[\s\S]*Xfce Terminal \(a\.desktop\)/);
});

test('queries resolve to operable controls, prefer exact names and report ambiguity', async () => {
  const { cua, calls } = fake();
  const app = await cua.getApp('Chrome', { emit: false });
  await app.setValue({ name: 'Keywords' }, 'äöü');
  assert.deepEqual(calls.at(-1), ['set_value', { windowId: 5, identity: 'bound-5', elementId: 3, value: 'äöü' }]);
  await app.click({ role: 'button', name: 'Save' });
  assert.equal(calls.at(-1)[1].elementId, 4);
  await app.click({ role: 'button', name: 'save', index: 1 });
  assert.equal(calls.at(-1)[1].elementId, 5);
  await app.click('Save draft');
  assert.equal(calls.at(-1)[1].elementId, 5);
  await assert.rejects(app.click({ role: 'button' }), /2 elements match \{role:"button"\}[\s\S]*4 push button Save/);
  await assert.rejects(app.click({ name: 'Missing' }, { timeoutMs: 0 }), /No element matches \{name:"Missing"\} after 0 ms/);
  await app.click([10, 20], { key: 'shift' });
  assert.deepEqual(calls.at(-1)[1].coordinates, [10, 20]);
});

test('a stale query target is resolved again once', async () => {
  const { cua, calls, state } = fake();
  const app = await cua.getApp('Chrome', { emit: false });
  state.failures.push(['click', 'Element 4 (push button \'Save\') no longer exists; find it again']);
  await app.click({ role: 'button', name: 'Save' });
  assert.equal(calls.filter(([op]) => op === 'click').length, 2);
});

test('getAXState prints changes after the first full tree, and everything on request', async () => {
  const filler = Array.from({ length: 40 }, (_, index) => node(100 + index, `text Paragraph ${index}`, { parent: 1, role: 'static' }));
  const { cua, output, state } = fake();
  state.nodes = [...state.nodes, ...filler];
  const app = await cua.getApp('Chrome');
  assert.match(output.at(-1), /^Accessibility source: at_spi\n1 frame Form\n\t2 text Keywords/);
  state.nodes = [
    state.nodes[0], state.nodes[1],
    node(3, 'entry (settable) Keywords, Value: neu', { parent: 1, value: 'neu', states: ['editable'] }),
    state.nodes[3],
    node(6, 'dialog Content Rights', { parent: 1 }),
    node(7, 'push button Done', { parent: 6, depth: 2 }),
    ...filler,
  ];
  const full = await app.getAXState();
  assert.match(full, /^Accessibility source: at_spi\n1 frame Form/);
  const printed = output.at(-1);
  assert.match(printed, /changes since the previous state/);
  assert.match(printed, /~ 3 entry \(settable\) Keywords, Value: neu/);
  assert.match(printed, /\+ 6 dialog Content Rights \(in 1\)\n\t\+ 7 push button Done/);
  assert.match(printed, /- 5 push button Save draft/);
  await app.getAXState();
  assert.match(output.at(-1), /No changes since the previous state/);
  await app.getAXState({ disableDiffing: true });
  assert.match(output.at(-1), /^Accessibility source: at_spi\n1 frame Form/);
});

test('large trees are cut at a line with a notice; return values stay complete', async () => {
  const nodes = [node(1, 'frame Big', { depth: 0 })];
  for (let id = 2; id < 3000; id++) nodes.push(node(id, `text Row ${id} ${'x'.repeat(20)}`, { parent: 1, role: 'static' }));
  const { cua, output } = fake({ nodes });
  const app = await cua.getApp('Chrome', { emit: false });
  const full = await app.getAXState();
  assert.equal(full.split('\n').length, 3000);
  assert.ok(Buffer.byteLength(output.at(-1)) <= 48 * 1024 + 1);
  assert.match(output.at(-1), /\[Showing \d+ of 2999 lines \(output limit\)/);
});

test('find prints only matches; waitFor waits for appearance and disappearance', async () => {
  const { cua, output, state } = fake();
  const app = await cua.getApp('Chrome', { emit: false });
  const found = await app.find({ role: 'button' });
  assert.deepEqual(found.map((item) => item.id), [4, 5]);
  assert.equal(output.at(-1), '4 push button Save\n5 push button Save draft');
  assert.deepEqual(await app.find('nothing'), []);
  assert.match(output.at(-1), /No element matches "nothing"/);
  state.onRead = (current) => { if (current.reads === 4) current.nodes = [...current.nodes, node(8, 'text Saved', { parent: 1, role: 'static' })]; };
  const saved = await app.waitFor('Saved', { timeoutMs: 3000 });
  assert.equal(saved.id, 8);
  state.onRead = (current) => { current.nodes = current.nodes.filter((item) => item.id !== 8); };
  assert.equal(await app.waitFor('Saved', { gone: true, timeoutMs: 3000 }), undefined);
  assert.match(output.at(-1), /Gone: "Saved"/);
  state.onRead = undefined;
  await assert.rejects(app.waitFor('Never', { timeoutMs: 300 }), /waitFor timed out after 300 ms/);
});

test('waitFor re-reads the whole tree about once per second for toolkits without events', async () => {
  const { cua, calls } = fake();
  const app = await cua.getApp('Chrome', { emit: false });
  await assert.rejects(app.waitFor('Never', { timeoutMs: 1300 }));
  assert.ok(calls.some(([op, args]) => op === 'state' && args.fresh === true));
});

test('files, screenshots and bounds pass their targets to the driver', async () => {
  const { cua, calls, output } = fake();
  const app = await cua.getApp('Chrome', { emit: false });
  assert.deepEqual(await app.chooseFiles(['/a.png', '/b.png'], { trigger: { role: 'button', name: 'Save' } }), { files: ['/a.png', '/b.png'], dialog: 'Open Files' });
  assert.equal(calls.at(-1)[1].triggerElementId, 4);
  assert.match(output.at(-1), /Chose 2 file\(s\) in “Open Files”/);
  await app.dropFiles('/a.png', { name: 'Keywords', role: 'entry' });
  assert.equal(calls.at(-1)[1].elementId, 3);
  await app.getScreenshot({ region: { role: 'button', name: 'Save draft' }, maxDimension: 800 });
  assert.equal(calls.at(-1)[1].region, 5);
  assert.equal(calls.at(-1)[1].maxDimension, 800);
  assert.deepEqual(await app.getBounds('Save draft'), { x: 1, y: 2, width: 3, height: 4 });
});
