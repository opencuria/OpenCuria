import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  mediaLoadMode,
  type WorkspaceFileLink,
} from "@opencuria/chat-core/workspaceFiles";

/** A workspace media URI, optionally held until `release()`. */
export type ResolvedChatMedia = string | { uri: string; release: () => void };

/** What a media element shows before its file loads. */
export type ChatMediaPreview = Pick<WorkspaceFileLink, "size" | "thumbnailUrl">;

/**
 * Loads workspace media for chat views. With `describe`, media above the
 * autoload size wait for a tap (`mediaLoadMode`); `describe` resolving to
 * null (e.g. a copy saved offline) loads the file right away.
 */
export interface ChatMediaResolver {
  (path: string): Promise<ResolvedChatMedia>;
  describe?: (path: string) => Promise<ChatMediaPreview | null>;
}

export type WorkspaceMediaState =
  /** `preview` stays while a tapped file loads. */
  | { status: "loading"; preview?: ChatMediaPreview }
  | { status: "ready"; uri: string }
  /** Waits for `request(path)`, or cannot load at all. */
  | { status: "tap" | "too_large"; preview: ChatMediaPreview }
  | { status: "failed" };

/** A media element that shows its preview instead of its file. */
export type MediaPlaceholderState = {
  status: "tap" | "too_large" | "loading";
  preview: ChatMediaPreview;
};

/** The placeholder `state` shows, if any. */
export function placeholderState(
  state: WorkspaceMediaState | undefined,
): MediaPlaceholderState | null {
  if (state?.status === "tap" || state?.status === "too_large") return state;
  if (state?.status === "loading" && state.preview)
    return { status: "loading", preview: state.preview };
  return null;
}

/**
 * Delays before automatic retries of a failed read. Reads fail transiently
 * while the workspace (re)connects, a transfer times out or a file is still
 * being written; once these are spent, a tap retries.
 */
export const MEDIA_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

interface Entry {
  state: WorkspaceMediaState;
  release?: () => void;
  failures: number;
  /** Ready files that the image view could not display. */
  displayFailures: number;
  timer?: ReturnType<typeof setTimeout>;
  /** Invalidates in-flight reads and timers of a superseded attempt. */
  attempt: number;
}

type Describe = NonNullable<ChatMediaResolver["describe"]>;

/**
 * Paths the user asked to load, per media source (one per workspace, server
 * and account) for the app session, so scrolled or reopened rows load them
 * again without asking. Every view of a requested path loads it.
 */
const requested = new WeakMap<
  Describe,
  { paths: Set<string>; listeners: Set<(path: string) => void> }
>();

function requestsOf(describe: Describe) {
  let entry = requested.get(describe);
  if (!entry) {
    entry = { paths: new Set(), listeners: new Set() };
    requested.set(describe, entry);
  }
  return entry;
}

const LOADING: WorkspaceMediaState = { status: "loading" };
const FAILED: WorkspaceMediaState = { status: "failed" };
const NONE: Record<string, WorkspaceMediaState> = {};

/**
 * Load workspace media for the given paths while `active`, retrying failed
 * reads. Each path keeps one lease; leases are released when a path goes
 * away, the row becomes inactive, the scope changes or on unmount.
 */
