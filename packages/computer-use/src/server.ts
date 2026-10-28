import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { Supervisor } from './supervisor.js';
import { CuaError, TOOLS, VERSION, errorResult, type RuntimeContext } from './protocol.js';

const supervisor = new Supervisor();
const server = new Server({ name: 'opencuria-computer-use', version: VERSION }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
  const args = request.params.arguments ?? {};
  const context = (request.params._meta?.['opencuria/context'] ?? {
    session_id: process.env.OPENCURIA_CUA_SESSION_ID ?? '',
    turn_id: process.env.OPENCURIA_CUA_TURN_ID ?? '',
  }) as RuntimeContext;
  context.request_meta = request.params._meta;
  try {
    supervisor.bindContext(context);
    if (request.params.name === 'js') return await supervisor.execute(args, context, extra.signal);
    if (request.params.name === 'js_reset') {
      await supervisor.reset();
      return { content: [{ type: 'text', text: 'js kernel reset' }], isError: false };
    }
    if (request.params.name === 'turn_ended') {
      if (args.session_id !== context.session_id || args.turn_id !== context.turn_id) throw new CuaError('INVALID_CONTEXT', 'Lifecycle identity does not match its owning turn');
      await supervisor.end();
      return { content: [{ type: 'text', text: '' }], isError: false };
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
