import inspector from 'node:inspector';
import { AsyncLocalStorage } from 'node:async_hooks';
import { format, inspect } from 'node:util';
import { readFile } from 'node:fs/promises';
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

function emit(content: unknown, name?: string): void {
  if (!current || execution.getStore() !== current.id) throw new Error('No active JavaScript execution');
  send({ type: 'content', id: current.id, content, name });
}

async function native(op: string, args: Record<string, unknown> = {}): Promise<NativeReply> {
  if (!current || execution.getStore() !== current.id) throw new Error('CUA operation outside an active js call');
  const id = ++requestId;
  const promise = new Promise<NativeReply>((resolve, reject) => pending.set(id, { resolve, reject }));
  send({ type: 'native', id: current.id, requestId: id, request: { op, args } });
  return promise;
}

let pendingConsoleNewline = false;
const write = (value: unknown, name?: string): void => {
  if (pendingConsoleNewline) {
    emit({ type: 'text', text: '\n' });
    pendingConsoleNewline = false;
  }
  emit({ type: 'text', text: typeof value === 'string' ? value : inspect(value) }, name);
};
const consoleLine = (...values: unknown[]): void => {
  write(format(...values));
  pendingConsoleNewline = true;
};
const emitImage = async (value: unknown): Promise<void> => {
  let mimeType: string | undefined;
  if (typeof value === 'object' && value !== null && 'bytes' in value) {
    const input = value as { bytes: unknown; mimeType?: string };
    mimeType = input.mimeType;
    value = input.bytes;
  }
  if (typeof value === 'string') {
    if (value.startsWith('file:')) value = await readFile(new URL(value));
    else {
      const match = /^data:(image\/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})$/.exec(value);
      if (!match) throw new Error('emitImage expects a data URL, file URL, image bytes, or { bytes, mimeType }.');
      mimeType = match[1];
      value = Buffer.from(match[2], 'base64');
    }
  }
  if (!(value instanceof Uint8Array)) throw new Error('emitImage expects a data URL, file URL, image bytes, or { bytes, mimeType }.');
  const data = Buffer.from(value);
  mimeType ??= data.subarray(0, 4).toString('hex') === '89504e47' ? 'image/png'
    : data.subarray(0, 3).toString('hex') === 'ffd8ff' ? 'image/jpeg'
    : data.subarray(0, 4).toString() === 'RIFF' && data.subarray(8, 12).toString() === 'WEBP' ? 'image/webp' : undefined;
  if (!mimeType || !['image/png', 'image/jpeg', 'image/webp'].includes(mimeType)) throw new Error('Unsupported image format.');
  emit({ type: 'image', mimeType, data: data.toString('base64'), _meta: { 'codex/imageDetail': 'original' } });
};
const cua = createCua(native, write, emitImage, () => current?.context.request_meta);
Object.assign(globalThis, {
  cua,
  nodeRepl: Object.freeze({ write, emitImage, get requestMeta() { return current?.context.request_meta; } }),
  console: Object.freeze({ log: consoleLine, info: consoleLine, debug: consoleLine, error: consoleLine, warn: consoleLine, dir: (value: unknown) => consoleLine(inspect(value)) }),
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
  pendingConsoleNewline = false;
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
    if (response?.exceptionDetails) {
      const description = response.exceptionDetails.exception?.description ?? response.exceptionDetails.text;
      failure = description.split('\n    at ')[0].replace(/^(?:Uncaught )?(?:\w*Error): /, '');
      const token = /^Unexpected token '(.*)'$/.exec(failure);
      if (response.exceptionDetails.exception?.className === 'SyntaxError' && token) {
        const line = response.exceptionDetails.lineNumber + 1;
        const column = response.exceptionDetails.columnNumber;
        failure = `[${line}:${column}-${line}:${column + token[1].length}]: Unexpected token: '${token[1]}'`;
      }
    }
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
