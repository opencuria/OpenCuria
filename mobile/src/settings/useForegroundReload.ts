import { useEffect, useRef } from "react";
import { AppState } from "react-native";

/**
 * Reloads when the app returns to the foreground, e.g. after finishing an
 * OAuth connection in the browser.
 */
export function useForegroundReload(reload: () => unknown): void {
  const latest = useRef(reload);
  latest.current = reload;
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") void latest.current();
    });
    return () => subscription.remove();
  }, []);
}
