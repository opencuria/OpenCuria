/**
 * Viewer-side rules for the embedded KasmVNC desktop, shared by the webapp
 * iframe and the native WebView: viewer URL, framebuffer size, connection
 * status messages and bounded reconnects.
 */
import type { DesktopViewerIntent } from "./desktopViewer";
import {
  DEFAULT_DESKTOP_HEIGHT,
  DEFAULT_DESKTOP_WIDTH,
} from "./workspaceCreation";

export { DEFAULT_DESKTOP_HEIGHT, DEFAULT_DESKTOP_WIDTH };

/** The workspace's fixed framebuffer size, falling back to the defaults. */
export function workspaceDesktopSize(
  workspace?: {
    desktop_width?: number | null;
    desktop_height?: number | null;
  } | null,
): { width: number; height: number } {
  return {
    width: workspace?.desktop_width || DEFAULT_DESKTOP_WIDTH,
    height: workspace?.desktop_height || DEFAULT_DESKTOP_HEIGHT,
  };
}

/**
 * URL of the KasmVNC client behind the backend's desktop proxy. `ticket` is
 * a single-use desktop ticket, never an access token: the proxy trades it
 * for a path-scoped cookie and derives the WebSocket path, including the
 * viewer intent, itself.
 */
export function buildDesktopViewerUrl(
  base: string,
  proxyUrl: string,
  ticket: string,
  intent?: DesktopViewerIntent,
): string {
  const [path, query = ""] = proxyUrl.split("?");
  const params = new URLSearchParams(query);
  params.set("ticket", ticket);
  params.set("resize", "scale");
  if (intent) {
    params.set("viewer_client_id", intent.viewer_client_id);
    params.set("intent_revision", String(intent.intent_revision));
  }
  return `${base}${path}?${params}`;
}

export type DesktopConnectionStatus =
  | "connected"
  | "disconnected"
  | "connecting";

const DESKTOP_CONNECTION_STATUSES = new Set<DesktopConnectionStatus>([
  "connected",
  "disconnected",
  "connecting",
]);

/** Read a `{ action: 'connection_state', value }` message from the viewer. */
export function parseDesktopConnectionStatus(
  value: unknown,
): DesktopConnectionStatus | null {
  if (!value || typeof value !== "object") return null;
  const message = value as { action?: unknown; value?: unknown };
  if (message.action !== "connection_state") return null;
  return typeof message.value === "string" &&
    DESKTOP_CONNECTION_STATUSES.has(message.value as DesktopConnectionStatus)
    ? (message.value as DesktopConnectionStatus)
    : null;
}

/** How long a loaded viewer may take to report a live connection. */
export const DESKTOP_CONNECTION_TIMEOUT_MS = 15_000;

export interface PausableTimer {
  start(delayMs: number): void;
  pause(): void;
  resume(): void;
  cancel(): void;
  readonly pending: boolean;
}

/** A one-shot timer whose remaining duration is retained while the app is hidden. */
export function createPausableTimer(callback: () => void): PausableTimer {
  let handle: ReturnType<typeof setTimeout> | null = null;
  let remainingMs: number | null = null;
  let startedAt = 0;

  const schedule = (delayMs: number): void => {
    remainingMs = Math.max(0, delayMs);
    startedAt = Date.now();
    handle = setTimeout(() => {
      handle = null;
      remainingMs = null;
      callback();
    }, remainingMs);
  };

  return {
    start(delayMs) {
      this.cancel();
      schedule(delayMs);
    },
    pause() {
      if (handle === null || remainingMs === null) return;
      clearTimeout(handle);
      remainingMs = Math.max(0, remainingMs - (Date.now() - startedAt));
      handle = null;
    },
    resume() {
      if (handle !== null || remainingMs === null) return;
      schedule(remainingMs);
    },
    cancel() {
      if (handle !== null) clearTimeout(handle);
      handle = null;
      remainingMs = null;
    },
    get pending() {
      return handle !== null || remainingMs !== null;
    },
  };
}

export interface DesktopReconnectBackoff {
  schedule(): void;
  pause(): void;
  resume(): void;
  reset(): void;
  retryNow(): void;
  readonly attempts: number;
}

/** Retry a lost viewer up to three times with capped exponential backoff. */
export function createDesktopReconnectBackoff(
  onRetry: () => void,
  onExhausted: () => void,
): DesktopReconnectBackoff {
  const delays = [1_000, 2_000, 4_000];
  let attempts = 0;
  const timer = createPausableTimer(onRetry);

  return {
    schedule() {
      if (timer.pending) return;
      if (attempts >= delays.length) {
        onExhausted();
        return;
      }
      timer.start(delays[attempts] ?? delays[delays.length - 1]!);
      attempts += 1;
    },
    pause: () => timer.pause(),
    resume: () => timer.resume(),
    reset() {
      timer.cancel();
      attempts = 0;
    },
    retryNow() {
      timer.cancel();
      attempts = 0;
      onRetry();
    },
    get attempts() {
      return attempts;
    },
  };
}

/**
 * Run id of a Computer Use subtask event, or null for other agents. While any
 * such run is active the desktop is read-only for people.
 */
export function computerUseRunId(event: {
  agent?: string | null;
  subtask_id: string;
  child_session_id?: string | null;
}): string | null {
  if ((event.agent || "").toLowerCase() !== "computeruse") return null;
  return event.child_session_id || event.subtask_id;
}
