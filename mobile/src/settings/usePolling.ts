import { useEffect, useRef } from "react";

/**
 * Calls `poll` every `intervalMs` while `enabled` and the page is mounted;
 * a slow request delays the next one instead of overlapping it.
 */
export function usePolling(
  poll: () => Promise<unknown> | unknown,
  intervalMs: number,
  enabled = true,
): void {
  const latest = useRef(poll);
  latest.current = poll;
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let active = true;
    const schedule = () => {
      timer = setTimeout(async () => {
        try {
          await latest.current();
        } catch {
          /* The page shows its own load errors. */
        }
        if (active) schedule();
      }, intervalMs);
    };
    schedule();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs, enabled]);
}
