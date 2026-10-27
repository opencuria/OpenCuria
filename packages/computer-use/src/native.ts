import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { CuaError, MAX_RESULT_BYTES, type NativeReply, type RuntimeContext } from './protocol.js';

/** One bounded JSON-lines connection to the workspace's Linux driver. */
export class NativeClient {
  private process?: ChildProcessWithoutNullStreams;
  private counter = 0;
  private tail = '';
  private closed = false;
  private pending = new Map<number, { resolve: (value: NativeReply) => void; reject: (error: Error) => void }>();

  constructor(private context: RuntimeContext) {}

  private start(): void {
    if (this.process) return;
    const environment: NodeJS.ProcessEnv = {
      PATH: '/usr/bin:/bin',
      HOME: process.env.HOME,
      DISPLAY: this.context.display ?? process.env.DISPLAY,
      XAUTHORITY: this.context.xauthority ?? process.env.XAUTHORITY,
      DBUS_SESSION_BUS_ADDRESS: this.context.dbus_session_bus_address ?? process.env.DBUS_SESSION_BUS_ADDRESS,
      OPENCURIA_CUA_GENERATION: String(this.context.generation ?? 0),
      OPENCURIA_CUA_MAX_DIMENSION: String(this.context.screenshot_max_dimension ?? 2400),
    };
    const child = spawn('/usr/bin/python3', [fileURLToPath(new URL('./linux.py', import.meta.url))], {
      env: environment, stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.process = child;
    child.stderr.resume(); // Native errors are returned as bounded protocol errors.
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      this.tail += chunk;
      if (Buffer.byteLength(this.tail) > MAX_RESULT_BYTES) {
        this.fail(new CuaError('OUTPUT_LIMIT', 'Native response exceeded its byte limit', 'unknown'));
        child.kill();
        return;
      }
      let end: number;
      while ((end = this.tail.indexOf('\n')) !== -1) {
        const line = this.tail.slice(0, end);
        this.tail = this.tail.slice(end + 1);
        try {
          const reply = JSON.parse(line);
          const entry = this.pending.get(reply.id);
          if (!entry) continue;
          this.pending.delete(reply.id);
          if (reply.ok) entry.resolve(reply.output);
          else entry.reject(new CuaError(reply.code ?? 'NATIVE_ERROR', String(reply.error).slice(0, 2000), reply.effect_status ?? 'unknown'));
        } catch {
          this.fail(new CuaError('NATIVE_PROTOCOL_ERROR', 'Invalid native response', 'unknown'));
          child.kill();
        }
      }
    });
    child.on('error', () => this.fail(new CuaError('DRIVER_UNAVAILABLE', 'Could not start the Linux desktop driver')));
    child.on('exit', () => { this.fail(new CuaError('DRIVER_CLOSED', 'Linux desktop driver disconnected', 'unknown')); this.process = undefined; });
  }

  private fail(error: Error): void {
    for (const entry of this.pending.values()) entry.reject(error);
    this.pending.clear();
  }

  async request(op: string, args: Record<string, unknown> = {}): Promise<NativeReply> {
    if (this.closed) throw new CuaError('DRIVER_CLOSED', 'Native driver has ended');
    this.start();
    const id = ++this.counter;
    const result = new Promise<NativeReply>((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.process!.stdin.write(JSON.stringify({ id, op, args }) + '\n');
    return result;
  }

  async close(): Promise<void> {
    this.closed = true;
    const child = this.process;
    if (!child) return;
    this.process = undefined;
    child.stdin.end();
    await new Promise<void>((resolve) => {
      const kill = setTimeout(() => child.kill('SIGKILL'), 2000);
      child.once('exit', () => { clearTimeout(kill); resolve(); });
      child.kill('SIGTERM');
    });
    this.fail(new CuaError('DRIVER_CLOSED', 'Linux desktop driver was closed', 'unknown'));
  }
}
