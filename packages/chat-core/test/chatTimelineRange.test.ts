import { describe, expect, it, vi } from "vitest";
import {
  ChatTimelineController,
  type ChatTimelineSnapshot,
  type TimelineRange,
} from "../src/chatTimelineController";
import type { HarnessMessage, HarnessSession } from "../src/harness";

const SESSION = "session";

function row(position: number, overrides: Partial<HarnessMessage> = {}) {
  const role = position % 2 === 0 ? "user" : "assistant";
  return {
    id: `m${position}`,
    session_id: SESSION,
    role,
    content: role === "user" ? `prompt ${position}` : "",
    position,
    completed_at: "2026-01-01T00:00:00Z",
    parts:
      role === "assistant"
        ? [
            {
              id: `p${position}`,
              session_id: SESSION,
              message_id: `m${position}`,
              type: "text",
              state: "completed",
              title: "",
              output: `answer ${position}`,
              position: 0,
            },
          ]
        : [],
    ...overrides,
  } as HarnessMessage;
}

/** A backend that honours ranges the way the timeline endpoint does. */
function server(initial: number, honourRanges = true) {
  const rows = new Map<number, HarnessMessage>();
  for (let position = 0; position < initial; position += 1)
    rows.set(position, row(position));
  const answer = (range?: TimelineRange): ChatTimelineSnapshot => {
    const all = [...rows.values()].sort((a, b) => a.position! - b.position!);
    if (!honourRanges || !range) return { messages: structuredClone(all) };
    let shown = all.filter(
      (message) =>
        (range.since === undefined || message.position! >= range.since) &&
        (range.before === undefined || message.position! < range.before),
    );
    if (range.limit !== undefined) shown = shown.slice(-range.limit);
    const first = shown[0]?.position ?? range.before ?? range.since ?? 0;
    return {
      messages: structuredClone(shown),
      older_count: all.filter((message) => message.position! < first).length,
    };
  };
  return { rows, answer };
}

function setup(
  backend: ReturnType<typeof server>,
  options: { tailLimit?: number; pageLimit?: number } = {},
) {
  const store = new Map<string, HarnessMessage[]>();
  const sessions = [{ id: SESSION, status: "idle", parent_id: null }];
  const load = vi.fn(async (_id: string, range?: TimelineRange) =>
    backend.answer(range),
  );
  const onChange = vi.fn();
  const controller = new ChatTimelineController<ChatTimelineSnapshot>({
    load,
    read: (id) => store.get(id),
    write: (id, next) => store.set(id, next),
    sessions: () => sessions as HarnessSession[],
    retainedSessionIds: () => [SESSION],
    forget: (id) => store.delete(id),
    onChange,
    tailLimit: options.tailLimit ?? 4,
    pageLimit: options.pageLimit ?? 3,
  });
  const positions = () => (store.get(SESSION) ?? []).map((m) => m.position);
  return { controller, store, load, onChange, positions };
}

