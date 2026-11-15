import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listeners = new Set<(state: { isConnected?: boolean }) => void>();
vi.mock("expo-network", () => ({
  addNetworkStateListener: (
    listener: (state: { isConnected?: boolean }) => void,
  ) => {
    listeners.add(listener);
    return { remove: () => listeners.delete(listener) };
  },
}));

const { NETWORK_LOSS_GRACE_MS, isUnreachable, watchNetwork } = await import(
  "./connectivity"
);

describe("isUnreachable", () => {
  it("tells missing answers from answers of the server", () => {
    expect(isUnreachable(new TypeError("Network request failed"))).toBe(true);
    expect(isUnreachable({ status: 0, code: "timeout" })).toBe(true);
    expect(isUnreachable({ status: 503 })).toBe(true);
    expect(isUnreachable({ status: 401 })).toBe(false);
    expect(isUnreachable({ status: 500 })).toBe(false);
  });
});

describe("watchNetwork", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    listeners.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("reports a returning network at once and a lost one once it lasts", () => {
    const changes: boolean[] = [];
    const stop = watchNetwork((connected) => changes.push(connected));
    const emit = (isConnected: boolean) =>
      listeners.forEach((listener) => listener({ isConnected }));

    // A handover: lost and back within the grace period.
    emit(false);
    vi.advanceTimersByTime(NETWORK_LOSS_GRACE_MS - 1);
    emit(true);
    vi.advanceTimersByTime(NETWORK_LOSS_GRACE_MS);
    expect(changes).toEqual([true]);

    emit(false);
    vi.advanceTimersByTime(NETWORK_LOSS_GRACE_MS);
    expect(changes).toEqual([true, false]);

    stop();
    emit(true);
    expect(changes).toEqual([true, false]);
    expect(listeners.size).toBe(0);
  });
});
