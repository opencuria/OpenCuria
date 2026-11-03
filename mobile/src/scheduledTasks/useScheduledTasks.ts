import { useCallback, useEffect, useRef, useState } from "react";
import type { OpenCuriaClient, ScheduledTask } from "@opencuria/client";
import { errorText } from "../workspaces/workspaceOperations";

/** Session events precede some ledger writes; let a dispatch settle first. */
const EVENT_SETTLE_MS = 250;

export interface ScheduledTasksState {
  tasks: ScheduledTask[];
  /** The list has been fetched once for this server and organization. */
  loaded: boolean;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  /** Apply a task the server just returned. */
  upsert: (task: ScheduledTask) => void;
  remove: (taskId: string) => void;
}

/** The current time, refreshed periodically so relative labels stay true. */
export function useNow(intervalMs = 30_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function sorted(tasks: ScheduledTask[]): ScheduledTask[] {
  return [...tasks].sort((a, b) => a.next_run_at.localeCompare(b.next_run_at));
}

/**
 * The signed-in user's scheduled tasks for one server and organization. The
 * list follows run and conversation events of the subscribed workspaces and
 * reconnects; a newer request or a local change supersedes older responses.
 */
export function useScheduledTasks(
  client: OpenCuriaClient | null,
  scopeKey: string,
): ScheduledTasksState {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (!client) return;
    const request = ++generation.current;
    setLoading(true);
    try {
      const rows = await client.scheduledTasks.list();
      if (request !== generation.current) return;
      setTasks(sorted(rows));
      setLoaded(true);
      setError(null);
    } catch (cause) {
      if (request === generation.current)
        setError(errorText(cause, "Could not load scheduled tasks."));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    generation.current += 1;
    setTasks([]);
    setLoaded(false);
    setError(null);
    setLoading(false);
    if (!client) return;
    void refresh();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const settle = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void refresh(), EVENT_SETTLE_MS);
    };
    const detach = [
      client.realtime.on("harness.session_status", settle),
      client.realtime.on("harness.conversations_changed", settle),
      client.realtime.onReconnect(settle),
    ];
    return () => {
      generation.current += 1;
      if (timer) clearTimeout(timer);
      detach.forEach((off) => off());
    };
  }, [client, scopeKey, refresh]);

  const upsert = useCallback((task: ScheduledTask) => {
    // A response already in flight predates this change.
    generation.current += 1;
    setLoading(false);
    setTasks((rows) =>
      sorted([...rows.filter((row) => row.id !== task.id), task]),
    );
  }, []);

  const remove = useCallback((taskId: string) => {
    generation.current += 1;
    setLoading(false);
    setTasks((rows) => rows.filter((row) => row.id !== taskId));
  }, []);

  return { tasks, loaded, loading, error, refresh, upsert, remove };
}
