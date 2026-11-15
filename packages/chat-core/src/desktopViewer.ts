/**
 * Desktop viewer lease coordination shared by the webapp and the native app.
 *
 * A client that shows a workspace desktop holds a viewer *intent*: its
 * `viewer_client_id` plus a monotonically increasing `intent_revision`. The
 * coordinator starts, observes, renews and releases that intent through a
 * transport port and never infers ownership from global desktop events.
 *
 * Sessions are plain records. Adapters observe them either by wrapping each
 * record (`track`, e.g. Vue's `reactive`) or through `subscribe`/`snapshot`
 * (immutable snapshots for React's `useSyncExternalStore`).
 */

export interface DesktopViewerIntent {
  viewer_client_id: string;
  intent_revision: number;
}

export interface DesktopStatus {
  active: boolean;
  proxy_url: string | null;
  viewer_held: boolean;
  computer_use_active: boolean;
  mcp_active?: boolean;
  holder_count?: number;
  viewer_lease_state?: string;
  revision?: number;
  intent_revision?: number;
  epoch?: string | null;
}

/** REST port of the coordinator; `@opencuria/client/desktop` implements it. */
export interface DesktopViewerTransport {
  start(
    workspaceId: string,
    intent: DesktopViewerIntent,
    signal: AbortSignal,
  ): Promise<unknown>;
  stop(workspaceId: string, intent: DesktopViewerIntent): Promise<unknown>;
  renew(
    workspaceId: string,
    intent: DesktopViewerIntent,
    signal: AbortSignal,
  ): Promise<unknown>;
  status(
    workspaceId: string,
    viewerClientId: string,
    signal: AbortSignal,
  ): Promise<DesktopStatus>;
}

const UUID_RE = /^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/;

