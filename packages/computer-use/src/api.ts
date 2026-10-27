import { DOCUMENTATION, type NativeReply } from './protocol.js';

type Native = (op: string, args?: Record<string, unknown>) => Promise<NativeReply>;
type Options = { emit?: boolean };
type Target = number | [number, number];

export function createCua(native: Native, write: (value: unknown) => void, emitImage: (value: Uint8Array) => Promise<void>) {
  let documented = false;
  const documentation = () => { if (!documented) { write(DOCUMENTATION); documented = true; } };

  async function inventory(op: string, options: Options = {}) {
    documentation();
    const result = await native(op);
    const value = result.items ?? result;
    if (options.emit !== false) write(value);
    return value;
  }

  async function getApp(target: { windowId: number }) {
    if (!target || !Number.isSafeInteger(target.windowId) || target.windowId <= 0) throw new Error('Linux getApp requires {windowId} from listWindows().');
    documentation();
    const binding = await native('bind', { windowId: target.windowId });
    let snapshotId = '';
    let screenshotId = '';
    const base = () => ({ windowId: target.windowId, identity: binding.identity, snapshotId });
    const resolveTarget = (value: Target) => Array.isArray(value)
      ? { coordinates: value, screenshotId }
      : { elementId: value };
    async function observe(image: boolean, text: boolean, options: Options = {}) {
      const result = await native('state', { ...base(), image, text });
      if (text) snapshotId = result.snapshotId;
      if (image) screenshotId = result.screenshotId;
      const bytes = result.image ? Buffer.from(result.image, 'base64') : undefined;
      if (options.emit !== false) {
        if (text) write(result.state);
        if (bytes) {
          write({ screenshot: { id: result.screenshotId, width: result.width, height: result.height, windowId: target.windowId, generation: result.generation } });
          await emitImage(bytes);
        }
      }
      return { state: result.state as string, screenshot: bytes };
    }
    const app = {
      windowId: target.windowId,
      activate: () => native('activate', base()).then(() => undefined),
      getAXState: async (options?: Options) => (await observe(false, true, options)).state,
      getScreenshot: async (options?: Options) => (await observe(true, false, options)).screenshot!,
      getAXStateAndScreenshot: (options?: Options) => observe(true, true, options),
      click: async (value: Target, options: Record<string, unknown> = {}) => { await native('click', { ...base(), ...resolveTarget(value), ...options }); },
      pressKey: async (key: string) => { await native('press_key', { ...base(), key }); },
      typeText: async (text: string, elementId?: number) => { await native('type_text', { ...base(), text, elementId }); },
      scroll: async (value: Target, direction: string, distance: { pixels: number } = { pixels: 300 }) => { await native('scroll', { ...base(), ...resolveTarget(value), direction, pixels: distance.pixels }); },
      drag: async (from: [number, number], to: [number, number]) => { await native('drag', { ...base(), from, to, screenshotId }); },
      performSecondaryAction: async (elementId: number, action: string) => { await native('secondary', { ...base(), elementId, action }); },
    };
    await app.getAXState();
    return app;
  }
  return Object.freeze({
    getState: (options?: Options) => inventory('inventory', options),
    listWindows: (options?: Options) => inventory('windows', options),
    listApps: (options?: Options) => inventory('apps', options),
    getApp,
    rewriteDocumentation: async () => { documented = false; documentation(); },
  });
}
