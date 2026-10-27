import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { Supervisor } from './supervisor.js';
import { CuaError, VERSION, errorResult, type RuntimeContext } from './protocol.js';

const supervisor = new Supervisor();
const server = new Server({ name: 'opencuria-computer-use', version: VERSION }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [
  {
    name: 'js',
    description: 'Operate the workspace Linux desktop with persistent JavaScript. Start with await cua.getState(), await cua.listWindows(), or var app = await cua.getApp({windowId: an observed ID}). Read the returned documentation before continuing. Prefer a purpose-built plugin when available. Await all operations and verify actions using fresh AX state or screenshots.',
    inputSchema: { type: 'object', additionalProperties: false, properties: {
      code: { type: 'string', minLength: 1, description: 'JavaScript to execute in this run’s persistent CUA runtime.' },
      timeout_ms: { type: 'integer', minimum: 1, maximum: 60_000 },
      title: { type: 'string', minLength: 1, maxLength: 80, description: 'Short description of the intended UI action.' },
    }, required: ['code'] },
    annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
  },
  {
    name: 'js_reset', description: 'Reset persistent JavaScript bindings. Native applications remain open. The next js call must read the CUA entry-point documentation again.',
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  },
  {
    name: 'turn_ended', description: 'Internal lifecycle notification. End this turn, stop pending input and close runtime processes. Repeated calls are harmless.',
    inputSchema: { type: 'object', additionalProperties: false, properties: { session_id: { type: 'string' }, turn_id: { type: 'string' }, hook_event_name: { type: 'string' } }, required: ['session_id', 'turn_id', 'hook_event_name'] },
    _meta: { 'opencuria/internal': true },
  },
] }));
server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
  const args = request.params.arguments ?? {};
  const context = (request.params._meta?.['opencuria/context'] ?? {
    session_id: process.env.OPENCURIA_CUA_SESSION_ID ?? '',
    turn_id: process.env.OPENCURIA_CUA_TURN_ID ?? '',
  }) as RuntimeContext;
  try {
    supervisor.bindContext(context);
    if (request.params.name === 'js') return await supervisor.execute(args, context, extra.signal);
    if (request.params.name === 'js_reset') {
      await supervisor.reset();
      return { content: [{ type: 'text', text: 'JavaScript state reset. Applications remain open.' }] };
    }
    if (request.params.name === 'turn_ended') {
      if (args.session_id !== context.session_id || args.turn_id !== context.turn_id) throw new CuaError('INVALID_CONTEXT', 'Lifecycle identity does not match its owning turn');
      await supervisor.end();
      return { content: [{ type: 'text', text: 'Turn ended. Runtime processes stopped.' }] };
    }
    throw new CuaError('UNKNOWN_TOOL', 'Unknown computer-use tool');
  } catch (error) { return errorResult(error); }
});

let closing = false;
async function close(): Promise<void> {
  if (closing) return;
  closing = true;
  await supervisor.end();
  await server.close();
}
for (const signal of ['SIGTERM', 'SIGINT'] as const) process.on(signal, () => { void close().finally(() => process.exit(0)); });
process.stdin.on('end', () => { void close(); });
await server.connect(new StdioServerTransport());
