import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

export interface ResourceState<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  /** Changed on the server; mounted pages reload it. */
  stale?: boolean;
}

interface Entry<T> extends ResourceState<T> {
  flight: Promise<void> | null;
  generation: number;
}

const IDLE: ResourceState<never> = {
  data: undefined,
  error: null,
  loading: false,
};

/**
 * Server data shared by the settings pages of one session, keyed by name:
 * a list page and the detail page pushed above it read and update the same
 * entry, so a save shows on both without reloading.
 */
export class ResourceStore {
  private entries = new Map<string, Entry<unknown>>();
  private listeners = new Map<string, Set<() => void>>();

  get<T>(key: string): ResourceState<T> {
    return (this.entries.get(key) as Entry<T> | undefined) ?? IDLE;
  }

  subscribe(key: string, listener: () => void): () => void {
    const set = this.listeners.get(key) ?? new Set();
    set.add(listener);
    this.listeners.set(key, set);
    return () => set.delete(listener);
  }

  /** Loads once; `force` reloads even when data is present. */
  load<T>(key: string, loader: () => Promise<T>, force = false): Promise<void> {
    const current = this.entries.get(key) as Entry<T> | undefined;
    if (current?.flight) return current.flight;
    if (
      current &&
      !force &&
      !current.stale &&
      (current.data !== undefined || current.error)
    )
      return Promise.resolve();
    const generation = (current?.generation ?? 0) + 1;
    const entry: Entry<T> = {
      data: current?.data,
      error: null,
      loading: true,
      flight: null,
      generation,
    };
    entry.flight = loader().then(
      (data) => this.settle(key, generation, { data, error: null }),
      (cause: unknown) =>
        this.settle(key, generation, {
          data: current?.data,
          error: errorMessage(cause, "Could not load these settings."),
        }),
    );
    this.write(key, entry);
    return entry.flight;
  }

  /**
   * Marks every entry whose key starts with `prefix` as changed, e.g. after
   * a detail page deleted an item; mounted pages reload, others on next use.
   */
  invalidate(prefix: string): void {
    for (const [key, entry] of this.entries)
      if (key.startsWith(prefix)) this.write(key, { ...entry, stale: true });
  }

  /** Replaces the data, e.g. with the server's response to a save. */
  set<T>(key: string, update: T | ((previous: T | undefined) => T)): void {
    const current = this.entries.get(key) as Entry<T> | undefined;
    const data =
      typeof update === "function"
        ? (update as (previous: T | undefined) => T)(current?.data)
        : update;
    this.write(key, {
      data,
      error: null,
      loading: current?.loading ?? false,
      flight: current?.flight ?? null,
      // A load already in flight must not overwrite this newer value.
      generation: (current?.generation ?? 0) + 1,
    });
  }

  private settle<T>(
    key: string,
    generation: number,
    result: { data: T | undefined; error: string | null },
  ) {
    const current = this.entries.get(key);
    if (!current || current.generation !== generation) {
      if (current?.flight)
        this.write(key, { ...current, flight: null, loading: false });
      return;
    }
    this.write(key, { ...result, loading: false, flight: null, generation });
  }

  private write(key: string, entry: Entry<unknown>) {
    this.entries.set(key, entry);
    this.listeners.get(key)?.forEach((listener) => listener());
  }
}

export interface Resource<T> extends ResourceState<T> {
  reload: () => Promise<void>;
  set: (update: T | ((previous: T | undefined) => T)) => void;
}

/** Subscribes to a shared resource and loads it on first use. */
export function useResource<T>(
  store: ResourceStore,
  key: string,
  loader: () => Promise<T>,
  options: { enabled?: boolean } = {},
): Resource<T> {
  const enabled = options.enabled ?? true;
  const subscribe = useCallback(
    (listener: () => void) => store.subscribe(key, listener),
    [store, key],
  );
  const snapshot = useCallback(() => store.get<T>(key), [store, key]);
  const state = useSyncExternalStore(subscribe, snapshot, snapshot);
  // The loader changes every render; the key names the data it loads.
  const latest = useRef(loader);
  latest.current = loader;
  useEffect(() => {
    if (enabled) void store.load(key, () => latest.current());
  }, [store, key, enabled, state.stale]);
  const reload = useCallback(
    () => store.load(key, () => latest.current(), true),
    [store, key],
  );
  const set = useCallback(
    (update: T | ((previous: T | undefined) => T)) => store.set(key, update),
    [store, key],
  );
  return { ...state, reload, set };
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
