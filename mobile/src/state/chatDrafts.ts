import type { ChatInputStorage } from "@opencuria/chat-core/chatInputCache";
import type { OfflineAccount, SavedDraft } from "../offline/offlineStore";

const DAY_MS = 24 * 60 * 60 * 1000;
/** Drafts kept per account at most; the least recently edited go first. */
export const MAX_SAVED_DRAFTS = 100;
/** A draft that was not edited for this long is deleted. */
export const SAVED_DRAFT_MAX_AGE_MS = 30 * DAY_MS;
/** Edits are written at most this often while typing. */
export const DRAFT_SAVE_DELAY_MS = 500;

export type DraftAccount = Pick<OfflineAccount, "loadDrafts" | "saveDrafts">;

/** Called with the key of a draft that changed in memory. */
export type DraftListener = (key: string) => void;

/**
 * One account's composer drafts. Reads are synchronous so a chat shows its
 * draft at once. The drafts are saved with the account's offline data, which
 * sign out deletes; without an account they are kept in memory only.
 */
export class ChatDraftStore implements ChatInputStorage {
  /** Settles once the drafts saved on this device are loaded. */
  readonly ready: Promise<void>;
  private readonly drafts = new Map<
    string,
    { text: string; editedAt: number }
  >();
  /** Drafts edited before the saved ones were loaded; the edits win. */
  private readonly edited = new Set<string>();
  private loaded = false;
  private dirty = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly listeners = new Set<DraftListener>();

  constructor(
    private readonly account: DraftAccount | null,
    private readonly now: () => number = Date.now,
  ) {
    this.ready = account ? this.load(account) : Promise.resolve();
  }

  getItem(key: string): string | null {
    return this.drafts.get(key)?.text ?? null;
  }

  setItem(key: string, text: string): void {
    this.drafts.set(key, { text, editedAt: this.now() });
    this.changed(key);
  }

  removeItem(key: string): void {
    if (this.drafts.delete(key) || !this.loaded) this.changed(key);
  }

  /**
   * Listen for drafts that change in memory, e.g. edited in the app
   * companion while the same chat shows in OpenCuria. Returns an unsubscribe.
   */
  subscribe(listener: DraftListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Write pending edits now, e.g. when the app goes to the background. */
  async flush(): Promise<void> {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (!this.account) return;
    await this.ready;
    if (!this.dirty) return;
    this.dirty = false;
    try {
      await this.account.saveDrafts(this.live());
    } catch {
      // The drafts stay in memory; the next edit or flush tries again.
      this.dirty = true;
    }
  }

  private changed(key: string): void {
    if (!this.loaded) this.edited.add(key);
    this.dirty = true;
    for (const listener of [...this.listeners]) listener(key);
    if (!this.account || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, DRAFT_SAVE_DELAY_MS);
  }

  private async load(account: DraftAccount): Promise<void> {
    try {
      for (const draft of await account.loadDrafts()) {
        const editedAt = Date.parse(draft.editedAt);
        if (this.edited.has(draft.key) || !Number.isFinite(editedAt)) continue;
        this.drafts.set(draft.key, { text: draft.text, editedAt });
      }
    } catch {
      // Unreadable drafts are gone; new ones are saved over them.
    }
    const kept = new Set(this.live().map((draft) => draft.key));
    for (const key of this.drafts.keys())
      if (!kept.has(key)) {
        this.drafts.delete(key);
        this.dirty = true;
      }
    this.edited.clear();
    this.loaded = true;
    // Expired drafts leave the device too, not only the composer.
    if (this.dirty) void Promise.resolve().then(() => this.flush());
  }

  /** Drafts worth keeping, most recently edited first. */
  private live(): SavedDraft[] {
    const now = this.now();
    return [...this.drafts]
      .filter(
        ([, draft]) =>
          draft.text.trim() && now - draft.editedAt < SAVED_DRAFT_MAX_AGE_MS,
      )
      .sort(([, a], [, b]) => b.editedAt - a.editedAt)
      .slice(0, MAX_SAVED_DRAFTS)
      .map(([key, draft]) => ({
        key,
        text: draft.text,
        editedAt: new Date(draft.editedAt).toISOString(),
      }));
  }
}

const shared = new WeakMap<
  DraftAccount,
  { store: ChatDraftStore; users: number }
>();

/**
 * The draft store of an account, one per account for every React root
 * (OpenCuria and the app companion), so neither saves over the other's
 * edits. Without an account, drafts live in memory per caller. Callers
 * `retainChatDraftStore` it while they show drafts.
 */
export function chatDraftStore(account: DraftAccount | null): ChatDraftStore {
  if (!account) return new ChatDraftStore(null);
  let entry = shared.get(account);
  if (!entry) {
    entry = { store: new ChatDraftStore(account), users: 0 };
    shared.set(account, entry);
  }
  return entry.store;
}

/**
 * Keep an account's shared store while it is used; the returned release
 * flushes it, and the last release forgets it, so the next user loads the
 * drafts saved on the device again.
 */
export function retainChatDraftStore(
  account: DraftAccount | null,
  store: ChatDraftStore,
): () => void {
  const entry = account ? shared.get(account) : undefined;
  if (!account || !entry || entry.store !== store)
    return () => void store.flush();
  entry.users += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    entry.users -= 1;
    void store.flush();
    // A root that renders again right away (React's strict effects) keeps it.
    queueMicrotask(() => {
      if (entry.users === 0 && shared.get(account) === entry)
        shared.delete(account);
    });
  };
}
