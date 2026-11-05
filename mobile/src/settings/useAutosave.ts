import { useCallback, useEffect, useRef, useState } from "react";
import { showToast } from "../ui/toast";
import { errorMessage } from "./resources";

export type AutosaveStatus =
  | "saved"
  | "waiting"
  | "saving"
  | "error"
  | "invalid";

export interface Autosave {
  status: AutosaveStatus;
  error: string | null;
  /** Saves the current value again after a failure. */
  retry: () => void;
}

/**
 * Saves a settings value shortly after it changes, as the webapp does: one
 * write at a time, newer edits saved after it, nothing saved while invalid,
 * and a pending change still saved when the page closes. The first value
 * seen is the saved baseline.
 */
export function useAutosave<T>(
  value: T | null | undefined,
  save: (value: T) => Promise<void>,
  {
    valid = true,
    enabled = true,
    delay = 500,
    failure = "Could not save these settings",
  }: {
    valid?: boolean;
    enabled?: boolean;
    delay?: number;
    /** Toast title when a save fails. */
    failure?: string;
  } = {},
): Autosave {
  const saved = useRef<string | null>(null);
  const saving = useRef(false);
  const latest = useRef({ value, save, valid, enabled });
  latest.current = { value, save, valid, enabled };
  const [state, setState] = useState<{
    saving: boolean;
    error: string | null;
    revision: number;
  }>({ saving: false, error: null, revision: 0 });

  const key = value == null ? null : JSON.stringify(value);
  if (saved.current === null && key !== null) saved.current = key;
  const dirty = key !== null && key !== saved.current;

  const write = useCallback(async () => {
    const {
      value: current,
      save: persist,
      valid: ok,
      enabled: on,
    } = latest.current;
    if (current == null || !ok || !on || saving.current) return;
    const snapshot = JSON.stringify(current);
    if (snapshot === saved.current) return;
    saving.current = true;
    setState((previous) => ({ ...previous, saving: true, error: null }));
    try {
      await persist(current);
      saved.current = snapshot;
      setState((previous) => ({
        saving: false,
        error: null,
        revision: previous.revision + 1,
      }));
    } catch (cause) {
      const message = errorMessage(cause, failure);
      showToast({ title: failure, message, tone: "error" });
      setState((previous) => ({ ...previous, saving: false, error: message }));
    } finally {
      saving.current = false;
    }
  }, [failure]);

  // Debounce edits; a newer edit made during a save is written afterwards.
  useEffect(() => {
    if (!dirty || !valid || !enabled || state.saving || state.error) return;
    const timer = setTimeout(() => void write(), delay);
    return () => clearTimeout(timer);
  }, [key, dirty, valid, enabled, delay, state.saving, state.error, write]);

  // Leaving the page saves a pending edit unless the last attempt failed.
  const failed = useRef(false);
  failed.current = Boolean(state.error);
  useEffect(
    () => () => {
      if (!failed.current) void write();
    },
    [write],
  );

  const retry = useCallback(() => {
    setState((previous) => ({ ...previous, error: null }));
    void write();
  }, [write]);

  const status: AutosaveStatus = state.saving
    ? "saving"
    : state.error
      ? "error"
      : dirty
        ? valid
          ? "waiting"
          : "invalid"
        : "saved";
  return { status, error: state.error, retry };
}

/** Footer text for a section that saves automatically. */
export function autosaveText(autosave: Autosave): string {
  switch (autosave.status) {
    case "saving":
      return "Saving…";
    case "waiting":
      return "Waiting to save…";
    case "invalid":
      return "Check your input";
    case "error":
      return `Changes not saved: ${autosave.error}`;
    default:
      return "All changes saved";
  }
}
