import { fork, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import type { CallToolResult, ContentBlock } from '@modelcontextprotocol/sdk/types.js';
import { NativeClient } from './native.js';
import { CuaError, DEFAULT_TIMEOUT_MS, MAX_CODE_BYTES, MAX_IMAGE_BYTES, MAX_RESULT_BYTES, MAX_TEXT_BYTES, MAX_TIMEOUT_MS, errorResult, type RuntimeContext, type WorkerEvent } from './protocol.js';

type Execution = {
  id: string;
  context: RuntimeContext;
  content: ContentBlock[];
  textBytes: number;
  imageBytes: number;
  imageCount: number;
  nativeBegin?: Promise<unknown>;
  operations: Set<Promise<void>>;
  finish: (error?: Error) => void;
};

/** Supervise one run's persistent worker without executing model code here. */
export class Supervisor {
  private worker?: ChildProcess;
  private native?: NativeClient;
  private active?: Execution;
  private ended = false;
  private finishing?: Promise<void>;
  private context?: RuntimeContext;
  private calls = 0;

  bindContext(context: RuntimeContext): void {
    if (!context.session_id || !context.turn_id) throw new CuaError('INVALID_CONTEXT', 'A backend session and turn identity are required');
    if (this.context && (this.context.session_id !== context.session_id || this.context.turn_id !== context.turn_id)) throw new CuaError('INVALID_CONTEXT', 'A runtime cannot change its owning turn');
    // Retain the first identity; execute also compares desktop generations.
    this.context ??= context;
  }

  private startWorker(): ChildProcess {
    if (this.worker) return this.worker;
    const worker = fork(fileURLToPath(new URL('./worker.js', import.meta.url)), [], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'], serialization: 'advanced',
      execArgv: ['--max-old-space-size=256'],
      env: { PATH: '/usr/bin:/bin', HOME: process.env.HOME },
    });
    this.worker = worker;
    for (const output of [worker.stdout, worker.stderr]) output?.on('data', (data: Buffer) => {
      if (this.active) this.content(this.active, { type: 'text', text: data.toString('utf8') });
    });
    worker.on('message', (event: WorkerEvent) => this.event(event));
    worker.on('error', () => this.active?.finish(new CuaError('WORKER_UNAVAILABLE', 'Could not start the JavaScript worker')));
    worker.on('exit', () => {
      if (this.worker !== worker) return;
      this.worker = undefined;
      this.active?.finish(new CuaError('WORKER_CLOSED', 'JavaScript state was reset', 'unknown'));
    });
    return worker;
  }

  private content(active: Execution, content: ContentBlock): void {
    if (this.active !== active) return;
    if (content.type === 'text') {
      const remaining = MAX_TEXT_BYTES - active.textBytes;
      const text = Buffer.from(content.text).subarray(0, Math.max(0, remaining)).toString('utf8');
      if (!text) return;
      active.textBytes += Buffer.byteLength(text);
      active.content.push({ type: 'text', text });
    } else if (content.type === 'image') {
      const size = Buffer.byteLength(content.data, 'base64');
      if (++active.imageCount > 2 || active.imageBytes + size > MAX_IMAGE_BYTES) {
        active.finish(new CuaError('OUTPUT_LIMIT', 'At most two images totalling 4 MiB may be returned. Use a smaller screenshot.', 'possible'));
        return;
      }
      active.imageBytes += size;
      active.content.push(content);
    }
  }

  private event(event: WorkerEvent): void {
    const active = this.active;
    if (!active || event.id !== active.id) return;
    if (event.type === 'content') this.content(active, event.content);
    else if (event.type === 'done') {
      const timedOut = /execution.*(terminated|timed out)/i.test(event.error ?? '');
      active.finish(event.error ? new CuaError(timedOut ? 'TIMEOUT' : 'JAVASCRIPT_ERROR', event.error.slice(0, 2000), 'possible') : undefined);
    }
    else if (event.type === 'native') {
      const native = this.native ??= new NativeClient(active.context);
      active.nativeBegin ??= native.request('begin', { executionId: active.id });
      const operation = (async () => {
        try {
          await active.nativeBegin;
          if (this.active !== active) throw new CuaError('EXECUTION_ENDED', 'The JavaScript execution ended');
          const value = await native.request(event.request.op, event.request.args);
          if (this.active === active && this.worker?.connected) this.worker.send({ type: 'native_result', requestId: event.requestId, value });
        } catch (error) {
          if (this.active === active && this.worker?.connected) this.worker.send({ type: 'native_result', requestId: event.requestId, error: error instanceof CuaError ? `${error.code}: ${error.message}` : error instanceof Error ? error.message : 'Native operation failed' });
        }
      })();
      active.operations.add(operation);
      void operation.finally(() => active.operations.delete(operation));
    }
  }

  async execute(args: Record<string, unknown>, context: RuntimeContext, signal?: AbortSignal): Promise<CallToolResult> {
    if (this.ended) return errorResult(new CuaError('TURN_ENDED', 'This runtime cannot resume an ended turn'));
    if (this.active || this.finishing) return errorResult(new CuaError('BUSY', 'Another JavaScript execution is active'));
    if (++this.calls > 100) return errorResult(new CuaError('CALL_LIMIT', 'Computer use reached its 100-call run budget'));
    const code = args.code;
    const timeout = args.timeout_ms ?? DEFAULT_TIMEOUT_MS;
    if (typeof code !== 'string' || !code.trim() || Buffer.byteLength(code) > MAX_CODE_BYTES) return errorResult(new CuaError('INVALID_ARGUMENT', 'code must be nonempty and at most 64 KiB'));
    if (!Number.isSafeInteger(timeout) || (timeout as number) < 1 || (timeout as number) > MAX_TIMEOUT_MS) return errorResult(new CuaError('INVALID_ARGUMENT', 'timeout_ms must be an integer from 1 to 60000'));
    if (!context.session_id || !context.turn_id) return errorResult(new CuaError('INVALID_CONTEXT', 'A backend session and turn identity are required'));
    if (this.context && (this.context.session_id !== context.session_id || this.context.turn_id !== context.turn_id)) return errorResult(new CuaError('INVALID_CONTEXT', 'A runtime cannot change its owning turn'));
    if (this.context && (this.context.generation !== context.generation || this.context.epoch !== context.epoch || this.context.input_generation !== context.input_generation)) await this.reset();
    this.context = context;
    const worker = this.startWorker();
    return await new Promise<CallToolResult>((resolve) => {
      let settled = false;
      const active: Execution = {
        id: randomUUID(), context, content: [], textBytes: 0, imageBytes: 0, imageCount: 0, operations: new Set(),
        finish: (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          signal?.removeEventListener('abort', cancel);
          this.active = undefined; // Fence late CUA callbacks before any await.
          this.finishing = (async () => {
            if (error instanceof CuaError && (['TIMEOUT', 'CANCELLED', 'OUTPUT_LIMIT', 'WORKER_CLOSED'].includes(error.code) || active.operations.size > 0)) {
              await this.stopWorker();
              await this.native?.close();
              this.native = undefined;
            }
            await Promise.allSettled([...active.operations]);
            if (active.nativeBegin) {
              try { await this.native?.request('end'); }
              catch { error ??= new CuaError('CLEANUP_UNCONFIRMED', 'Could not confirm desktop input cleanup', 'unknown'); }
            }
            const result: CallToolResult = error ? errorResult(error) : {
              content: active.content.length ? active.content : [{ type: 'text', text: 'JavaScript completed.' }],
              structuredContent: { computer_use: { title: typeof args.title === 'string' ? args.title.slice(0, 80) : 'Computer use', effect_status: 'completed' } },
            };
            if (Buffer.byteLength(JSON.stringify(result)) > MAX_RESULT_BYTES) resolve(errorResult(new CuaError('OUTPUT_LIMIT', 'Computer use response exceeded 6 MiB', 'possible')));
            else resolve(result);
          })().catch(() => resolve(errorResult(new CuaError('CLEANUP_UNCONFIRMED', 'Runtime cleanup failed', 'unknown')))).finally(() => { this.finishing = undefined; });
        },
      };
      const cancel = () => active.finish(new CuaError('CANCELLED', 'JavaScript was cancelled; state was reset and effects may already have occurred', 'unknown'));
      const timer = setTimeout(() => active.finish(new CuaError('TIMEOUT', 'JavaScript timed out; state was reset and effects may already have occurred', 'unknown')), timeout as number);
      this.active = active;
      if (signal?.aborted) { cancel(); return; }
      signal?.addEventListener('abort', cancel, { once: true });
      worker.send({ type: 'execute', id: active.id, code, timeout, context });
    });
  }

  private async stopWorker(): Promise<void> {
    const worker = this.worker;
    this.worker = undefined;
    if (!worker || worker.exitCode !== null || worker.signalCode !== null) return;
    await new Promise<void>((resolve) => {
      worker.once('exit', () => resolve());
      worker.kill('SIGKILL');
    });
  }

  async reset(): Promise<void> {
    this.active?.finish(new CuaError('CANCELLED', 'JavaScript state was reset', 'unknown'));
    await this.finishing;
    await this.stopWorker();
    await this.native?.close();
    this.native = undefined;
  }

  async end(): Promise<void> {
    this.ended = true;
    await this.reset();
  }
}
