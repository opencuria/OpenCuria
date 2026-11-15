import { CryptoDigestAlgorithm, digestStringAsync } from "expo-crypto";
import type {
  HarnessMessage,
  HarnessSession,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import {
  classifyWorkspaceFile,
  getWorkspaceFileExtension,
} from "@opencuria/chat-core/workspaceFileRefs";
import type {
  AuthUser,
  HarnessConversation,
  Organization,
  Workspace,
} from "@opencuria/client";
import {
  createLocalFiles,
  joinUri,
  temporaryUri,
  type LocalFiles,
} from "../platform/localFiles";

const DAY_MS = 24 * 60 * 60 * 1000;
/** A saved chat is deleted when it was not opened on this device for this long. */
export const SAVED_CHAT_MAX_AGE_MS = 7 * DAY_MS;
/** Chats kept per account at most; the least recently opened go first. */
export const MAX_SAVED_CHATS = 100;
/** Bytes of pictures kept per chat at most. */
export const MAX_CHAT_MEDIA_BYTES = 64 * 1024 * 1024;

/** Format of the files below; others are ignored (and removed by `cleanUp`). */
const VERSION = 1;
const PROFILE = "profile.json";
const OVERVIEW = "overview.json";
const INDEX = "chats.json";
const CHATS = "chats";
const TRANSCRIPT = "transcript.json";

export type OfflineFiles = Pick<
  LocalFiles,
  | "root"
  | "exists"
  | "readText"
  | "writeText"
  | "makeDirectory"
  | "remove"
  | "list"
  | "copy"
  | "size"
>;

/** The signed-in account as it was last loaded, so OpenCuria opens offline. */
export interface SavedProfile {
  user: AuthUser;
  organizations: Organization[];
  savedAt: string;
}

/** The workspaces and the chat list as they were last loaded. */
export interface SavedOverview {
  workspaces: Workspace[];
  conversations: HarnessConversation[];
  savedAt: string;
}

/** A chat saved on this device. */
export interface SavedChat {
  sessionId: string;
  workspaceId: string;
  /** Its row in the chat list; subtasks have none and open from their parent. */
  conversation: HarnessConversation | null;
  /** When it was last opened here; a week later it is deleted. */
  openedAt: string;
  savedAt: string;
}

/** A saved chat's content as it was last read. */
export interface SavedTranscript {
  session: HarnessSession;
  /** Sessions of the chat's workspace, for its parents and subtasks. */
  sessions: HarnessSession[];
  messages: HarnessMessage[];
  todos: HarnessTodo[];
}

interface ChatEntry extends SavedChat {
  /** Name of the chat's directory. */
  key: string;
}

interface Stored {
  version: number;
  scope: string;
}

type Hash = (value: string) => Promise<string>;

/**
 * Hex SHA-256 of `value`. Inputs are joined with `|`, never control
 * characters: the native bridge ends strings at a NUL character, so every
 * input would hash alike.
 */
const sha256: Hash = (value) =>
  digestStringAsync(CryptoDigestAlgorithm.SHA256, value);

function isEntry(value: unknown): value is ChatEntry {
  const entry = value as Partial<ChatEntry> | null;
  return Boolean(
    entry &&
    typeof entry.sessionId === "string" &&
    typeof entry.workspaceId === "string" &&
    typeof entry.openedAt === "string" &&
    typeof entry.savedAt === "string" &&
    typeof entry.key === "string" &&
    /^[0-9a-f]{32}$/.test(entry.key),
  );
}

function time(value: string | undefined): number {
  const parsed = Date.parse(value ?? "");
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Chats that have not expired, most recently opened first, as many as kept. */
function liveChats(chats: ChatEntry[], now: number): ChatEntry[] {
  return chats
    .filter((chat) => now - time(chat.openedAt) < SAVED_CHAT_MAX_AGE_MS)
    .sort((a, b) => time(b.openedAt) - time(a.openedAt))
    .slice(0, MAX_SAVED_CHATS);
}

/** Pictures keep their type; anything unusual is stored without one. */
function mediaExtension(path: string): string {
  const extension = getWorkspaceFileExtension(path);
  return /^[a-z0-9]{1,12}$/.test(extension) ? `.${extension}` : "";
}

/**
 * Chats, pictures and lists saved on this device so OpenCuria opens offline.
 * They live in the app's cache: the server has them, so they are never
 * backed up. Each account (server, organization and user) has its own
 * directory; file and directory names are hashes, never identifiers. Writes
 * of all accounts run one at a time and replace files only once their
 * content is complete.
 */
export class OfflineStore {
  private readonly accounts = new Map<string, OfflineAccount>();
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly files: OfflineFiles,
    private readonly hash: Hash = sha256,
    private readonly now: () => Date = () => new Date(),
  ) {}

  /** What is saved for one account (`appScope`). */
  account(scope: string): OfflineAccount {
    let account = this.accounts.get(scope);
    if (!account) {
      account = new OfflineAccount(
        scope,
        this.files,
        this.hash,
        this.now,
        this.serialized.bind(this),
      );
      this.accounts.set(scope, account);
    }
    return account;
  }

  /**
   * Delete everything saved, e.g. on sign out. Saves still on their way,
   * such as those of a screen that is closing, are dropped.
   */
  clear(): Promise<void> {
    for (const account of this.accounts.values()) account.revoke();
    this.accounts.clear();
    return this.serialized(async () => {
      this.files.remove(this.files.root);
    });
  }

  /**
   * Delete chats that were not opened for a week (with their pictures),
   * chats beyond the limit and leftovers of interrupted saves. Accounts other
   * than `keep` go entirely once nothing of theirs was used for a week.
   */
  cleanUp(keep: string | null): Promise<void> {
    return this.serialized(async () => {
      const kept = keep ? await this.account(keep).key() : null;
      const now = this.now().getTime();
      for (const name of this.files.list(this.files.root)) {
        const directory = joinUri(this.files.root, name);
        const [profile, overview, index] = await Promise.all([
          readStored<{ savedAt?: string }>(this.files, directory, PROFILE),
          readStored<{ savedAt?: string }>(this.files, directory, OVERVIEW),
          readStored<{ chats?: unknown }>(this.files, directory, INDEX),
        ]);
        const chats = Array.isArray(index?.chats)
          ? index.chats.filter(isEntry)
          : [];
        const live = liveChats(chats, now);
        const scope = profile?.scope ?? overview?.scope ?? index?.scope;
        const used = Math.max(
          time(profile?.savedAt),
          time(overview?.savedAt),
          ...live.map((chat) => time(chat.openedAt)),
        );
        if (
          name !== kept &&
          (!scope || (!live.length && now - used >= SAVED_CHAT_MAX_AGE_MS))
        ) {
          this.files.remove(directory);
          if (scope) {
            this.accounts.get(scope)?.revoke();
            this.accounts.delete(scope);
          }
          continue;
        }
        if (scope && live.length !== chats.length)
          writeStored(this.files, directory, INDEX, scope, { chats: live });
        const keys = new Set(live.map((chat) => chat.key));
        const chatsDirectory = joinUri(directory, CHATS);
        for (const chat of this.files.list(chatsDirectory))
          if (!keys.has(chat)) this.files.remove(joinUri(chatsDirectory, chat));
      }
    });
  }

  private serialized<T>(task: () => Promise<T>): Promise<T> {
    const next = this.queue.then(task, task);
    this.queue = next.catch(() => undefined);
    return next;
  }
}

/** One account's saved profile, lists, chats and chat pictures. */
export class OfflineAccount {
  private directoryKey: Promise<string> | null = null;
  /** Content last written per file, so unchanged lists are not written again. */
  private readonly written = new Map<string, string>();
  /** Its data was deleted: later saves are dropped instead of restoring it. */
  private revoked = false;

  constructor(
    readonly scope: string,
    private readonly files: OfflineFiles,
    private readonly hash: Hash,
    private readonly now: () => Date,
    private readonly serialized: <T>(task: () => Promise<T>) => Promise<T>,
  ) {}

  /** Called by the store when it deletes this account's data. */
  revoke(): void {
    this.revoked = true;
  }

  /** Name of the account's directory. */
  key(): Promise<string> {
    this.directoryKey ??= this.hash(`account|${this.scope}`).then((digest) =>
      digest.slice(0, 32),
    );
    return this.directoryKey;
  }

  async loadProfile(): Promise<SavedProfile | null> {
    const stored = await this.read<SavedProfile>(PROFILE);
    return stored && stored.user && Array.isArray(stored.organizations)
      ? {
          user: stored.user,
          organizations: stored.organizations,
          savedAt: stored.savedAt,
        }
      : null;
  }

  saveProfile(user: AuthUser, organizations: Organization[]): Promise<void> {
    return this.write(PROFILE, { user, organizations });
  }

  async loadOverview(): Promise<SavedOverview | null> {
    const stored = await this.read<SavedOverview>(OVERVIEW);
    return stored &&
      Array.isArray(stored.workspaces) &&
      Array.isArray(stored.conversations)
      ? {
          workspaces: stored.workspaces,
          conversations: stored.conversations,
          savedAt: stored.savedAt,
        }
      : null;
  }

  saveOverview(
    workspaces: Workspace[],
    conversations: HarnessConversation[],
  ): Promise<void> {
    return this.write(OVERVIEW, { workspaces, conversations });
  }

  /** Saved chats that have not expired, most recently opened first. */
  async chats(): Promise<SavedChat[]> {
    const entries = liveChats(await this.index(), this.now().getTime());
    return entries.map(({ key: _key, ...chat }) => chat);
  }

  /** A saved chat's content; null when it was never saved or has expired. */
  async loadChat(sessionId: string): Promise<SavedTranscript | null> {
    const entry = liveChats(await this.index(), this.now().getTime()).find(
      (chat) => chat.sessionId === sessionId,
    );
    if (!entry) return null;
    const stored = await this.read<{
      sessionId?: string;
      transcript?: SavedTranscript;
    }>(joinUri(CHATS, entry.key, TRANSCRIPT));
    const transcript = stored?.transcript;
    return stored?.sessionId === sessionId &&
      transcript?.session?.id === sessionId &&
      Array.isArray(transcript.messages)
      ? {
          session: transcript.session,
          sessions: Array.isArray(transcript.sessions)
            ? transcript.sessions
            : [transcript.session],
          messages: transcript.messages,
          todos: Array.isArray(transcript.todos) ? transcript.todos : [],
        }
      : null;
  }

  /**
   * Save the chat the user is reading, which also counts as opening it.
   * `conversation` is its chat list row (kept from before when unknown).
   */
  saveChat(
    transcript: SavedTranscript,
    conversation: HarnessConversation | null,
  ): Promise<void> {
    const sessionId = transcript.session.id;
    return this.change(async () => {
      const key = await this.chatKey(sessionId);
      const directory = joinUri(await this.directory(), CHATS, key);
      this.files.makeDirectory(directory);
      this.files.writeText(
        joinUri(directory, TRANSCRIPT),
        JSON.stringify({
          version: VERSION,
          scope: this.scope,
          sessionId,
          transcript,
        }),
      );
      const chats = await this.index();
      const previous = chats.find((chat) => chat.sessionId === sessionId);
      const now = this.now().toISOString();
      await this.writeIndex([
        {
          sessionId,
          workspaceId: transcript.session.workspace_id,
          conversation: transcript.session.parent_id
            ? null
            : (conversation ?? previous?.conversation ?? null),
          openedAt: now,
          savedAt: now,
          key,
        },
        ...chats.filter((chat) => chat.sessionId !== sessionId),
      ]);
    });
  }

  /** Remember that a saved chat was opened, so it is kept another week. */
  markOpened(sessionId: string): Promise<void> {
    return this.change(async () => {
      const chats = await this.index();
      if (!chats.some((chat) => chat.sessionId === sessionId)) return;
      const openedAt = this.now().toISOString();
      await this.writeIndex(
        chats.map((chat) =>
          chat.sessionId === sessionId ? { ...chat, openedAt } : chat,
        ),
      );
    });
  }

  /**
   * Keep a copy of a workspace picture (`file` is the loaded copy) with the
   * chat that shows it. Pictures beyond the chat's budget are not kept.
   */
  saveMedia(sessionId: string, path: string, file: string): Promise<void> {
    if (classifyWorkspaceFile(path) !== "image") return Promise.resolve();
    return this.change(async () => {
      const directory = joinUri(
        await this.directory(),
        CHATS,
        await this.chatKey(sessionId),
      );
      const name = await this.mediaName(path);
      const target = joinUri(directory, name);
      const bytes = this.files.size(file);
      if (!bytes) return;
      if (this.files.exists(target) && this.files.size(target) === bytes)
        return;
      const used = this.files
        .list(directory)
        .filter((entry) => entry !== name && entry !== TRANSCRIPT)
        .reduce(
          (sum, entry) => sum + this.files.size(joinUri(directory, entry)),
          0,
        );
      if (used + bytes > MAX_CHAT_MEDIA_BYTES) return;
      this.files.makeDirectory(directory);
      this.files.copy(file, target);
    });
  }

  /** `file://` URI of a picture saved with the chat, or null. */
  async media(sessionId: string, path: string): Promise<string | null> {
    if (classifyWorkspaceFile(path) !== "image") return null;
    const uri = joinUri(
      await this.directory(),
      CHATS,
      await this.chatKey(sessionId),
      await this.mediaName(path),
    );
    return this.files.exists(uri) ? uri : null;
  }

  private async directory(): Promise<string> {
    return joinUri(this.files.root, await this.key());
  }

  private async chatKey(sessionId: string): Promise<string> {
    return (await this.hash(`chat|${sessionId}`)).slice(0, 32);
  }

  private async mediaName(path: string): Promise<string> {
    const digest = (await this.hash(`media|${path}`)).slice(0, 32);
    return `${digest}${mediaExtension(path)}`;
  }

  private async index(): Promise<ChatEntry[]> {
    const stored = await this.read<{ chats?: unknown }>(INDEX);
    return Array.isArray(stored?.chats) ? stored.chats.filter(isEntry) : [];
  }

  private async writeIndex(chats: ChatEntry[]): Promise<void> {
    writeStored(this.files, await this.directory(), INDEX, this.scope, {
      chats,
    });
  }

  private async read<T>(name: string): Promise<(T & Stored) | null> {
    const stored = await readStored<T>(
      this.files,
      await this.directory(),
      name,
    );
    return stored?.scope === this.scope ? stored : null;
  }

  /** Runs a change after the ones before it, unless the data was deleted. */
  private change(task: () => Promise<void>): Promise<void> {
    return this.serialized(async () => {
      if (!this.revoked) await task();
    });
  }

  private write(name: string, value: object): Promise<void> {
    return this.change(async () => {
      const content = JSON.stringify(value);
      if (this.written.get(name) === content) return;
      writeStored(this.files, await this.directory(), name, this.scope, {
        ...value,
        savedAt: this.now().toISOString(),
      });
      this.written.set(name, content);
    });
  }
}

async function readStored<T>(
  files: OfflineFiles,
  directory: string,
  name: string,
): Promise<(T & Stored) | null> {
  const uri = joinUri(directory, name);
  // A replacement interrupted after the old file was removed leaves the
  // complete new content in the temporary file (see `writeText`).
  for (const candidate of [uri, temporaryUri(uri)]) {
    if (!files.exists(candidate)) continue;
    try {
      const parsed = JSON.parse(await files.readText(candidate)) as
        | (T & Stored)
        | null;
      if (
        parsed &&
        typeof parsed === "object" &&
        parsed.version === VERSION &&
        typeof parsed.scope === "string"
      )
        return parsed;
    } catch {
      /* Unreadable: try the next candidate. */
    }
  }
  return null;
}

function writeStored(
  files: OfflineFiles,
  directory: string,
  name: string,
  scope: string,
  value: object,
): void {
  files.makeDirectory(directory);
  files.writeText(
    joinUri(directory, name),
    JSON.stringify({ ...value, version: VERSION, scope }),
  );
}

let shared: OfflineStore | null = null;

/** The device's offline store; one per process, like the app installer. */
export function offlineStore(): OfflineStore {
  shared ??= new OfflineStore(createLocalFiles("opencuria-offline", "cache"));
  return shared;
}
