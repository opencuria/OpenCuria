import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { AppState } from "react-native";
import { randomUUID } from "expo-crypto";
import type { OpenCuriaClient } from "@opencuria/client";
import { desktopViewerTransport } from "@opencuria/client/desktop";
import {
  createDesktopViewerCoordinator,
  type DesktopViewerCoordinator,
  type DesktopViewerSnapshot,
} from "@opencuria/chat-core/desktopViewer";
import { computerUseRunId } from "@opencuria/chat-core/desktopSurface";

const coordinators = new WeakMap<OpenCuriaClient, DesktopViewerCoordinator>();

/**
 * One viewer identity per signed-in client, shared by every desktop panel.
 * Lease rules are the webapp's (`@opencuria/chat-core/desktopViewer`).
 */
export function desktopViewerCoordinator(
  client: OpenCuriaClient,
): DesktopViewerCoordinator {
  let coordinator = coordinators.get(client);
  if (!coordinator) {
    coordinator = createDesktopViewerCoordinator({
      transport: desktopViewerTransport(() => client.desktop),
      viewerClientId: randomUUID(),
    });
    coordinators.set(client, coordinator);
  }
  return coordinator;
}

export interface DesktopViewerState {
  coordinator: DesktopViewerCoordinator;
  snapshot: DesktopViewerSnapshot;
  /** Computer Use drives the desktop; people only watch. */
  computerUseActive: boolean;
  takeControl: () => Promise<void>;
  takeControlBusy: boolean;
}

/**
 * Holds this client's viewer intent while the panel is mounted, starts the
 * desktop once the panel is shown and follows desktop and Computer Use
 * events of the workspace. The lease ends (after the shared grace period)
 * when the panel unmounts, i.e. when the tools sheet closes.
 */
export function useDesktopViewer(
  client: OpenCuriaClient,
  workspaceId: string,
  active: boolean,
): DesktopViewerState {
  const coordinator = useMemo(() => desktopViewerCoordinator(client), [client]);
  const snapshot = useSyncExternalStore(
    (listener) => coordinator.subscribe(workspaceId, listener),
    () => coordinator.snapshot(workspaceId),
  );
  const [runs, setRuns] = useState<ReadonlySet<string>>(() => new Set());
  const [takeControlBusy, setTakeControlBusy] = useState(false);
  const statusRef = useRef(snapshot.status);

  useEffect(() => {
    const owner = {};
    coordinator.retain(workspaceId, owner);
    return () => {
      void coordinator.release(workspaceId, owner);
    };
  }, [coordinator, workspaceId]);

  useEffect(() => {
    if (active) void coordinator.acquire(workspaceId);
  }, [active, coordinator, workspaceId]);

  // Each status snapshot is authoritative for Computer Use, as on the web.
  useEffect(() => {
    if (!snapshot.status || statusRef.current === snapshot.status) return;
    statusRef.current = snapshot.status;
    setRuns(
      snapshot.status.computer_use_active ? new Set(["active"]) : new Set(),
    );
  }, [snapshot.status]);

  useEffect(() => {
    const realtime = client.realtime;
    const refresh = (event: { workspace_id: string }) => {
      if (event.workspace_id === workspaceId)
        void coordinator.refresh(workspaceId);
    };
    const cleanups = [
      realtime.subscribeWorkspace(workspaceId),
      realtime.on("desktop:started", refresh),
      realtime.on("desktop:stopped", refresh),
      realtime.on("desktop:viewer_released", refresh),
      realtime.on("harness.subtask_started", (event) => {
        if (event.workspace_id !== workspaceId) return;
        const runId = computerUseRunId(event);
        if (runId) setRuns((current) => new Set([...current, runId]));
      }),
      realtime.on("harness.subtask_finished", (event) => {
        if (event.workspace_id !== workspaceId) return;
        const runId = computerUseRunId(event);
        if (!runId) return;
        setRuns((current) => {
          const next = new Set(current);
          next.delete(runId);
          next.delete("active");
          return next;
        });
      }),
      realtime.onReconnect(() => {
        if (coordinator.session(workspaceId).wanted)
          void coordinator.refresh(workspaceId, true);
      }),
    ];
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [client, coordinator, workspaceId]);

  // Timers pause in the background; the lease may have expired meanwhile.
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && coordinator.session(workspaceId).wanted)
        void coordinator.refresh(workspaceId, true);
    });
    return () => subscription.remove();
  }, [coordinator, workspaceId]);

  const takeControl = async () => {
    if (takeControlBusy) return;
    setTakeControlBusy(true);
    const previous = runs;
    setRuns(new Set());
    try {
      await client.desktop.takeControl(workspaceId);
    } catch (error) {
      setRuns(previous);
      throw error;
    } finally {
      setTakeControlBusy(false);
    }
  };

  return {
    coordinator,
    snapshot,
    computerUseActive: runs.size > 0,
    takeControl,
    takeControlBusy,
  };
}
