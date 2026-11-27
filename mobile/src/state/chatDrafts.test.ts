import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SavedDraft } from "../offline/offlineStore";
import {
  ChatDraftStore,
  DRAFT_SAVE_DELAY_MS,
  MAX_SAVED_DRAFTS,
  SAVED_DRAFT_MAX_AGE_MS,
  chatDraftStore,
  retainChatDraftStore,
} from "./chatDrafts";

const NOW = Date.parse("2026-11-18T12:00:00Z");

function account(saved: SavedDraft[] = []) {
  let drafts = saved;
  return {
    loadDrafts: vi.fn(async () => drafts),
    saveDrafts: vi.fn(async (next: SavedDraft[]) => {
      drafts = next;
    }),
    get saved() {
      return drafts;
    },
  };
}

function draft(key: string, text: string, age = 0): SavedDraft {
  return { key, text, editedAt: new Date(NOW - age).toISOString() };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ChatDraftStore", () => {
  it("reads at once and saves edits shortly after typing", async () => {
    const device = account();
    const store = new ChatDraftStore(device);
    await store.ready;

    store.setItem("chat-input-w1-s1", "Fix the");
    store.setItem("chat-input-w1-s1", "Fix the test");
    expect(store.getItem("chat-input-w1-s1")).toBe("Fix the test");
    expect(device.saveDrafts).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DELAY_MS);
    expect(device.saveDrafts).toHaveBeenCalledTimes(1);
    expect(device.saved).toEqual([draft("chat-input-w1-s1", "Fix the test")]);

    store.removeItem("chat-input-w1-s1");
    await store.flush();
    expect(device.saved).toEqual([]);

    const reopened = new ChatDraftStore(account([draft("k", "kept")]));
    await reopened.ready;
    expect(reopened.getItem("k")).toBe("kept");
  });

  it("keeps edits made before the saved drafts were loaded", async () => {
    const device = account([draft("typed", "old"), draft("sent", "old")]);
    const store = new ChatDraftStore(device);
    store.setItem("typed", "new");
    store.removeItem("sent");
    await store.ready;
    await store.flush();

    expect(store.getItem("typed")).toBe("new");
    expect(store.getItem("sent")).toBeNull();
    expect(device.saved).toEqual([draft("typed", "new")]);
  });

  it("deletes drafts not edited for a month and beyond the limit", async () => {
    const saved = Array.from({ length: MAX_SAVED_DRAFTS + 1 }, (_, index) =>
      draft(`k${index}`, "text", index * 1000),
    );
    saved.push(draft("old", "text", SAVED_DRAFT_MAX_AGE_MS));
    const device = account(saved);
    const store = new ChatDraftStore(device);
    await store.ready;
    await vi.runAllTimersAsync();

    expect(store.getItem("k0")).toBe("text");
    expect(store.getItem(`k${MAX_SAVED_DRAFTS}`)).toBeNull();
    expect(store.getItem("old")).toBeNull();
    expect(device.saved).toHaveLength(MAX_SAVED_DRAFTS);
  });

  it("keeps working when saved drafts cannot be read or written", async () => {
    const device = account();
    device.loadDrafts.mockRejectedValueOnce(new Error("unreadable"));
    device.saveDrafts.mockRejectedValueOnce(new Error("disk full"));
    const store = new ChatDraftStore(device);
    await store.ready;

    store.setItem("k", "text");
    await store.flush();
    expect(store.getItem("k")).toBe("text");
    expect(device.saved).toEqual([]);

    await store.flush();
    expect(device.saved).toEqual([draft("k", "text")]);
  });

  it("keeps drafts in memory without an account", async () => {
    const store = new ChatDraftStore(null);
    store.setItem("k", "text");
    await store.flush();
    expect(store.getItem("k")).toBe("text");
  });
});

describe("chatDraftStore", () => {
  it("gives every root of an account one store and tells them of edits", async () => {
    const device = account();
    const main = chatDraftStore(device);
    const companion = chatDraftStore(device);
    expect(companion).toBe(main);
    const releaseMain = retainChatDraftStore(device, main);
    const releaseCompanion = retainChatDraftStore(device, companion);
    const changed = vi.fn();
    const unsubscribe = main.subscribe(changed);
    await main.ready;

    companion.setItem("chat-input-w1-s1", "From the app");
    expect(changed).toHaveBeenCalledWith("chat-input-w1-s1");
    expect(main.getItem("chat-input-w1-s1")).toBe("From the app");
    unsubscribe();

    // The last release saves and forgets the store; the next user loads again.
    releaseCompanion();
    await Promise.resolve();
    expect(chatDraftStore(device)).toBe(main);
    releaseMain();
    await vi.advanceTimersByTimeAsync(0);
    expect(device.saved.map((item) => item.text)).toEqual(["From the app"]);
    expect(chatDraftStore(device)).not.toBe(main);
  });

  it("keeps drafts without an account apart per caller", () => {
    expect(chatDraftStore(null)).not.toBe(chatDraftStore(null));
  });
});
