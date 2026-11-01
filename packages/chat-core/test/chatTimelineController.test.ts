import { describe, expect, it, vi } from "vitest";
import { ChatTimelineController } from "../src/chatTimelineController";
import type { ChatTimelineSnapshot } from "../src/chatTimelineController";
import type {
  HarnessMessage,
  HarnessPart,
  HarnessSession,
} from "../src/harness";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function part(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: "part",
    session_id: "session",
    message_id: "answer",
    type: "text",
    state: "running",
    title: "",
    output: "",
    ...overrides,
  };
}

function message(
  output: string,
  overrides: Partial<HarnessMessage> = {},
): HarnessMessage {
  return {
    id: "answer",
    session_id: "session",
    role: "assistant",
    content: output,
    parts: [part({ output })],
    ...overrides,
  };
}

function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

function harness(
  immutable: boolean,
  options: {
    detailLimit?: number;
    transcriptLimit?: number;
    transcriptBudgetBytes?: number;
  } = {},
) {
  const messages = new Map<string, HarnessMessage[]>();
  const statuses = new Map<string, HarnessSession["status"]>([
    ["session", "busy"],
  ]);
  const protectedIds = new Set<string>();
  const loads: ReturnType<typeof deferred<ChatTimelineSnapshot>>[] = [];
  const details: ReturnType<typeof deferred<HarnessPart>>[] = [];
  const load = vi.fn(() => {
    const pending = deferred<ChatTimelineSnapshot>();
    loads.push(pending);
    return pending.promise;
  });
  const loadDetail = vi.fn(() => {
    const pending = deferred<HarnessPart>();
    details.push(pending);
    return pending.promise;
  });
  const controller = new ChatTimelineController({
    load,
    loadDetail,
    read: (id) => messages.get(id),
    write: (id, next) => {
      messages.set(id, immutable ? freeze(next) : next);
    },
    status: (id) => statuses.get(id),
    protectedSessions: () => new Set(protectedIds),
    forget: (id) => {
      messages.delete(id);
    },
    ...options,
  });
  return {
    controller,
    messages,
    statuses,
    protectedIds,
    loads,
    details,
    load,
    loadDetail,
  };
}

