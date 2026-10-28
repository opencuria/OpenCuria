import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const referenceRoot = process.env.CUA_REFERENCE_ROOT ?? '/usr/lib/chatgpt/resources/cua_node';
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';

async function connect(t, reference) {
  const client = new Client({ name: 'cua-compatibility', version: '1' });
  const transport = reference ? {
    command: `${referenceRoot}/bin/node`,
    args: [`${referenceRoot}/lib/node_modules/@oai/cua-repl/bin/cua-repl.mjs`],
    env: {
      PATH: process.env.PATH, NODE_REPL_NODE_PATH: `${referenceRoot}/bin/node`,
      CUA_REPL_NODE_REPL_PATH: `${referenceRoot}/bin/node_repl`,
      CUA_REPL_ENABLED_SURFACES: 'computer', NODE_REPL_JS_BANNER: '',
      NODE_REPL_DISABLE_ANALYTICS: '1', NODE_REPL_TRUSTED_SERVICES: '{}',
    },
  } : {
    command: process.execPath,
    args: [fileURLToPath(new URL('../dist/server.js', import.meta.url))],
    env: { PATH: process.env.PATH, OPENCURIA_CUA_SESSION_ID: 'parity', OPENCURIA_CUA_TURN_ID: 'parity' },
  };
  await client.connect(new StdioClientTransport({ ...transport, stderr: 'pipe' }));
  t.after(() => client.close());
  return client;
}

test('original ChatGPT contract: schemas, text channels, images, errors, lexical state and reset', {
  skip: !existsSync(`${referenceRoot}/bin/node_repl`),
}, async t => {
  const original = await connect(t, true);
  const actual = await connect(t, false);
  const names = new Set(['js', 'js_reset', 'turn_ended']);
  assert.deepEqual((await actual.listTools()).tools, (await original.listTools()).tools.filter(tool => names.has(tool.name)));
  const directory = await mkdtemp('/tmp/opencuria-cua-parity-');
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = `${directory}/image.png`;
  await writeFile(path, Buffer.from(png, 'base64'));
  const inputs = [
    '', '1 + 1', 'let retained = 40;', 'retained += 2; nodeRepl.write(retained);',
    'nodeRepl.write({ b: 2, a: [1, true, null] }); nodeRepl.write(undefined); nodeRepl.write(null);',
    'nodeRepl.write("doc", "cua.core"); nodeRepl.write("state", "cua.state");',
    'nodeRepl.write("a"); nodeRepl.write("b");',
    'console.log("a", 2); console.warn("b"); console.error("c");',
    'nodeRepl.write("a", "first"); nodeRepl.write("b", "second"); nodeRepl.write("c", "first");',
    `nodeRepl.write('before'); await nodeRepl.emitImage(Buffer.from('${png}', 'base64')); nodeRepl.write('after');`,
    `await nodeRepl.emitImage({bytes:Buffer.from('${png}','base64'),mimeType:'image/png'});`,
    `await nodeRepl.emitImage('data:image/png;base64,${png}');`,
    `await nodeRepl.emitImage(${JSON.stringify(pathToFileURL(path).href)});`,
    'throw new Error("example");', 'var =',
    'nodeRepl.write(retained);',
  ];
  for (const code of inputs) {
    const call = client => client.callTool({ name: 'js', arguments: { code } });
    const [expected, result] = await Promise.all([call(original), call(actual)]);
    assert.deepEqual({ content: result.content, isError: result.isError }, { content: expected.content, isError: expected.isError }, code);
  }
  assert.deepEqual(await actual.callTool({ name: 'js_reset', arguments: {} }), await original.callTool({ name: 'js_reset', arguments: {} }));
});
