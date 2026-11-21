/**
 * Composer drafts shared by the web and native composers.
 *
 * Each chat keeps the text typed but not sent yet, and so does each
 * workspace's new chat; drafts of different engines are kept apart. Clients
 * supply the storage: the web uses the tab's `sessionStorage`, the app a store
 * saved on the device.
 */

import type { HarnessId } from "./harness";

/** The chat a draft belongs to. */
export interface ChatInputScope {
  workspaceId: string;
  /** None for the workspace's new chat. */
  sessionId?: string | null;
  /** None for the native agent. */
  harnessId?: HarnessId | null;
}

/** Synchronous string storage such as `sessionStorage`. */
export interface ChatInputStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** Drafts of one storage. */
export interface ChatInputCache {
  /** The scope's draft; empty when it has none. */
  load(scope: ChatInputScope): string;
  /** Keep `text` as the scope's draft; blank text deletes it. */
  save(scope: ChatInputScope, text: string): void;
  clear(scope: ChatInputScope): void;
  /** The draft follows its chat, e.g. to another engine: `text` leaves `from` for `to`. */
  move(from: ChatInputScope, to: ChatInputScope, text: string): void;
}

/** Storage key of a draft; native drafts keep the key from before engines. */
export function chatInputCacheKey(scope: ChatInputScope): string {
  const chat = scope.sessionId || "default";
  const engine = scope.harnessId || "native";
  const suffix = engine === "native" ? "" : `-${engine}`;
  return `chat-input-${scope.workspaceId}-${chat}${suffix}`;
}

/**
 * Drafts in the storage `storage()` returns at each access; none keeps no
 * drafts. A storage failure, such as a full quota, never interrupts typing
 * or sending.
 */
export function createChatInputCache(
  storage: () => ChatInputStorage | null,
): ChatInputCache {
  function attempt<T>(fallback: T, use: (store: ChatInputStorage) => T): T {
    try {
      const store = storage();
      return store ? use(store) : fallback;
    } catch {
      return fallback;
    }
  }
  const save = (scope: ChatInputScope, text: string): void =>
    attempt(undefined, (store) => {
      const key = chatInputCacheKey(scope);
      if (text.trim()) store.setItem(key, text);
      else store.removeItem(key);
    });
  const clear = (scope: ChatInputScope): void =>
    attempt(undefined, (store) => store.removeItem(chatInputCacheKey(scope)));
  return {
    load: (scope) =>
      attempt("", (store) => store.getItem(chatInputCacheKey(scope)) ?? ""),
    save,
    clear,
    move: (from, to, text) => {
      clear(from);
      save(to, text);
    },
  };
}
