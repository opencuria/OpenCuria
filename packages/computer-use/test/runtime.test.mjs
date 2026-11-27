import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { fileURLToPath } from 'node:url';

async function runtime(t) {
  const client = new Client({ name: 'computer-use-contract-test', version: '1' });
  const transport = new StdioClientTransport({ command: process.execPath, args: [fileURLToPath(new URL('../dist/server.js', import.meta.url))], env: { PATH: process.env.PATH, OPENCURIA_CUA_SESSION_ID: 'test-session', OPENCURIA_CUA_TURN_ID: 'test-turn' }, stderr: 'pipe' });
  await client.connect(transport);
  t.after(() => client.close());
  return client;
}

const text = result => result.content.filter(item => item.type === 'text').map(item => item.text).join('\n');

test('exposes bounded JS/reset and internal lifecycle schemas', async t => {
  const client = await runtime(t);
  const result = await client.listTools();
  assert.deepEqual(result.tools.map(tool => tool.name), ['js', 'js_reset', 'turn_ended']);
  assert.equal(result.tools[0].annotations, undefined);
  assert.deepEqual(result.tools[2]._meta.ui.visibility, []);
});

test('retains lexical state and top-level await, then resets bindings', async t => {
  const client = await runtime(t);
  const a = await client.callTool({ name: 'js', arguments: { code: 'var counter = await Promise.resolve(40); nodeRepl.write(counter);' } });
  assert.equal(a.isError, false);
  assert.equal(text(a), '40');
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'counter += 2; nodeRepl.write(counter);' } })), '42');
  await client.callTool({ name: 'js_reset', arguments: {} });
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(typeof counter);' } })), 'undefined');
});

test('reference text channels and console formatting remain stable without the reference installation', async t => {
  const client = await runtime(t);
  const result = await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write("a", "first"); nodeRepl.write("b", "second"); nodeRepl.write("c", "first"); console.log("hello", 2); console.warn("last");' } });
  assert.equal(result.isError, false);
  assert.deepEqual(result.content, [
    { type: 'text', text: 'ac' }, { type: 'text', text: 'b' },
    { type: 'text', text: 'hello 2\nlast' },
  ]);
  assert.equal(typeof result._meta['codex/nodeReplExecutionDurationMs'], 'number');
  assert.equal(result.structuredContent, undefined);
});

test('reference image blocks keep their MIME, detail hint and text channel', async t => {
  const client = await runtime(t);
  const data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
  const result = await client.callTool({ name: 'js', arguments: { code: `nodeRepl.write('before'); await nodeRepl.emitImage('data:image/png;base64,${data}'); nodeRepl.write('after');` } });
  assert.deepEqual(result.content, [
    { type: 'text', text: 'beforeafter' },
    { type: 'image', mimeType: 'image/png', data, _meta: { 'codex/imageDetail': 'original' } },
  ]);
});

test('syntax errors preserve bindings and say that nothing ran; infinite code is terminated and reset', async t => {
  const client = await runtime(t);
  await client.callTool({ name: 'js', arguments: { code: 'var retained = 42;' } });
  const syntax = await client.callTool({ name: 'js', arguments: { code: 'const a = 1; const a = 2; var never = 1;' } });
  assert.equal(syntax.isError, true);
  assert.match(text(syntax), /nothing from this call ran/);
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(`${retained} ${typeof never}`)' } })), '42 undefined');
  const loop = await client.callTool({ name: 'js', arguments: { code: 'while (true) {}', timeout_ms: 100 } });
  assert.equal(loop.isError, true);
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(typeof retained)' } })), 'undefined');
});

const meta = (turn, extra = {}) => ({ 'opencuria/context': { session_id: 'test-session', turn_id: turn, ...extra } });
const run = (client, code, turn, extra) => client.callTool({ name: 'js', arguments: { code }, _meta: meta(turn, extra) });
const end = (client, turn, reason = 'Stop', session = 'test-session') => client.callTool({ name: 'turn_ended', arguments: { session_id: session, turn_id: turn, hook_event_name: reason }, _meta: meta(turn) });

test('a chat session keeps JS state across turns; Stop fences only the ended turn', async t => {
  const client = await runtime(t);
  assert.equal((await run(client, 'var kept = 41; function helper() { return kept + 1; }', 'turn-1')).isError, false);
  assert.equal((await end(client, 'turn-1', 'Stop', 'other-session')).isError, true);
  assert.equal((await end(client, 'turn-1')).isError, false);
  const late = await run(client, 'nodeRepl.write(1)', 'turn-1');
  assert.equal(late.isError, true);
  assert.match(text(late), /ended turn/);
  assert.equal(text(await run(client, 'nodeRepl.write(helper())', 'turn-2')), '42');
  assert.equal((await end(client, 'turn-2', 'Interrupt')).isError, false);
  assert.equal((await end(client, 'turn-2', 'DesktopStop')).isError, false);
  assert.equal(text(await run(client, 'nodeRepl.write(kept)', 'turn-2')), '41');
});

test('a restarted desktop keeps variables and says that windows must be bound again', async t => {
  const client = await runtime(t);
  await run(client, 'var survivor = "here";', 'turn-1', { generation: 1, epoch: 'a' });
  const after = await run(client, 'nodeRepl.write(survivor)', 'turn-2', { generation: 2, epoch: 'a' });
  assert.equal(after.isError, false);
  assert.match(text(after), /desktop restarted[\s\S]*here$/);
  assert.doesNotMatch(text(await run(client, 'nodeRepl.write(survivor)', 'turn-3', { generation: 2, epoch: 'a' })), /restarted/);
});

test('workspace files can be read from JavaScript, other paths cannot', async t => {
  const { mkdtemp, writeFile, rm } = await import('node:fs/promises');
  const folder = await mkdtemp('/tmp/opencuria-cua-read-');
  t.after(() => rm(folder, { recursive: true, force: true }));
  await writeFile(`${folder}/text.txt`, 'Grüße ✅\nZeile 2');
  const client = await runtime(t);
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: `nodeRepl.write(await nodeRepl.readFile(${JSON.stringify(`${folder}/text.txt`)}))` } })), 'Grüße ✅\nZeile 2');
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: `nodeRepl.write((await nodeRepl.readFile(${JSON.stringify(`${folder}/text.txt`)}, { encoding: null })).length)` } })), String(Buffer.byteLength('Grüße ✅\nZeile 2')));
  const denied = await client.callTool({ name: 'js', arguments: { code: 'await nodeRepl.readFile("/etc/hostname")' } });
  assert.equal(denied.isError, true);
  assert.match(text(denied), /limited to/);
});

test('shipped artifact manifest matches the current source build', async () => {
  const { readFile } = await import('node:fs/promises');
  const shipped = JSON.parse(await readFile(new URL('../../../runner/src/runtime/assets/computer-use-0.3.4.json', import.meta.url), 'utf8'));
  const built = JSON.parse(await readFile(new URL('../dist/manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(shipped.manifest, built, 'Rebuild and pack the Computer Use artifact after source changes.');
});