describe.each([false, true])(
  "ChatTimelineController (immutable storage: %s)",
  (immutable) => {
    it("deduplicates a request and rejects an obsolete navigation response", async () => {
      const h = harness(immutable);
      let selection = "first";
      const first = h.controller.fetch("session", {
        context: "first",
        isCurrent: () => selection === "first",
      });
      const duplicate = h.controller.fetch("session", { context: "first" });
      expect(h.load).toHaveBeenCalledTimes(1);
      selection = "second";
      const second = h.controller.fetch("session", { context: "second" });
      h.loads[1]!.resolve({ messages: [message("new")] });
      await second;
      h.loads[0]!.resolve({ messages: [message("old")] });
      await Promise.all([first, duplicate]);
      expect(h.messages.get("session")?.[0]?.content).toBe("new");
    });

    it("preserves live progress and metadata without mutating a published snapshot", async () => {
      const h = harness(immutable);
      const previous = freeze([
        message("live answer", {
          model: "model",
          parts: [part({ output: "live answer", state: "completed" })],
        }),
      ]);
      h.messages.set("session", previous);
      const request = h.controller.fetch("session");
      h.loads[0]!.resolve({ messages: [message("live")] });
      await request;
      const current = h.messages.get("session")!;
      expect(current[0]?.content).toBe("live answer");
      expect(current[0]?.parts[0]?.state).toBe("completed");
      expect(previous[0]?.content).toBe("live answer");
      expect(current).not.toBe(previous);
    });

    it("does not truncate a final stream when a busy request arrives after idle", async () => {
      const h = harness(immutable);
      h.messages.set("session", freeze([message("final answer")]));
      const request = h.controller.fetch("session");
      h.statuses.set("session", "idle");
      h.loads[0]!.resolve({ messages: [message("final")] });
      await request;
      expect(h.messages.get("session")?.[0]?.content).toBe("final answer");
      const terminal = h.controller.fetch("session");
      h.loads[1]!.resolve({
        messages: [message("authoritative", { completed_at: "done" })],
      });
      await terminal;
      expect(h.messages.get("session")?.[0]?.content).toBe("authoritative");
      expect(h.messages.get("session")?.[0]?.parts[0]?.state).toBe("completed");
    });

    it("captures gate/request metadata after waiting for a pre-idle fetch", async () => {
      const h = harness(immutable);
      let revision = 1;
      const committed: number[] = [];
      const first = h.controller.fetch("session");
      const refresh = h.controller.fetch("session", {
        refreshAfterExisting: true,
        prepare: () => {
          const atStart = revision;
          return () => {
            committed.push(atStart);
          };
        },
      });
      revision = 2;
      h.loads[0]!.resolve({ messages: [] });
      await first;
      await vi.waitFor(() => expect(h.loads).toHaveLength(2));
      h.loads[1]!.resolve({ messages: [message("final")] });
      await refresh;
      expect(committed).toEqual([2]);
    });

    it("never resurrects a removed session or a prior authentication context", async () => {
      const h = harness(immutable);
      const request = h.controller.fetch("session");
      h.controller.clear();
      h.loads[0]!.resolve({ messages: [message("private")] });
      await request;
      expect(h.messages.has("session")).toBe(false);
      const next = h.controller.fetch("session");
      h.controller.forget("session");
      h.loads[1]!.resolve({ messages: [message("deleted")] });
      await next;
      expect(h.messages.has("session")).toBe(false);
    });

    it("propagates a current transport error and suppresses errors from stale contexts", async () => {
      const h = harness(immutable);
      const first = h.controller.fetch("session");
      const failure = expect(first).rejects.toThrow("offline");
      h.loads[0]!.reject(new Error("offline"));
      await failure;
      const old = h.controller.fetch("session");
      h.controller.invalidate("session");
      h.loads[1]!.reject(new Error("obsolete"));
      await expect(old).resolves.toBeUndefined();
    });

    it("hydrates completed details once, restores them across snapshots and retains parent IDs", async () => {
      const h = harness(immutable);
      h.messages.set(
        "session",
        freeze([
          message("", {
            parts: [part({ type: "tool", tool: "read", state: "completed" })],
          }),
        ]),
      );
      const first = h.controller.requestDetail("session", "part");
      const same = h.controller.requestDetail("session", "part");
      expect(h.loadDetail).toHaveBeenCalledTimes(1);
      h.details[0]!.resolve(
        part({
          type: "tool",
          state: "completed",
          input: { tool: "read", arguments: "file" },
          output: "contents",
        }),
      );
      await Promise.all([first, same]);
      const before = h.messages.get("session");
      const timeline = h.controller.fetch("session");
      h.loads[0]!.resolve({
        messages: [
          message("", { parts: [part({ type: "tool", state: "completed" })] }),
        ],
      });
      await timeline;
      expect(h.messages.get("session")?.[0]?.parts[0]).toMatchObject({
        output: "contents",
        detail_loaded: true,
        session_id: "session",
        message_id: "answer",
      });
      expect(before?.[0]?.parts[0]?.output).toBe("contents");
    });

    it("rejects detail hydration after selection change and run invalidation", async () => {
      const h = harness(immutable);
      h.messages.set("session", [
        message("", { parts: [part({ type: "tool", state: "completed" })] }),
      ]);
      let selected = true;
      const request = h.controller.requestDetail("session", "part", {
        isCurrent: () => selected,
      });
      selected = false;
      const failure = expect(request).rejects.toThrow("Conversation changed");
      h.details[0]!.resolve(part({ output: "secret", detail_loaded: true }));
      await failure;
      expect(h.messages.get("session")?.[0]?.parts[0]?.detail_loaded).not.toBe(
        true,
      );
      selected = true;
      const next = h.controller.requestDetail("session", "part");
      h.controller.invalidateDetails("session");
      const invalid = expect(next).rejects.toThrow("Conversation changed");
      h.details[1]!.resolve(part({ output: "old" }));
      await invalid;
    });

    it("rejects unfinished details without a network request", async () => {
      const h = harness(immutable);
      h.messages.set("session", [message("running")]);
      await expect(
        h.controller.requestDetail("session", "part"),
      ).rejects.toThrow("until the part is complete");
      expect(h.loadDetail).not.toHaveBeenCalled();
    });

    it("bounds detail retention while protecting pinned rows and preserving errors", async () => {
      const h = harness(immutable, { detailLimit: 1 });
      const first = part({
        id: "first",
        type: "tool",
        state: "error",
        output: "Tool failed",
        detail_loaded: true,
      });
      const second = part({
        id: "second",
        type: "tool",
        state: "completed",
        output: "large",
        detail_loaded: true,
      });
      h.messages.set(
        "session",
        freeze([message("", { parts: [first, second] })]),
      );
      h.controller.pinDetail("session", "first", true);
      h.controller.rememberDetail("session", first);
      h.controller.rememberDetail("session", second);
      expect(h.controller.cachedDetail("session", "first")).toBeDefined();
      expect(h.messages.get("session")?.[0]?.parts[1]).toMatchObject({
        output: "",
        detail_loaded: false,
      });
      h.controller.pinDetail("session", "first", false);
      h.controller.rememberDetail("session", second);
      expect(h.messages.get("session")?.[0]?.parts[0]).toMatchObject({
        output: "Tool failed",
        detail_loaded: false,
      });
    });

    it("uses one LRU policy for transcripts and protects explicit pins", () => {
      const h = harness(immutable, { transcriptLimit: 1 });
      for (const id of ["first", "second", "active"]) {
        h.messages.set(id, [message(id)]);
        h.controller.touch(id, true);
      }
      h.protectedIds.add("active");
      h.controller.trim();
      expect([...h.messages.keys()]).toEqual(["second", "active"]);
    });

    it("enforces the byte budget even when the transcript count is within limit", () => {
      const h = harness(immutable, { transcriptBudgetBytes: 1 });
      h.messages.set("large", [message("retained bytes")]);
      h.controller.touch("large", true);
      h.controller.trim();
      expect(h.messages.has("large")).toBe(false);
    });
  },
);
