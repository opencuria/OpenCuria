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
  assert.equal(result.tools[0].annotations.readOnlyHint, false);
  assert.equal(result.tools[2]._meta['opencuria/internal'], true);
});

test('retains lexical state and top-level await, then resets bindings', async t => {
  const client = await runtime(t);
  const a = await client.callTool({ name: 'js', arguments: { code: 'var counter = await Promise.resolve(40); nodeRepl.write(counter);' } });
  assert.equal(a.isError, undefined);
  assert.equal(text(a), '40');
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'counter += 2; nodeRepl.write(counter);' } })), '42');
  await client.callTool({ name: 'js_reset', arguments: {} });
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(typeof counter);' } })), 'undefined');
});

test('syntax errors preserve bindings; infinite code is terminated and reset', async t => {
  const client = await runtime(t);
  await client.callTool({ name: 'js', arguments: { code: 'var retained = 42;' } });
  const syntax = await client.callTool({ name: 'js', arguments: { code: 'var =' } });
  assert.equal(syntax.isError, true);
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(retained)' } })), '42');
  const loop = await client.callTool({ name: 'js', arguments: { code: 'while (true) {}', timeout_ms: 100 } });
  assert.equal(loop.isError, true);
  assert.equal(text(await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(typeof retained)' } })), 'undefined');
});

test('lifecycle fences a turn and cannot be redirected to another turn', async t => {
  const client = await runtime(t);
  const bad = await client.callTool({ name: 'turn_ended', arguments: { session_id: 'other', turn_id: 'test-turn', hook_event_name: 'Stop' } });
  assert.equal(bad.isError, true);
  const good = await client.callTool({ name: 'turn_ended', arguments: { session_id: 'test-session', turn_id: 'test-turn', hook_event_name: 'Stop' } });
  assert.equal(good.isError, undefined);
  const late = await client.callTool({ name: 'js', arguments: { code: 'nodeRepl.write(42)' } });
  assert.equal(late.isError, true);
  assert.match(text(late), /TURN_ENDED/);
});

test('shipped artifact manifest matches the current source build', async () => {
  const { readFile } = await import('node:fs/promises');
  const shipped = JSON.parse(await readFile(new URL('../../../runner/src/runtime/assets/computer-use-0.1.0.json', import.meta.url), 'utf8'));
  const built = JSON.parse(await readFile(new URL('../dist/manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(shipped.manifest, built, 'Rebuild and pack the Computer Use artifact after source changes.');
});
