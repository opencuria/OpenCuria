import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/** A workspace media file URI, optionally held until `release()`. */
export type ResolvedChatMedia = string | { uri: string; release: () => void };

export type WorkspaceMediaState =
  | { status: "loading" }
  | { status: "ready"; uri: string }
  | { status: "failed" };

/**
 * Delays before automatic retries of a failed read. Reads fail transiently
 * while the socket (re)connects, a transfer times out or a file is still
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
  resolve?: (path: string) => Promise<ResolvedChatMedia>;
  scope: string;
  active: boolean;
}): {
  media: Record<string, WorkspaceMediaState>;
  /** Read a failed path again. */
  retry: (path: string) => void;
  /** The view could not show a ready file (e.g. it was evicted): read again once. */
  reportDisplayError: (path: string) => void;
} {
  const resolveRef = useRef(resolve);
  resolveRef.current = resolve;
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
      entry.state = LOADING;
      const current = () =>
        entries.current.get(path) === entry && entry.attempt === attempt;
      void Promise.resolve()
        .then(() => {
          const resolver = resolveRef.current;
          if (!resolver) throw new Error("Media resolver unavailable.");
          return resolver(path);
        })
        .then((resolved) => {
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
        })
        .catch((error: unknown) => {
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