export function useWorkspaceMedia({
  paths,
  resolve,
  scope,
  active,
}: {
  paths: readonly string[];
  resolve?: ChatMediaResolver;
  scope: string;
  active: boolean;
}): {
  media: Record<string, WorkspaceMediaState>;
  /** Read a failed path again. */
  retry: (path: string) => void;
  /** Load a path above the autoload size; remembered for the session. */
  request: (path: string) => void;
  /** The view could not show a ready file (e.g. it was evicted): read again once. */
  reportDisplayError: (path: string) => void;
} {
  const resolveRef = useRef(resolve);
  resolveRef.current = resolve;
  const describe = resolve?.describe;
  const entries = useRef(new Map<string, Entry>());
  const entriesScope = useRef(scope);
  // Published states carry their scope, so a new scope never shows old files.
  const [published, setPublished] = useState({ scope, media: NONE });
  const enabled = active && Boolean(resolve);
  const pathKey = useMemo(() => JSON.stringify([...new Set(paths)]), [paths]);

  const publish = useCallback(() => {
    const next: Record<string, WorkspaceMediaState> = {};
    for (const [path, entry] of entries.current) next[path] = entry.state;
    const nextScope = entriesScope.current;
    setPublished((previous) => {
      const keys = Object.keys(next);
      return previous.scope === nextScope &&
        keys.length === Object.keys(previous.media).length &&
        keys.every((key) => previous.media[key] === next[key])
        ? previous
        : { scope: nextScope, media: keys.length ? next : NONE };
    });
  }, []);

  const load = useCallback(
    (path: string, entry: Entry) => {
      const attempt = ++entry.attempt;
      if (entry.timer) clearTimeout(entry.timer);
      entry.timer = undefined;
      const preview =
        entry.state.status === "tap" ? entry.state.preview : undefined;
      entry.state = preview ? { status: "loading", preview } : LOADING;
      const current = () =>
        entries.current.get(path) === entry && entry.attempt === attempt;
      void (async () => {
        const resolver = resolveRef.current;
        if (!resolver) throw new Error("Media resolver unavailable.");
        const link = resolver.describe ? await resolver.describe(path) : null;
        if (!current()) return;
        const mode = link
          ? mediaLoadMode(link, requestsOf(resolver.describe!).paths.has(path))
          : "auto";
        if (mode !== "auto") {
          entry.failures = 0;
          entry.state = {
            status: mode,
            preview: { size: link!.size, thumbnailUrl: link!.thumbnailUrl },
          };
          publish();
          return;
        }
        const resolved = await resolver(path);
        const uri = typeof resolved === "string" ? resolved : resolved?.uri;
        const release =
          typeof resolved === "string" ? undefined : resolved?.release;
        if (!current()) {
          release?.();
          return;
        }
        if (!uri) {
          release?.();
          throw new Error("The media file is unavailable.");
        }
        entry.release = release;
        entry.failures = 0;
        entry.state = { status: "ready", uri };
        publish();
      })().catch((error: unknown) => {
        if (!current()) return;
        entry.failures += 1;
        const delay = isRetryable(error)
          ? MEDIA_RETRY_DELAYS_MS[entry.failures - 1]
          : undefined;
        if (delay === undefined) {
          entry.state = FAILED;
        } else {
          entry.timer = setTimeout(() => {
            entry.timer = undefined;
            if (current()) load(path, entry);
          }, delay);
        }
        publish();
      });
    },
    [publish],
  );

  const releaseAll = useCallback(() => {
    for (const entry of entries.current.values()) dispose(entry);
    entries.current.clear();
  }, []);

  useEffect(() => releaseAll, [releaseAll]);

  useEffect(() => {
    const map = entries.current;
    if (entriesScope.current !== scope) {
      entriesScope.current = scope;
      releaseAll();
    }
    if (!enabled) {
      releaseAll();
      publish();
      return;
    }
    const wanted = new Set<string>(JSON.parse(pathKey) as string[]);
    for (const [path, entry] of map)
      if (!wanted.has(path)) {
        dispose(entry);
        map.delete(path);
      }
    for (const path of wanted)
      if (!map.has(path)) {
        const entry: Entry = {
          state: LOADING,
          failures: 0,
          displayFailures: 0,
          attempt: 0,
        };
        map.set(path, entry);
        load(path, entry);
      }
    publish();
  }, [enabled, load, pathKey, publish, releaseAll, scope]);

  // A path requested anywhere loads in every view that waits for it.
  useEffect(() => {
    if (!describe) return;
    const { listeners } = requestsOf(describe);
    const onRequest = (path: string) => {
      const entry = entries.current.get(path);
      if (entry?.state.status !== "tap") return;
      load(path, entry);
      publish();
    };
    listeners.add(onRequest);
    return () => {
      listeners.delete(onRequest);
    };
  }, [describe, load, publish]);

  const retry = useCallback(
    (path: string) => {
      const entry = entries.current.get(path);
      if (!entry || entry.state.status !== "failed") return;
      entry.failures = 0;
      entry.displayFailures = 0;
      load(path, entry);
      publish();
    },
    [load, publish],
  );

  const request = useCallback(
    (path: string) => {
      if (!describe) return;
      const requests = requestsOf(describe);
      if (requests.paths.has(path)) return;
      requests.paths.add(path);
      for (const listener of [...requests.listeners]) listener(path);
    },
    [describe],
  );

  const reportDisplayError = useCallback(
    (path: string) => {
      const entry = entries.current.get(path);
      if (!entry || entry.state.status !== "ready") return;
      entry.displayFailures += 1;
      const release = entry.release;
      entry.release = undefined;
      release?.();
      if (entry.displayFailures > 1) entry.state = FAILED;
      else load(path, entry);
      publish();
    },
    [load, publish],
  );

  return {
    media: enabled && published.scope === scope ? published.media : NONE,
    retry,
    request,
    reportDisplayError,
  };
}

function dispose(entry: Entry): void {
  entry.attempt += 1;
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = undefined;
  const release = entry.release;
  entry.release = undefined;
  release?.();
}

/** Errors may opt out of automatic retries with `retryable: false`. */
function isRetryable(error: unknown): boolean {
  return !(
    typeof error === "object" &&
    error !== null &&
    (error as { retryable?: unknown }).retryable === false
  );
}
