import { DOCUMENTATION, type NativeReply } from './protocol.js';

type Native = (op: string, args?: Record<string, unknown>) => Promise<NativeReply>;
type ObservationOptions = { emit?: boolean; disableDiffing?: boolean };
type Target = number | [number, number];
type Write = (value: unknown, name?: string) => void;

/** Adapt the public CUA contract to OpenCuria's run-owned native I/O service. */
export function createCua(native: Native, write: Write, emitImage: (value: Uint8Array) => Promise<void>, requestMeta: () => unknown = () => undefined) {
  let documented = false;
  let documentationRequest: unknown;
  function documentation() {
    if (!documented) {
      write(DOCUMENTATION, 'cua.core');
      documented = true;
      documentationRequest = requestMeta();
    }
  }
  async function inventory(op: string, options: ObservationOptions = {}) {
    documentation();
    const result = await native(op);
    const value = result.items ?? result;
    if (options.emit !== false) write(JSON.stringify(value), 'cua.state');
    return value;
  }
  async function getApp(target: { windowId: number }) {
    documentation();
    if (!target || !Number.isSafeInteger(target.windowId) || target.windowId <= 0) throw new Error('linux getApp requires { windowId } from listApps() or listWindows().');
    const windows = (await native('windows')).items;
    if (!windows.some((window: { id: number }) => window.id === target.windowId)) throw new Error(`Window ${target.windowId} is unavailable.`);
    const binding = await native('bind', { windowId: target.windowId });
    let snapshotId = '';
    const base = () => ({ windowId: target.windowId, identity: binding.identity, snapshotId });
    const resolveTarget = (value: Target) => Array.isArray(value) ? { coordinates: value } : { elementId: value };
    async function observe(image: boolean, text: boolean, options: ObservationOptions = {}) {
      const result = await native('state', { ...base(), image, text });
      snapshotId = result.snapshotId;
      const bytes = result.image ? Buffer.from(result.image, 'base64') : undefined;
      if (options.emit !== false) {
        if (text) write(result.state, 'cua.state');
        if (bytes) await emitImage(bytes);
      }
      return bytes ? { state: result.state as string, screenshot: bytes } : { state: result.state as string };
    }
    const app = {
      getAXState: async (options?: ObservationOptions) => (await observe(false, true, options)).state,
      getScreenshot: async (options?: ObservationOptions) => (await observe(true, false, options)).screenshot!,
      getAXStateAndScreenshot: (options?: ObservationOptions) => observe(true, true, options),
      click: async (value: Target, options: { mouseButton?: string; clickCount?: number; durationMs?: number; key?: string } = {}) => {
        if (options.durationMs !== undefined) {
          if (!Array.isArray(value)) throw new Error('Linux timed clicks require window-relative coordinates.');
          if (!Number.isSafeInteger(options.durationMs) || options.durationMs < 0) throw new Error('durationMs must be a non-negative safe integer.');
        }
        if (options.key !== undefined && !Array.isArray(value)) throw new Error('Linux clicks with key require window-relative coordinates.');
        await native('click', { ...base(), ...resolveTarget(value), ...options });
      },
      drag: async (from: [number, number], to: [number, number]) => { await native('drag', { ...base(), from, to }); },
      scroll: async (value: Target, direction: string, distance?: number | { pixels: number }) => {
        if (typeof distance === 'number') throw new Error('Linux scroll accepts { pixels }, not pages.');
        if (distance && (!Number.isFinite(distance.pixels) || distance.pixels <= 0)) throw new Error('pixels must be a positive finite number.');
        await native('scroll', { ...base(), ...resolveTarget(value), direction, ...(distance ? { pixels: distance.pixels } : {}) });
      },
      selectText: async (..._args: unknown[]) => { throw new Error('selectText is unavailable on Linux.'); },
      setValue: async (..._args: unknown[]) => { throw new Error('setValue is unavailable on Linux; use click, pressKey, and typeText.'); },
      paste: async (text: string, options?: { format?: string }) => {
        if (options?.format !== undefined && options.format !== 'text') throw new Error('Linux paste supports only text format.');
        await native('type_text', { ...base(), text });
      },
      pressKey: async (key: string, options?: { durationMs?: number }) => {
        if (options?.durationMs !== undefined && (!Number.isSafeInteger(options.durationMs) || options.durationMs < 0)) throw new Error('durationMs must be a non-negative safe integer.');
        await native('press_key', { ...base(), key, ...options });
      },
      typeCharacters: async (text: string) => {
        const result = await native('type_characters', { ...base(), text });
        return { charactersSent: result.charactersSent as number };
      },
      typeText: async (text: string, elementId?: number) => { await native('type_text', { ...base(), text, elementId }); },
      performSecondaryAction: async (elementId: number, action: string) => { await native('secondary', { ...base(), elementId, action }); },
    };
    await app.getAXState();
    return app;
  }
  return {
    getState: (options?: ObservationOptions) => inventory('inventory', options),
    listWindows: (options?: ObservationOptions) => inventory('windows', options),
    listApps: (options?: ObservationOptions) => inventory('apps', options),
    getApp,
    computer: {
      target: 'linux',
      launch_app: async (input: { app: string }) => { await native('launch_app', input); },
    },
    rewriteDocumentation: async () => {
      if (!documented) { documentation(); return; }
      const meta = requestMeta();
      if (meta == null || meta !== documentationRequest) {
        write(DOCUMENTATION, 'cua.core');
        documentationRequest = meta;
      }
    },
  };
}
