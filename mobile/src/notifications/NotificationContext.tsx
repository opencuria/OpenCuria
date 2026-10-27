import React, {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
} from "react";
import { useSession } from "../screens/SessionContext";
import { NotificationController, type NotificationState } from "./controller";
import { nativeNotifications } from "./native";
import { liveActivities } from "./liveActivity";
import { NotificationStorage } from "./storage";

const empty: NotificationState = {
  ready: false,
  busy: false,
  notifications: false,
  liveActivities: false,
  permissionGranted: false,
  capabilities: null,
  error: null,
  target: null,
};
const Context = createContext<{
  controller: NotificationController | null;
  state: NotificationState;
}>({ controller: null, state: empty });

/** Keep the native lifecycle above screens so cold-start notification taps survive hydration. */
export function NotificationProvider({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { client, session, user, registerResource, registerSignOutCleanup } =
    useSession();
  const scope = `${session?.serverUrl ?? ""}:${user?.id ?? ""}:${session?.organizationId ?? ""}`;
  const controller = useMemo(() => {
    if (!client || !user || !session?.organizationId) return null;
    return new NotificationController(
      client,
      session.organizationId,
      new NotificationStorage(scope),
      nativeNotifications,
      liveActivities,
    );
  }, [client, user?.id, session?.organizationId, scope]);
  const state = useSyncExternalStore(
    controller?.subscribe ?? (() => () => undefined),
    controller?.getSnapshot ?? (() => empty),
    () => empty,
  );
  const mounts = useRef(new WeakMap<NotificationController, number>());
  useEffect(() => {
    if (!controller) return;
    mounts.current.set(controller, (mounts.current.get(controller) ?? 0) + 1);
    const unregisterResource = registerResource(() => controller.dispose());
    const unregisterCleanup = registerSignOutCleanup(() => controller.revoke());
    void controller.start();
    return () => {
      unregisterCleanup();
      unregisterResource();
      mounts.current.set(controller, (mounts.current.get(controller) ?? 1) - 1);
      queueMicrotask(() => {
        if (!mounts.current.get(controller)) controller.dispose();
      });
    };
  }, [controller, registerResource, registerSignOutCleanup]);
  return (
    <Context.Provider value={{ controller, state }}>
      {children}
    </Context.Provider>
  );
}

export function useNotifications() {
  return useContext(Context);
}
