import { act, renderHook } from "@testing-library/react-native";
import { AppState, type AppStateStatus } from "react-native";
import type { OfflineStore } from "./offlineStore";
import {
  CLEANUP_DELAY_MS,
  CLEANUP_INTERVAL_MS,
  useSavedChatCleanup,
} from "./useSavedChatCleanup";

const SCOPE = "https://curia.test/api/v1|org|7";

describe("useSavedChatCleanup", () => {
  let changes: Array<(state: AppStateStatus) => void>;
  const remove = jest.fn();

  beforeEach(() => {
    jest.useFakeTimers();
    changes = [];
    jest
      .spyOn(AppState, "addEventListener")
      .mockImplementation((_event, listener) => {
        changes.push(listener as (state: AppStateStatus) => void);
        return { remove } as never;
      });
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("deletes expired chats shortly after opening and on later returns", () => {
    const store = {
      cleanUp: jest.fn().mockResolvedValue(undefined),
    } as unknown as OfflineStore;
    const { unmount } = renderHook(() => useSavedChatCleanup(SCOPE, store));

    expect(store.cleanUp).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(CLEANUP_DELAY_MS));
    expect(store.cleanUp).toHaveBeenCalledWith(SCOPE);

    // Coming back soon does not clean up again; hours later it does.
    act(() => changes.forEach((change) => change("active")));
    expect(store.cleanUp).toHaveBeenCalledTimes(1);
    act(() => jest.setSystemTime(Date.now() + CLEANUP_INTERVAL_MS));
    act(() => changes.forEach((change) => change("active")));
    expect(store.cleanUp).toHaveBeenCalledTimes(2);

    unmount();
    expect(remove).toHaveBeenCalled();
  });

  it("waits for an account", () => {
    const store = { cleanUp: jest.fn() } as unknown as OfflineStore;
    renderHook(() => useSavedChatCleanup(null, store));
    act(() => jest.advanceTimersByTime(CLEANUP_DELAY_MS));
    expect(store.cleanUp).not.toHaveBeenCalled();
  });
});
