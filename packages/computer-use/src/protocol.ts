import type { CallToolResult, ContentBlock } from '@modelcontextprotocol/sdk/types.js';

export const VERSION = '0.1.0';
export const MAX_CODE_BYTES = 64 * 1024;
export const MAX_TEXT_BYTES = 64 * 1024;
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
export const MAX_RESULT_BYTES = 6 * 1024 * 1024;
export const DEFAULT_TIMEOUT_MS = 30_000;
export const MAX_TIMEOUT_MS = 60_000;

export type RuntimeContext = {
  session_id: string;
  turn_id: string;
  generation?: number;
  input_generation?: number;
  epoch?: string;
  display?: string;
  xauthority?: string;
  dbus_session_bus_address?: string;
  screenshot_max_dimension?: number;
};

export type NativeReply = Record<string, any>;
export type NativeRequest = { op: string; args: Record<string, unknown> };
export type WorkerRequest = { type: 'execute'; id: string; code: string; timeout: number; context: RuntimeContext };
export type WorkerEvent =
  | { type: 'native'; id: string; requestId: number; request: NativeRequest }
  | { type: 'content'; id: string; content: ContentBlock }
  | { type: 'done'; id: string; error?: string };

export class CuaError extends Error {
  constructor(public code: string, message: string, public effectStatus = 'none') {
    super(message);
  }
}

export function errorResult(error: unknown): CallToolResult {
  const message = error instanceof Error ? error.message : 'Computer use failed';
  const code = error instanceof CuaError ? error.code : 'COMPUTER_USE_ERROR';
  const effect_status = error instanceof CuaError ? error.effectStatus : 'unknown';
  return {
    isError: true,
    content: [{ type: 'text', text: `${code}: ${message}`.slice(0, 4000) }],
    structuredContent: { computer_use: { code, effect_status } },
  };
}

export const DOCUMENTATION = `Computer use runs on the workspace Linux desktop. State persists between js calls in this run. A new turn or js_reset discards JavaScript bindings, not applications.
Start with exactly one entry point: await cua.getState(), await cua.listWindows(), or var app = await cua.getApp({windowId: ID_FROM_INVENTORY}). Read its output before acting.
Use only observed window IDs and element IDs. getApp returns API documentation and its first AX state. It does not activate the window. Call await app.activate() explicitly before coordinate or keyboard input.
Methods:
cua.getState(); cua.listApps(); cua.listWindows(); cua.getApp({windowId}); cua.rewriteDocumentation();
app.activate(); app.getAXState(); app.getScreenshot(); app.getAXStateAndScreenshot();
app.click(elementId | [x,y], {mouseButton?: 'left'|'right'|'middle',clickCount?:1|2});
app.pressKey('Control_L+a'); app.typeText(text, optionalElementId);
app.scroll(elementId | [x,y], 'up'|'down'|'left'|'right', {pixels: 300});
app.drag([startX,startY],[endX,endY]); app.performSecondaryAction(elementId, actionName);
Coordinates are relative to the latest emitted window screenshot. Observe again after UI changes, before another coordinate action. Element IDs belong to the latest AX snapshot of their window. Report stale references by observing again, never guessing.
AX source x11 is a limited fallback, not a full accessibility tree. Use its screenshot when elements are unavailable. Browser DOM APIs are not enabled; use the separate browser plugin for DOM automation.
Observation methods emit their text/images automatically. nodeRepl.write(value) emits additional text; await nodeRepl.emitImage(bytes) emits an image. Do not wrap observation methods in emitImage.
Await every CUA operation. Unawaited calls cannot continue after js ends. On timeout the worker is reset; effects may already have occurred. Reobserve before recovery, never blindly replay actions.`;
