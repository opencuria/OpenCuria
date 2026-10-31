import { useEffect, useId, useRef, useSyncExternalStore } from "react";

export type ToastTone = "success" | "info" | "warning" | "error";

export interface ToastOptions {
  title: string;
  message?: string;
  tone?: ToastTone;
  /** Milliseconds until it hides on its own; defaults by tone. */
  duration?: number;
}

export interface Toast {
  id: number;
  title: string;
  message?: string;
  tone: ToastTone;
  /** Milliseconds it stays once a host shows it. */
  duration: number;
}

/** Confirmations pass quickly; problems stay long enough to be read. */
const DURATION: Record<ToastTone, number> = {
  success: 2600,
  info: 2600,
  warning: 5000,
  error: 6000,
};

let current: Toast | null = null;
let nextId = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let armedId: number | null = null;
/**
 * Mounted hosts in the order they first mounted: the newest belongs to the
 * topmost screen or sheet. A host that re-subscribes (Fast Refresh) keeps
 * its place.
 */
let hosts: { key: string; order: number }[] = [];
let hostOrder = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Brief feedback for a finished action, shown at the top of whichever screen
 * or sheet is in front. A new toast replaces the visible one. Returns its id.
 */
export function showToast({
  title,
  message,
  tone = "info",
  duration,
}: ToastOptions): number {
  if (timer) clearTimeout(timer);
  timer = null;
  armedId = null;
  const id = ++nextId;
  current = {
    id,
    title,
    message: message || undefined,
    tone,
    duration: duration ?? DURATION[tone],
  };
  emit();
  return id;
}

/**
 * Starts a toast's time once a host shows it, so a busy moment between the
 * action and the first frame never shortens it.
 */
export function armToast(id: number): void {
  if (!current || current.id !== id || armedId === id) return;
  armedId = id;
  timer = setTimeout(() => dismissToast(id), current.duration);
}

/** Hides the toast (only the given one, when an id is passed). */
export function dismissToast(id?: number): void {
  if (!current || (id !== undefined && current.id !== id)) return;
  if (timer) clearTimeout(timer);
  timer = null;
  armedId = null;
  current = null;
  emit();
}

/** The visible toast, for tests and accessibility announcements. */
export function currentToast(): Toast | null {
  return current;
}

/**
 * Registers a toast host and returns the toast it should show: only the most
 * recently mounted host (the screen or sheet in front) shows it.
 */
export function useToastHost(): Toast | null {
  const key = useId();
  const order = useRef(0);
  if (!order.current) order.current = ++hostOrder;
  useEffect(() => {
    hosts = [
      ...hosts.filter((host) => host.key !== key),
      { key, order: order.current },
    ].sort((a, b) => a.order - b.order);
    emit();
    return () => {
      hosts = hosts.filter((host) => host.key !== key);
      emit();
    };
  }, [key]);
  return useSyncExternalStore(
    subscribe,
    () => (hosts[hosts.length - 1]?.key === key ? current : null),
    () => null,
  );
}
