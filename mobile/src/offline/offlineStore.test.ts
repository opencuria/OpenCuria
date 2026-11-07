import { beforeEach, describe, expect, it } from "vitest";
import { File, __directories, __files } from "expo-file-system";
import type {
  HarnessMessage,
  HarnessSession,
} from "@opencuria/chat-core/harness";
import type {
  AuthUser,
  HarnessConversation,
  Workspace,
} from "@opencuria/client";
import { createLocalFiles } from "../platform/localFiles";
import {
  MAX_CHAT_MEDIA_BYTES,
  MAX_SAVED_CHATS,
  OfflineStore,
  type SavedTranscript,
} from "./offlineStore";

const ROOT = "file:///cache/opencuria-offline";
const SCOPE = "https://curia.test/api/v1|org-a|7";
const OTHER = "https://curia.test/api/v1|org-b|7";
const DAY = 24 * 60 * 60 * 1000;

let clock = Date.parse("2026-11-07T12:00:00Z");
const now = () => new Date(clock);

function store() {
  return new OfflineStore(
    createLocalFiles("opencuria-offline", "cache"),
    undefined,
    now,
  );
}

const user: AuthUser = {
  id: 7,
  email: "ada@example.test",
  first_name: "Ada",
  last_name: "Lovelace",
  organizations: [],
} as unknown as AuthUser;

function session(id: string, parentId: string | null = null): HarnessSession {
  return {
    id,
    workspace_id: "ws-1",
    parent_id: parentId,
    title: `Chat ${id}`,
    mode: "build",
    agent_name: "build",
    model: "m",
    status: "idle",
    cost: 0,
    tokens: {},
  };
}

function transcript(
  id: string,
  parentId: string | null = null,
): SavedTranscript {
  return {
    session: session(id, parentId),
    sessions: [session(id, parentId)],
    messages: [
      {
        id: `${id}-m1`,
        session_id: id,
        role: "assistant",
        content: "",
        parts: [],
      } as HarnessMessage,
    ],
    todos: [],
  };
}

function conversation(id: string): HarnessConversation {
  return {
    session_id: id,
    workspace_id: "ws-1",
    workspace_name: "dev",
    title: `Chat ${id}`,
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: false,
    updated_at: "2026-11-07T11:00:00Z",
    last_message_at: "2026-11-07T11:00:00Z",
  };
}

function picture(name: string, bytes: number): string {
  const file = new File(`file:///cache/opencuria/${name}`);
  file.create({ intermediates: true });
  file.write("x".repeat(bytes));
  return file.uri;
}

beforeEach(() => {
  __files.clear();
  __directories.clear();
  clock = Date.parse("2026-11-07T12:00:00Z");
});

