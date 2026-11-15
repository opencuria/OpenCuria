import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { build } from 'esbuild';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = process.env.CUA_REFERENCE_ROOT ?? '/usr/lib/chatgpt/resources/cua_node';
const bindingPath = `${root}/lib/node_modules/@oai/cua/dist/lib/js/oai_js_cua/src/tinysky_alt/bind_linux_app.js`;
const normalize = value => value instanceof Uint8Array ? [...value] : value && typeof value === 'object'
  ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalize(item)])) : value;

test('same native app inputs produce the reference observations, return values and validation errors', {
  skip: !existsSync(bindingPath),
}, async t => {
  const source = await build({
    entryPoints: [fileURLToPath(new URL('../src/api.ts', import.meta.url))],
    bundle: true, write: false, format: 'esm', platform: 'node',
  });
  const { createCua } = await import(`data:text/javascript;base64,${Buffer.from(source.outputFiles[0].text).toString('base64')}`);
  const { bind_linux_app } = await import(pathToFileURL(bindingPath).href);
  const bytes = Uint8Array.from([1, 2, 3]);
  const text = '0 frame Example';
  let emitted = [];
  const write = (value, name) => { emitted.push({ value, name }); };
  const emitImage = async value => { emitted.push({ image: [...value] }); };
  const previous = globalThis.nodeRepl;
  globalThis.nodeRepl = { write, emitImage };
  t.after(() => { globalThis.nodeRepl = previous; });
  const window = { id: 42 };
  const sky = {
    get_window_state: async () => ({ window, ax_tree_source: 'at_spi', ax_tree: { to_string: () => text }, screenshots: [{ bytes }] }),
    type_characters: async () => 2,
  };
  for (const method of ['click', 'drag', 'scroll', 'type_text', 'press_key', 'perform_secondary_action']) sky[method] = async () => undefined;
  const reference = bind_linux_app(sky, window);
  const native = async (op, args = {}) => {
    if (op === 'windows') return { items: [window] };
    if (op === 'bind') return { identity: 'window' };
    if (op === 'state') return { state: `Accessibility source: at_spi\n${text}`, snapshotId: 'snapshot', ...(args.image ? { image: Buffer.from(bytes).toString('base64') } : {}) };
    if (op === 'type_characters') return { charactersSent: 2 };
    return {};
  };
  const actual = await createCua(native, write, emitImage).getApp({ windowId: 42 });
  for (const [method, args] of [
    ['getAXState', []], ['getAXState', [{ emit: false }]],
    ['getScreenshot', []], ['getScreenshot', [{ emit: false }]],
    ['getAXStateAndScreenshot', []], ['getAXStateAndScreenshot', [{ emit: false }]],
    ['click', [[20, 30], { mouseButton: 'right', durationMs: 20, key: 'shift' }]],
    ['click', [3, { durationMs: 10 }]], ['click', [[1, 2], { durationMs: -1 }]],
    ['click', [3, { key: 'ctrl' }]],
    ['scroll', [[10, 20], 'down', { pixels: 120 }]], ['scroll', [3, 'down', 1]],
    ['scroll', [[10, 20], 'down', { pixels: 0 }]],
    ['pressKey', ['ctrl+a', { durationMs: 10 }]], ['pressKey', ['a', { durationMs: -1 }]],
    ['paste', ['hello']], ['paste', ['hello', { format: 'html' }]],
    ['typeText', ['hello', 2]], ['typeCharacters', ['20']],
    ['setValue', [2, 'value']], ['selectText', [2, 'value']],
    ['performSecondaryAction', [2, 'activate']], ['drag', [[10, 20], [30, 40]]],
  ]) {
    async function observe(app) {
      emitted = [];
      try { return { value: normalize(await app[method](...args)), emitted }; }
      catch (error) { return { error: error.message, emitted }; }
    }
    const expected = await observe(reference);
    const result = await observe(actual);
    assert.deepEqual(result, expected, `${method}(${JSON.stringify(args)})`);
  }
});
