import { useEffect } from "react";
import { AppState } from "react-native";
import { offlineStore, type OfflineStore } from "./offlineStore";

/** The first clean-up waits until OpenCuria has settled after opening. */
export const CLEANUP_DELAY_MS = 5_000;
/** Later clean-ups run when OpenCuria comes back, at most this often. */
export const CLEANUP_INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * Deletes saved chats in the background that were not opened for a week,
 * shortly after OpenCuria opens and when it returns to the foreground.
 * `scope` is the signed-in account, whose profile stays for offline use.
 */
export function useSavedChatCleanup(
  scope: string | null,
  store: OfflineStore = offlineStore(),
): void {
  useEffect(() => {
    if (!scope) return;
    let last = 0;
    const run = () => {
      if (Date.now() - last < CLEANUP_INTERVAL_MS) return;
      last = Date.now();
      void store.cleanUp(scope).catch(() => undefined);
    };
    const timer = setTimeout(run, CLEANUP_DELAY_MS);
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") run();
    });
    return () => {
      clearTimeout(timer);
      subscription.remove();
    };
  }, [scope, store]);
}
