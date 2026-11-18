import { describe, expect, it } from "vitest";

import {
  chatInputCacheKey,
  createChatInputCache,
  type ChatInputStorage,
} from "../src/chatInputCache";

function memoryStorage(): ChatInputStorage & { items: Map<string, string> } {
  const items = new Map<string, string>();
  return {
    items,
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}

describe("chatInputCacheKey", () => {
  it("keys a draft by workspace, chat and engine", () => {
    expect(chatInputCacheKey({ workspaceId: "w1" })).toBe(
      "chat-input-w1-default",
    );
    expect(
      chatInputCacheKey({
        workspaceId: "w1",
        sessionId: "s1",
        harnessId: "native",
      }),
    ).toBe("chat-input-w1-s1");
    expect(
      chatInputCacheKey({
        workspaceId: "w1",
        sessionId: null,
        harnessId: "claude",
      }),
    ).toBe("chat-input-w1-default-claude");
  });
});

describe("createChatInputCache", () => {
  const newChat = { workspaceId: "w1" };
  const chat = { workspaceId: "w1", sessionId: "s1" };

  it("keeps a draft per chat and deletes blank ones", () => {
    const storage = memoryStorage();
    const cache = createChatInputCache(() => storage);

    cache.save(newChat, "Plan the release");
    cache.save(chat, "Fix the test\n");

    expect(cache.load(newChat)).toBe("Plan the release");
    expect(cache.load(chat)).toBe("Fix the test\n");
    expect(cache.load({ workspaceId: "w2" })).toBe("");

    cache.save(chat, "  \n");
    expect(storage.items.has("chat-input-w1-s1")).toBe(false);
    cache.clear(newChat);
    expect(storage.items.size).toBe(0);
  });

  it("moves a draft along with its chat", () => {
    const storage = memoryStorage();
    const cache = createChatInputCache(() => storage);
    const claude = { ...newChat, harnessId: "claude" as const };
    cache.save(newChat, "old text");

    cache.move(newChat, claude, "current text");

    expect(cache.load(newChat)).toBe("");
    expect(cache.load(claude)).toBe("current text");
    cache.move(claude, newChat, "");
    expect(storage.items.size).toBe(0);
  });

  it("keeps working without storage or when it fails", () => {
    const missing = createChatInputCache(() => null);
    missing.save(chat, "text");
    expect(missing.load(chat)).toBe("");

    const failing = createChatInputCache(() => ({
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    }));
    expect(() => failing.save(chat, "text")).not.toThrow();
    expect(() => failing.move(chat, newChat, "text")).not.toThrow();
    expect(failing.load(chat)).toBe("");
  });
});
