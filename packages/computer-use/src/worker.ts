import inspector from 'node:inspector';
import { AsyncLocalStorage } from 'node:async_hooks';
import { inspect } from 'node:util';
import { createCua } from './api.js';
import { type NativeReply, type WorkerRequest } from './protocol.js';

const session = new inspector.Session();
session.connect();
let current: WorkerRequest | undefined;
const execution = new AsyncLocalStorage<string>();
let requestId = 0;
const pending = new Map<number, { resolve: (value: NativeReply) => void; reject: (error: Error) => void }>();

function send(value: unknown): void {
  if (process.connected) process.send?.(value);
}

function emit(content: unknown): void {
  if (!current || execution.getStore() !== current.id) throw new Error('No active JavaScript execution');
  send({ type: 'content', id: current.id, content });
}

async function native(op: string, args: Record<string, unknown> = {}): Promise<NativeReply> {
  if (!current || execution.getStore() !== current.id) throw new Error('CUA operation outside an active js call');
  const id = ++requestId;
  const promise = new Promise<NativeReply>((resolve, reject) => pending.set(id, { resolve, reject }));
  send({ type: 'native', id: current.id, requestId: id, request: { op, args } });
  return promise;
}

const write = (value: unknown): void => emit({ type: 'text', text: typeof value === 'string' ? value : inspect(value, { depth: 5, maxArrayLength: 100, maxStringLength: 16_000 }) });
const emitImage = async (value: unknown): Promise<void> => {
  if (!(value instanceof Uint8Array)) throw new Error('emitImage expects screenshot bytes');
  const data = Buffer.from(value);
  const mimeType = data[0] === 0x89 && data[1] === 0x50 ? 'image/png' : 'image/jpeg';
  emit({ type: 'image', mimeType, data: data.toString('base64') });
};
Object.assign(globalThis, {
  cua: createCua(native, write, emitImage),
  nodeRepl: Object.freeze({ write, emitImage }),
  console: Object.freeze({ log: (...values: unknown[]) => write(values.length === 1 ? values[0] : values), error: (...values: unknown[]) => write(values), warn: (...values: unknown[]) => write(values) }),
});

process.on('message', (message: any) => {
  if (message.type === 'native_result') {
    const entry = pending.get(message.requestId);
    if (!entry) return;
    pending.delete(message.requestId);
    if (message.error) entry.reject(new Error(message.error));
    else entry.resolve(message.value);
    return;
  }
  if (message.type !== 'execute' || current) return;
  current = message;
  execution.run(message.id, () => session.post('Runtime.evaluate', {
    expression: message.code,
    replMode: true,
    awaitPromise: true,
    timeout: message.timeout,
    objectGroup: message.id,
  }, (error, rawResponse) => {
    const response = rawResponse as inspector.Runtime.EvaluateReturnType | undefined;
    const id = current?.id;
    let failure = error?.message;
    if (response?.exceptionDetails) failure = response.exceptionDetails.exception?.description ?? response.exceptionDetails.text;
    if (pending.size && !failure) failure = 'Await every CUA operation before js returns.';
    current = undefined;
    for (const entry of pending.values()) entry.reject(new Error('JavaScript execution ended'));
    pending.clear();
    session.post('Runtime.releaseObjectGroup', { objectGroup: message.id });
    session.post('Runtime.discardConsoleEntries');
    send({ type: 'done', id, error: failure });
  }));
});

process.on('disconnect', () => process.exit(0));