describe("OfflineStore", () => {
  it("keeps profile and lists per account under hashed names", async () => {
    const offline = store();
    const workspace = { id: "ws-1", name: "dev" } as Workspace;
    await offline.account(SCOPE).saveProfile(user, []);
    await offline
      .account(SCOPE)
      .saveOverview([workspace], [conversation("s1")]);

    expect(await offline.account(SCOPE).loadProfile()).toMatchObject({
      user: { email: "ada@example.test" },
      organizations: [],
    });
    expect(await offline.account(SCOPE).loadOverview()).toMatchObject({
      workspaces: [workspace],
      conversations: [{ session_id: "s1" }],
    });
    // Another organization of the same user sees nothing of it.
    expect(await offline.account(OTHER).loadProfile()).toBeNull();
    expect(await offline.account(OTHER).loadOverview()).toBeNull();
    for (const uri of [...__files.keys(), ...__directories])
      if (uri.startsWith(ROOT))
        expect(uri.slice(ROOT.length)).not.toMatch(/curia|org-a|s1/);
  });

  it("saves the chat being read and opens it again, subtasks without a list row", async () => {
    const offline = store();
    const account = offline.account(SCOPE);
    await account.saveChat(transcript("s1"), conversation("s1"));
    await account.saveChat(transcript("child", "s1"), conversation("child"));

    expect(await account.chats()).toMatchObject([
      { sessionId: "child", workspaceId: "ws-1", conversation: null },
      { sessionId: "s1", conversation: { session_id: "s1" } },
    ]);
    expect(await account.loadChat("s1")).toMatchObject({
      session: { id: "s1" },
      messages: [{ id: "s1-m1" }],
      todos: [],
    });
    expect(await account.loadChat("unknown")).toBeNull();
    expect(await offline.account(OTHER).loadChat("s1")).toBeNull();

    // A later save without a known row keeps the one from before.
    await account.saveChat(transcript("s1"), null);
    expect((await account.chats())[0]).toMatchObject({
      sessionId: "s1",
      conversation: { session_id: "s1" },
    });
  });

  it("forgets chats not opened for a week, unless they are opened again", async () => {
    const offline = store();
    const account = offline.account(SCOPE);
    await account.saveChat(transcript("old"), conversation("old"));
    await account.saveChat(transcript("kept"), conversation("kept"));
    clock += 6 * DAY;
    await account.markOpened("kept");
    clock += 1 * DAY + 1;

    expect((await account.chats()).map((chat) => chat.sessionId)).toEqual([
      "kept",
    ]);
    expect(await account.loadChat("old")).toBeNull();
    expect(await account.loadChat("kept")).not.toBeNull();
  });

  it("keeps pictures with their chat and deletes them with it", async () => {
    const offline = store();
    const account = offline.account(SCOPE);
    await account.saveChat(transcript("s1"), conversation("s1"));
    const source = picture("a.png", 10);
    await account.saveMedia("s1", "/workspace/shot.png", source);
    await account.saveMedia("s1", "/workspace/demo.mp4", picture("b.mp4", 10));

    const saved = await account.media("s1", "/workspace/shot.png");
    expect(saved).toMatch(/\.png$/);
    expect(await new File(saved!).text()).toBe("x".repeat(10));
    expect(await account.media("s1", "/workspace/demo.mp4")).toBeNull();
    expect(await account.media("s2", "/workspace/shot.png")).toBeNull();

    clock += 8 * DAY;
    await offline.cleanUp(SCOPE);
    expect(await account.media("s1", "/workspace/shot.png")).toBeNull();
    expect(await account.chats()).toEqual([]);
    // The signed-in account's profile stays for opening offline.
    await account.saveProfile(user, []);
    clock += 30 * DAY;
    await offline.cleanUp(SCOPE);
    expect(await account.loadProfile()).not.toBeNull();
  });

  it("does not keep pictures beyond a chat's budget", async () => {
    const offline = store();
    const account = offline.account(SCOPE);
    await account.saveMedia(
      "s1",
      "/workspace/big.png",
      picture("big.png", MAX_CHAT_MEDIA_BYTES - 5),
    );
    await account.saveMedia("s1", "/workspace/more.png", picture("m.png", 10));
    expect(await account.media("s1", "/workspace/big.png")).not.toBeNull();
    expect(await account.media("s1", "/workspace/more.png")).toBeNull();
  });

  it("removes other accounts once unused for a week and leftovers of saves", async () => {
    const offline = store();
    await offline.account(OTHER).saveProfile(user, []);
    await offline.account(SCOPE).saveChat(transcript("s1"), conversation("s1"));
    // A picture saved for a chat that was never saved.
    await offline
      .account(SCOPE)
      .saveMedia("lost", "/workspace/a.png", picture("a.png", 3));

    clock += 2 * DAY;
    await offline.cleanUp(SCOPE);
    expect(await offline.account(OTHER).loadProfile()).not.toBeNull();
    expect(
      await offline.account(SCOPE).media("lost", "/workspace/a.png"),
    ).toBeNull();
    expect(await offline.account(SCOPE).loadChat("s1")).not.toBeNull();

    clock += 6 * DAY;
    await offline.cleanUp(SCOPE);
    expect(await offline.account(OTHER).loadProfile()).toBeNull();
  });

  it("keeps the most recently opened chats when there are too many", async () => {
    const offline = store();
    const account = offline.account(SCOPE);
    for (let index = 0; index <= MAX_SAVED_CHATS; index += 1) {
      clock += 1000;
      await account.saveChat(transcript(`s${index}`), null);
    }
    await offline.cleanUp(SCOPE);
    const chats = await account.chats();
    expect(chats).toHaveLength(MAX_SAVED_CHATS);
    expect(chats.some((chat) => chat.sessionId === "s0")).toBe(false);
    expect(await account.loadChat("s0")).toBeNull();
  });

  it("deletes everything on sign out and saves again afterwards", async () => {
    const offline = store();
    await offline.account(SCOPE).saveProfile(user, []);
    await offline.account(SCOPE).saveChat(transcript("s1"), conversation("s1"));
    await offline.clear();
    expect([...__files.keys()].filter((uri) => uri.startsWith(ROOT))).toEqual(
      [],
    );
    expect(await offline.account(SCOPE).loadProfile()).toBeNull();

    await offline.account(SCOPE).saveProfile(user, []);
    expect(await offline.account(SCOPE).loadProfile()).not.toBeNull();
  });

  it("drops saves that arrive after everything was deleted", async () => {
    const offline = store();
    const stale = offline.account(SCOPE);
    await stale.saveProfile(user, []);
    const clearing = offline.clear();
    // A screen closing on sign out still saves its chat.
    const late = stale.saveChat(transcript("s1"), conversation("s1"));
    await Promise.all([clearing, late]);
    await stale.saveMedia("s1", "/workspace/a.png", picture("a.png", 3));
    expect([...__files.keys()].filter((uri) => uri.startsWith(ROOT))).toEqual(
      [],
    );
    expect(await offline.account(SCOPE).loadChat("s1")).toBeNull();
  });

  it("keeps chats, pictures and accounts apart where the native hash ends strings at NUL", async () => {
    const { createHash } = await import("node:crypto");
    // React Native's bridge passes strings as C strings to expo-crypto.
    const nativeHash = async (value: string) =>
      createHash("sha256").update(value.split("\0")[0]!).digest("hex");
    const offline = new OfflineStore(
      createLocalFiles("opencuria-offline", "cache"),
      nativeHash,
      now,
    );
    const account = offline.account(SCOPE);
    await account.saveChat(transcript("s1"), conversation("s1"));
    await account.saveChat(transcript("s2"), conversation("s2"));
    expect((await account.loadChat("s1"))?.session.id).toBe("s1");
    expect((await account.loadChat("s2"))?.session.id).toBe("s2");

    await account.saveMedia("s1", "/workspace/a.png", picture("a.png", 3));
    await account.saveMedia("s1", "/workspace/b.png", picture("b.png", 5));
    const a = await account.media("s1", "/workspace/a.png");
    const b = await account.media("s1", "/workspace/b.png");
    expect(a).not.toBe(b);
    expect(await new File(b!).text()).toBe("xxxxx");

    await offline.account(OTHER).saveProfile(user, []);
    expect(await account.loadProfile()).toBeNull();
  });

  it("ignores files of another format", async () => {
    const offline = store();
    await offline.account(SCOPE).saveProfile(user, []);
    const [profile] = [...__files.keys()].filter((uri) =>
      uri.endsWith("profile.json"),
    );
    new File(profile!).write(JSON.stringify({ version: 99, scope: SCOPE }));
    expect(await offline.account(SCOPE).loadProfile()).toBeNull();
    new File(profile!).write("{broken");
    expect(await offline.account(SCOPE).loadProfile()).toBeNull();
  });
});