function uuidFromHex(hex: string): string {
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

/** RFC 4122 UUID. Safari and non-secure pages omit crypto.randomUUID. */
export function createViewerClientId(): string {
  const cryptoApi = (
    globalThis as {
      crypto?: {
        randomUUID?: () => string;
        getRandomValues?: (array: Uint8Array) => Uint8Array;
      };
    }
  ).crypto;
  try {
    const uuid = cryptoApi?.randomUUID?.();
    if (typeof uuid === "string" && UUID_RE.test(uuid)) return uuid;
  } catch {
    // randomUUID throws in some non-secure contexts; use getRandomValues.
  }
  try {
    const bytes = cryptoApi?.getRandomValues?.(new Uint8Array(16));
    if (bytes?.length === 16) {
      bytes[6] = (bytes[6]! & 0x0f) | 0x40;
      bytes[8] = (bytes[8]! & 0x3f) | 0x80;
      const hex = Array.from(bytes, (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join("");
      return uuidFromHex(hex);
    }
  } catch {
    // Fall through so module init never throws.
  }
  const nibbles = Array.from({ length: 32 }, () =>
    Math.floor(Math.random() * 16).toString(16),
  );
  nibbles[12] = "4";
  nibbles[16] = ((Number.parseInt(nibbles[16]!, 16) & 0x3) | 0x8).toString(16);
  return uuidFromHex(nibbles.join(""));
}

export const DESKTOP_RENEW_MS = 45_000;
const POLL_MS = 1_000;
const STARTUP_OBSERVE_MS = 1_000;
export const DESKTOP_STARTUP_MS = 30_000;
const DESKTOP_REQUEST_TIMEOUT_MS = 10_000;
const MAX_START_ATTEMPTS = 3;
/** Navigation unmounts a surface before its replacement retains the intent. */
export const RELEASE_GRACE_MS = 1_500;

interface DeferredRelease {
  timer: ReturnType<typeof setTimeout>;
  resolve: (released: boolean) => void;
}

export interface ViewerSession {
  revision: number;
  epoch: string | null;
  leaseState: string;
  wanted: boolean;
  connecting: boolean;
  error: string | null;
  status: DesktopStatus | null;
  owners: Set<object>;
  pending: Promise<void> | null;
  observation: number;
  startupDeadline: number;
  startAttempts: number;
  timer: ReturnType<typeof setTimeout> | null;
  startupTimer: ReturnType<typeof setTimeout> | null;
  release: DeferredRelease | null;
}

/** The observable part of a session, immutable for React snapshots. */
export interface DesktopViewerSnapshot {
  readonly revision: number;
  readonly epoch: string | null;
  readonly leaseState: string;
  readonly wanted: boolean;
  readonly connecting: boolean;
  readonly error: string | null;
  readonly status: DesktopStatus | null;
  /** Lease held and the desktop route ready: mount the viewer. */
  readonly ready: boolean;
  /** Startup could not be confirmed within its deadline. */
  readonly startupFailed: boolean;
  /**
   * Proxy route of this client's desktop while one is reported, also during
   * transient outages. A mounted viewer stays on it; a new one waits for `ready`.
   */
  readonly route: string | null;
}

export interface DesktopViewerCoordinatorOptions {
  transport: DesktopViewerTransport;
  /** Defaults to a new random id; never share it between browser tabs. */
  viewerClientId?: string;
  /** Wraps each new session record before use, e.g. Vue's `reactive`. */
  track?: (session: ViewerSession) => ViewerSession;
}

export interface DesktopViewerCoordinator {
  readonly viewerClientId: string;
  /** The live, mutable session record (created on first access). */
  session(workspaceId: string): ViewerSession;
  /** Cached immutable view of the session; replaced on every change. */
  snapshot(workspaceId: string): DesktopViewerSnapshot;
  /** Notified (coalesced per microtask) whenever the session changes. */
  subscribe(workspaceId: string, listener: () => void): () => void;
  /** The intent the viewer URL must carry, or null while none is wanted. */
  intent(workspaceId: string): DesktopViewerIntent | null;
  /** Query own lease, never infer ownership from global desktop events. */
  refresh(workspaceId: string, renew?: boolean): Promise<DesktopStatus | null>;
  /** Count a surface as owner; cancels a pending deferred release. */
  retain(workspaceId: string, owner: object): void;
  /** Start (or recover) this client's viewer intent. */
  acquire(workspaceId: string, recovery?: boolean): Promise<void>;
  /** Final release: immediately sends a tombstone, even while starting. */
  close(workspaceId: string): Promise<boolean>;
  /** Last surface gone: release after a grace period unless re-retained. */
  release(workspaceId: string, owner: object): Promise<boolean>;
  /**
   * The viewer lost its connection. Without a mounted viewer a still-wanted
   * intent is restored via `start`; otherwise the lease is renewed and
   * confirmed. Resolves true when a mounted viewer should remount.
   */
  reconnect(
    workspaceId: string,
    viewerMounted: boolean,
    start?: () => Promise<void>,
  ): Promise<boolean>;
}

function isReady(session: ViewerSession): boolean {
  return (
    session.leaseState === "held" &&
    Boolean(session.status?.active) &&
    Boolean(session.status?.proxy_url)
  );
}

function withDesktopRequest<T>(
  request: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const requestPromise = Promise.resolve().then(() =>
    request(controller.signal),
  );
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error("Desktop request timed out"));
    }, DESKTOP_REQUEST_TIMEOUT_MS);
  });
  return Promise.race([requestPromise, deadline]).finally(() => {
    if (timeout !== undefined) clearTimeout(timeout);
  });
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Create the per-client registry of desktop viewer sessions. */
export function createDesktopViewerCoordinator(
  options: DesktopViewerCoordinatorOptions,
): DesktopViewerCoordinator {
  const { transport } = options;
  const viewerClientId = options.viewerClientId ?? createViewerClientId();
  const track = options.track ?? ((session: ViewerSession) => session);
  const sessions = new Map<string, ViewerSession>();
  const snapshots = new Map<string, DesktopViewerSnapshot>();
  const listeners = new Map<string, Set<() => void>>();
  const pendingNotifications = new Set<string>();

  function notify(workspaceId: string): void {
    snapshots.delete(workspaceId);
    if (pendingNotifications.has(workspaceId)) return;
    pendingNotifications.add(workspaceId);
    queueMicrotask(() => {
      pendingNotifications.delete(workspaceId);
      for (const listener of [...(listeners.get(workspaceId) ?? [])])
        listener();
    });
  }

  function session(workspaceId: string): ViewerSession {
    let existing = sessions.get(workspaceId);
    if (!existing) {
      const record: ViewerSession = {
        revision: 0,
        epoch: null,
        leaseState: "unknown",
        wanted: false,
        connecting: false,
        error: null,
        status: null,
        owners: new Set(),
        pending: null,
        timer: null,
        startupTimer: null,
        observation: 0,
        startupDeadline: 0,
        startAttempts: 0,
        release: null,
      };
      const observed = new Proxy(record, {
        set(target, key, value) {
          const changed = !Object.is(
            (target as unknown as Record<PropertyKey, unknown>)[key],
            value,
          );
          (target as unknown as Record<PropertyKey, unknown>)[key] = value;
          if (changed) notify(workspaceId);
          return true;
        },
      });
      existing = track(observed);
      sessions.set(workspaceId, existing);
    }
    return existing;
  }

  function snapshot(workspaceId: string): DesktopViewerSnapshot {
    const cached = snapshots.get(workspaceId);
    if (cached) return cached;
    const current = session(workspaceId);
    const startupFailed = Boolean(
      current.startupDeadline && !current.connecting && current.error,
    );
    const observedRevision =
      current.status?.revision ?? current.status?.intent_revision;
    const route =
      current.wanted &&
      !startupFailed &&
      current.status?.active &&
      current.status.proxy_url &&
      (observedRevision === undefined || observedRevision === current.revision)
        ? current.status.proxy_url
        : null;
    const next: DesktopViewerSnapshot = Object.freeze({
      revision: current.revision,
      epoch: current.epoch,
      leaseState: current.leaseState,
      wanted: current.wanted,
      connecting: current.connecting,
      error: current.error,
      status: current.status ? Object.freeze({ ...current.status }) : null,
      ready: current.wanted && isReady(current),
      startupFailed,
      route,
    });
    snapshots.set(workspaceId, next);
    return next;
  }

  function subscribe(workspaceId: string, listener: () => void): () => void {
    let set = listeners.get(workspaceId);
    if (!set) listeners.set(workspaceId, (set = new Set()));
    set.add(listener);
    return () => {
      set.delete(listener);
      if (set.size === 0) listeners.delete(workspaceId);
    };
  }

  function intentOf(current: ViewerSession): DesktopViewerIntent {
    return {
      viewer_client_id: viewerClientId,
      intent_revision: current.revision,
    };
  }

  function intent(workspaceId: string): DesktopViewerIntent | null {
    const current = session(workspaceId);
    return current.wanted && current.revision > 0 ? intentOf(current) : null;
  }

  function cancelTimer(current: ViewerSession): void {
    if (current.timer !== null) clearTimeout(current.timer);
    current.timer = null;
  }

  function cancelStartupTimer(current: ViewerSession): void {
    if (current.startupTimer !== null) clearTimeout(current.startupTimer);
    current.startupTimer = null;
  }

  function isCurrent(current: ViewerSession, revision: number): boolean {
    return (
      current.wanted && current.owners.size > 0 && current.revision === revision
    );
  }

  function schedule(
    workspaceId: string,
    current: ViewerSession,
    delay: number,
    renew = false,
  ): void {
    cancelTimer(current);
    if (!isCurrent(current, current.revision)) return;
    current.timer = setTimeout(() => {
      current.timer = null;
      void refresh(workspaceId, renew);
    }, delay);
  }

  function scheduleStartupDeadline(
    current: ViewerSession,
    revision: number,
  ): void {
    cancelStartupTimer(current);
    const delay = Math.max(0, current.startupDeadline - Date.now());
    current.startupTimer = setTimeout(() => {
      current.startupTimer = null;
      if (!isCurrent(current, revision) || !current.startupDeadline) return;
      failStartup(current);
    }, delay);
  }

  function failStartup(current: ViewerSession): void {
    cancelTimer(current);
    cancelStartupTimer(current);
    current.pending = null;
    current.observation += 1;
    current.connecting = false;
    current.error =
      "Desktop startup could not be confirmed. Close and try again.";
  }

  function refresh(
    workspaceId: string,
    renew = false,
  ): Promise<DesktopStatus | null> {
    const current = session(workspaceId);
    return observe(
      workspaceId,
      current,
      current.revision,
      current.wanted,
      renew,
    );
  }

  async function observe(
    workspaceId: string,
    current: ViewerSession,
    revision: number,
    wanted: boolean,
    renew: boolean,
  ): Promise<DesktopStatus | null> {
    const observation = ++current.observation;
    const isLatest = () =>
      current.observation === observation &&
      current.revision === revision &&
      current.wanted === wanted;
    try {
      if (
        renew &&
        isCurrent(current, revision) &&
        current.leaseState === "held"
      ) {
        // Renew is only allowed for a confirmed existing intent. Failed renewal
        // is followed by status, not by an implicit start.
        await withDesktopRequest((signal) =>
          transport.renew(workspaceId, intentOf(current), signal),
        ).catch(() => undefined);
        if (!isLatest()) return null;
      }
      const status = await withDesktopRequest((signal) =>
        transport.status(workspaceId, viewerClientId, signal),
      );
      if (!isLatest()) return null;
      if (current.startupDeadline && Date.now() >= current.startupDeadline) {
        failStartup(current);
        return null;
      }
      const wasReady = isReady(current);
      current.status = status;
      const remoteRevision = status.revision ?? status.intent_revision;
      const state = status.viewer_lease_state ?? "unknown";
      const epochChanged = Boolean(
        current.epoch && status.epoch && current.epoch !== status.epoch,
      );
      current.epoch = status.epoch ?? null;
      if (remoteRevision !== undefined && remoteRevision > current.revision) {
        current.revision = remoteRevision;
      }
      // A global active/viewer_held summary is explicitly not our membership.
      current.leaseState = remoteRevision === revision ? state : "unknown";
      if (!isCurrent(current, current.revision)) {
        if (!current.wanted && current.revision === revision) {
          current.status = null;
          current.leaseState = "released";
        }
        return status;
      }
      const terminal = ["expired", "released"].includes(state);
      const olderEnded =
        remoteRevision !== undefined && remoteRevision < revision && terminal;
      // A 202 start is only dispatch acknowledgement. It may have been rejected
      // while the previous revision was closing. Retry only after its terminal
      // tombstone is conclusively observed, with a newer revision and a bound.
      if (
        epochChanged ||
        olderEnded ||
        ["expired", "released"].includes(current.leaseState)
      ) {
        current.pending = null;
        current.leaseState = "unknown";
        await acquire(workspaceId, true);
        // Recovery can replace an expired response with a fresh status snapshot.
        return current.status;
      }
      if (!isReady(current) && state === "held" && wasReady) {
        // A route can disappear after startup. Re-observe promptly without
        // reacquiring the already-held viewer intent.
        current.startupDeadline = Date.now() + DESKTOP_STARTUP_MS;
        current.startAttempts = 0;
        scheduleStartupDeadline(current, revision);
      } else if (
        !isReady(current) &&
        state === "held" &&
        !current.startupDeadline
      ) {
        // Preserve the bounded readiness poll even when this client begins
        // observing an intent that is already held.
        current.startupDeadline = Date.now() + DESKTOP_STARTUP_MS;
        scheduleStartupDeadline(current, revision);
      }
      current.connecting = current.startupDeadline
        ? !isReady(current)
        : current.leaseState !== "held";
      if (isReady(current)) {
        current.startupDeadline = 0;
        current.startAttempts = 0;
        cancelStartupTimer(current);
      }
      if (isReady(current)) current.error = null;
      if (current.connecting) {
        // Routing can lag durable lease ownership. Poll status only until both
        // are ready; do not renew the lease on each readiness observation.
        schedule(workspaceId, current, POLL_MS);
      } else {
        current.error = null;
        schedule(workspaceId, current, DESKTOP_RENEW_MS, true);
      }
      return status;
    } catch (error) {
      if (!isLatest() || !isCurrent(current, revision)) return null;
      if (current.startupDeadline && Date.now() >= current.startupDeadline) {
        failStartup(current);
        return null;
      }
      current.error = messageOf(error);
      // An outage is not confirmation of this client's lease. Observe promptly while starting.
      current.leaseState = "unknown";
      schedule(
        workspaceId,
        current,
        current.startupDeadline ? STARTUP_OBSERVE_MS : DESKTOP_RENEW_MS,
        !current.startupDeadline,
      );
      return null;
    }
  }

  function cancelDeferredRelease(current: ViewerSession): void {
    const release = current.release;
    if (!release) return;
    current.release = null;
    clearTimeout(release.timer);
    release.resolve(true);
  }

  function retain(workspaceId: string, owner: object): void {
    const current = session(workspaceId);
    cancelDeferredRelease(current);
    current.owners.add(owner);
  }

  function acquire(workspaceId: string, recovery = false): Promise<void> {
    const current = session(workspaceId);
    if (current.owners.size === 0) return Promise.resolve();
    if (current.pending) return current.pending;
    if (current.wanted && isReady(current)) return Promise.resolve();
    cancelTimer(current);
    if (!recovery || !current.startupDeadline) {
      current.startupDeadline = Date.now() + DESKTOP_STARTUP_MS;
      current.startAttempts = 0;
    }
    if (
      current.startAttempts >= MAX_START_ATTEMPTS ||
      Date.now() >= current.startupDeadline
    ) {
      failStartup(current);
      return Promise.resolve();
    }
    current.startAttempts += 1;
    current.observation += 1;
    current.revision += 1;
    const revision = current.revision;
    current.wanted = true;
    current.connecting = true;
    current.leaseState = "unknown";
    current.error = null;
    scheduleStartupDeadline(current, revision);
    const payload = intentOf(current);
    const request = (async () => {
      try {
        await withDesktopRequest((signal) =>
          transport.start(workspaceId, payload, signal),
        );
        if (!isCurrent(current, revision)) return;
        await refresh(workspaceId);
      } catch (error) {
        if (!isCurrent(current, revision)) return;
        if (current.startupDeadline && Date.now() >= current.startupDeadline) {
          failStartup(current);
        } else {
          current.error = messageOf(error);
          // The POST may have reached the runner. Preserve intent and observe only.
          schedule(workspaceId, current, STARTUP_OBSERVE_MS);
        }
      } finally {
        if (current.revision === revision) current.pending = null;
      }
    })();
    current.pending = request;
    return request;
  }

  async function close(workspaceId: string): Promise<boolean> {
    const current = session(workspaceId);
    cancelDeferredRelease(current);
    if (!current.wanted) return true;
    const payload = intentOf(current);
    current.wanted = false;
    current.status = null;
    current.connecting = false;
    current.leaseState = "released";
    current.pending = null;
    current.observation += 1;
    current.startupDeadline = 0;
    cancelTimer(current);
    cancelStartupTimer(current);
    try {
      await transport.stop(workspaceId, payload);
      if (current.revision === payload.intent_revision && !current.wanted) {
        await refresh(workspaceId);
      }
      return true;
    } catch (error) {
      if (current.revision !== payload.intent_revision || current.wanted)
        return false;
      current.error = messageOf(error);
      // Retry the same tombstone during outages, never a newer intent.
      current.timer = setTimeout(() => {
        current.timer = null;
        if (current.revision !== payload.intent_revision || current.wanted)
          return;
        current.wanted = true;
        void close(workspaceId);
      }, DESKTOP_RENEW_MS);
      return false;
    }
  }

  function release(workspaceId: string, owner: object): Promise<boolean> {
    const current = session(workspaceId);
    current.owners.delete(owner);
    if (current.owners.size > 0 || !current.wanted)
      return Promise.resolve(true);
    cancelDeferredRelease(current);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        current.release = null;
        if (current.owners.size > 0) return resolve(true);
        void close(workspaceId).then(resolve);
      }, RELEASE_GRACE_MS);
      current.release = { timer, resolve };
    });
  }

  async function reconnect(
    workspaceId: string,
    viewerMounted: boolean,
    start: () => Promise<void> = () => acquire(workspaceId),
  ): Promise<boolean> {
    if (!viewerMounted) {
      // Recovery restores only a still-wanted intent; never restart a
      // desktop this client stopped (or never asked for) once its route ends.
      if (session(workspaceId).wanted) await start();
      return false;
    }
    const status = await refresh(workspaceId, true);
    return Boolean(status?.active && status.proxy_url);
  }

  return {
    viewerClientId,
    session,
    snapshot,
    subscribe,
    intent,
    refresh,
    retain,
    acquire,
    close,
    release,
    reconnect,
  };
}
