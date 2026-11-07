import * as Network from "expo-network";

/** Answers that mean the server itself is down behind a reachable proxy. */
const GATEWAY_FAILURES = new Set([502, 503, 504]);

/**
 * Whether a failed request means OpenCuria could not be reached (no answer,
 * a timeout or an unavailable gateway) rather than that it answered.
 */
export function isUnreachable(error: unknown): boolean {
  const status =
    error && typeof error === "object" && "status" in error
      ? (error as { status?: unknown }).status
      : undefined;
  if (typeof status !== "number") return true;
  return status === 0 || GATEWAY_FAILURES.has(status);
}

/** A handover between networks (Wi-Fi to cellular) is not a lost connection. */
export const NETWORK_LOSS_GRACE_MS = 2_000;

/**
 * Calls `listener` when the device gets or loses a network connection, as
 * the system reports it; nothing is sent anywhere to find out. A loss is
 * reported once it lasted `NETWORK_LOSS_GRACE_MS`.
 */
export function watchNetwork(
  listener: (connected: boolean) => void,
): () => void {
  let lost: ReturnType<typeof setTimeout> | undefined;
  try {
    const subscription = Network.addNetworkStateListener((state) => {
      if (lost) clearTimeout(lost);
      lost = undefined;
      if (state.isConnected !== false) listener(true);
      else
        lost = setTimeout(() => {
          lost = undefined;
          listener(false);
        }, NETWORK_LOSS_GRACE_MS);
    });
    return () => {
      if (lost) clearTimeout(lost);
      subscription.remove();
    };
  } catch {
    // Without the native module, reconnecting relies on retries and resumes.
    return () => undefined;
  }
}