describe("ChatTimelineController ranges", () => {
  it("opens with the newest messages and backfills the rest in pages", async () => {
    const h = setup(server(12));
    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenCalledWith(SESSION, { limit: 4 });
    expect(h.positions()).toEqual([8, 9, 10, 11]);
    expect(h.controller.hasOlder(SESSION)).toBe(true);

    await h.controller.backfill(SESSION);
    expect(h.load.mock.calls.slice(1).map((call) => call[1])).toEqual([
      { limit: 3, before: 8 },
      { limit: 3, before: 5 },
      { limit: 3, before: 2 },
    ]);
    expect(h.positions()).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    expect(h.controller.hasOlder(SESSION)).toBe(false);
    expect(h.onChange).toHaveBeenCalledTimes(3);
    // Older rows are shown exactly like loaded ones.
    expect(h.store.get(SESSION)![1]).toMatchObject({
      session_id: SESSION,
      content: "answer 1",
    });
  });

  it("asks a reload only for the messages that can have changed", async () => {
    const backend = server(10);
    const h = setup(backend);
    await h.controller.fetch(SESSION);
    await h.controller.backfill(SESSION);
    backend.rows.set(10, row(10));
    backend.rows.set(11, row(11));

    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenLastCalledWith(SESSION, { since: 8 });
    expect(h.positions()).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    expect(h.controller.hasOlder(SESSION)).toBe(false);
  });

  it("reloads from the first recent message still streaming", async () => {
    const backend = server(10);
    backend.rows.set(5, row(5, { completed_at: null }));
    const h = setup(backend, { tailLimit: 20 });
    await h.controller.fetch(SESSION);
    backend.rows.set(5, row(5));
    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenLastCalledWith(SESSION, { since: 5 });
    expect(h.store.get(SESSION)![5]!.completed_at).not.toBeNull();
  });

  it("does not reload for an old reply that never finished", async () => {
    const backend = server(20);
    backend.rows.set(3, row(3, { completed_at: null }));
    const h = setup(backend, { tailLimit: 30 });
    await h.controller.fetch(SESSION);
    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenLastCalledWith(SESSION, { since: 18 });
  });

  it("reloads in full when the held messages contradict the server", async () => {
    const backend = server(10);
    const h = setup(backend);
    await h.controller.fetch(SESSION);
    await h.controller.backfill(SESSION);
    // Another client edited an old prompt: later messages are gone.
    for (const position of [3, 4, 5, 6, 7, 8, 9]) backend.rows.delete(position);
    backend.rows.set(3, row(3, { content: "edited" }));

    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenLastCalledWith(SESSION);
    expect(h.positions()).toEqual([0, 1, 2, 3]);
    expect(h.controller.hasOlder(SESSION)).toBe(false);
  });

  it("keeps a partial chat consistent while older pages are missing", async () => {
    const backend = server(12);
    const h = setup(backend);
    await h.controller.fetch(SESSION);
    backend.rows.set(12, row(12));
    await h.controller.fetch(SESSION);
    expect(h.load).toHaveBeenLastCalledWith(SESSION, { since: 10 });
    expect(h.positions()).toEqual([8, 9, 10, 11, 12]);
    expect(h.controller.hasOlder(SESSION)).toBe(true);
    await h.controller.backfill(SESSION);
    expect(h.positions()).toHaveLength(13);
  });

  it("treats a backend that ignores ranges as a complete load", async () => {
    const h = setup(server(10, false));
    await h.controller.fetch(SESSION);
    expect(h.positions()).toHaveLength(10);
    expect(h.controller.hasOlder(SESSION)).toBe(false);
    await h.controller.backfill(SESSION);
    expect(h.load).toHaveBeenCalledTimes(1);
  });

  it("loads everything when asked and without a configured range", async () => {
    const backend = server(10);
    const h = setup(backend);
    await h.controller.fetch(SESSION);
    await h.controller.fetch(SESSION, { full: true });
    expect(h.load).toHaveBeenLastCalledWith(SESSION);
    expect(h.positions()).toHaveLength(10);
    expect(h.controller.hasOlder(SESSION)).toBe(false);

    const plain = new ChatTimelineController<ChatTimelineSnapshot>({
      load: h.load,
      read: (id) => h.store.get(id),
      write: (id, next) => h.store.set(id, next),
      sessions: () => [],
      retainedSessionIds: () => [SESSION],
      forget: () => undefined,
    });
    h.load.mockClear();
    await plain.fetch(SESSION);
    expect(h.load).toHaveBeenCalledWith(SESSION);
  });

  it("drops a page that arrives after the transcript was forgotten", async () => {
    const h = setup(server(12));
    await h.controller.fetch(SESSION);
    let release!: () => void;
    h.load.mockImplementationOnce(async (_id, range) => {
      await new Promise<void>((resolve) => (release = resolve));
      return server(12).answer(range);
    });
    const running = h.controller.backfill(SESSION);
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    h.controller.forget(SESSION);
    h.store.delete(SESSION);
    release();
    await running;
    expect(h.store.has(SESSION)).toBe(false);
    expect(h.onChange).not.toHaveBeenCalled();
  });

  it("never leaves a gap when the chat is reloaded during a page", async () => {
    const backend = server(12);
    const h = setup(backend);
    await h.controller.fetch(SESSION);
    let release!: () => void;
    h.load
      .mockImplementationOnce(async (_id, range) => backend.answer(range))
      .mockImplementationOnce(async (_id, range) => {
        await new Promise<void>((resolve) => (release = resolve));
        return backend.answer(range);
      });
    const running = h.controller.backfill(SESSION);
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    expect(h.positions()).toEqual([5, 6, 7, 8, 9, 10, 11]);
    // Evicted and opened again while the page before 5 is on its way.
    h.controller.forget(SESSION);
    h.store.delete(SESSION);
    await h.controller.fetch(SESSION);
    expect(h.positions()).toEqual([8, 9, 10, 11]);
    release();
    await running;
    expect(h.positions()).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  });

  it("stops backfilling when the chat is no longer current", async () => {
    const h = setup(server(12));
    await h.controller.fetch(SESSION);
    let current = true;
    h.load.mockImplementationOnce(async (_id, range) => {
      current = false;
      return server(12).answer(range);
    });
    await h.controller.backfill(SESSION, { isCurrent: () => current });
    expect(h.load).toHaveBeenCalledTimes(2);
    expect(h.positions()).toEqual([5, 6, 7, 8, 9, 10, 11]);
  });

  it("survives a failed page and leaves what is shown untouched", async () => {
    const h = setup(server(12));
    await h.controller.fetch(SESSION);
    h.load.mockRejectedValueOnce(new Error("offline"));
    await expect(h.controller.backfill(SESSION)).resolves.toBeUndefined();
    expect(h.positions()).toEqual([8, 9, 10, 11]);
    expect(h.controller.hasOlder(SESSION)).toBe(true);
    // A later attempt carries on where it stopped.
    await h.controller.backfill(SESSION);
    expect(h.positions()).toHaveLength(12);
  });

  it("shares one backfill between callers", async () => {
    const h = setup(server(12));
    await h.controller.fetch(SESSION);
    await Promise.all([
      h.controller.backfill(SESSION),
      h.controller.backfill(SESSION),
    ]);
    expect(h.load).toHaveBeenCalledTimes(4);
  });
});
