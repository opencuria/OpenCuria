import { describe, expect, it, vi } from "vitest";
import type { OpenCuriaClient } from "@opencuria/client";
import {
  TerminalSession,
  getTerminalSession,
  releaseTerminalSessions,
  trimTerminalHistory,
} from "./terminalSession";
import { encodeTerminalInput } from "../utf8";

function fakeClient() {
  const handlers = new Map<string, Set<(data: any) => void>>();
  const reconnect = new Set<() => void>();
  let resolveStart: (value: { task_id: string }) => void = () => undefined;
  const terminal = {
    start: vi.fn(
      () =>
        new Promise<{ task_id: string }>((resolve) => {
          resolveStart = resolve;
        }),
    ),
    input: vi.fn(() => true),
    resize: vi.fn(() => true),
    close: vi.fn(() => true),
  };
  const client = {
    terminal,
    realtime: {
      subscribeWorkspace: vi.fn(() => () => undefined),
      on: (event: string, handler: (data: any) => void) => {
        const set = handlers.get(event) ?? new Set();
        set.add(handler);
        handlers.set(event, set);
        return () => set.delete(handler);
      },
      onReconnect: (callback: () => void) => {
        reconnect.add(callback);
        return () => reconnect.delete(callback);
      },
    },
  } as unknown as OpenCuriaClient;
  return {
    client,
    terminal,
    emit: (event: string, data: unknown) =>
      handlers.get(event)?.forEach((handler) => handler(data)),
    reconnect: () => reconnect.forEach((callback) => callback()),
    resolveStart: (taskId: string) => resolveStart({ task_id: taskId }),
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const b64 = (text: string) => Buffer.from(text, "utf8").toString("base64");

describe("TerminalSession", () => {
  it("adopts only its own started task, even before the start request resolves", async () => {
    const fake = fakeClient();
    const session = new TerminalSession(fake.client, "ws");
    session.resize({ cols: 100, rows: 30 });
    session.start();
    expect(fake.terminal.start).toHaveBeenCalledWith("ws", 100, 30);
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "other",
      task_id: "t-other",
    });
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "mine",
      task_id: "t1",
    });
    fake.resolveStart("t1");
    await flush();
    expect(session.getSnapshot()).toMatchObject({
      status: "running",
      terminalId: "mine",
    });
    expect(fake.terminal.resize).toHaveBeenCalledWith("ws", "mine", 100, 30);
  });

  it("streams decoded output of its terminal and encodes input", async () => {
    const fake = fakeClient();
    const session = new TerminalSession(fake.client, "ws");
    const output: string[] = [];
    session.onOutput((data) => output.push(data));
    session.start();
    fake.resolveStart("t1");
    await flush();
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "mine",
      task_id: "t1",
    });
    const euro = Buffer.from("€", "utf8");
    fake.emit("terminal:output", {
      workspace_id: "ws",
      terminal_id: "mine",
      data: euro.subarray(0, 1).toString("base64"),
    });
    fake.emit("terminal:output", {
      workspace_id: "ws",
      terminal_id: "mine",
      data: euro.subarray(1).toString("base64"),
    });
    fake.emit("terminal:output", {
      workspace_id: "ws",
      terminal_id: "foreign",
      data: b64("x"),
    });
    expect(output.join("")).toBe("€");
    expect(session.getHistory()).toBe("€");
    expect(session.write("ls\r")).toBe(true);
    expect(fake.terminal.input).toHaveBeenCalledWith(
      "ws",
      "mine",
      encodeTerminalInput("ls\r"),
    );
  });

  it("reports a failed start and restarts after a reconnect", async () => {
    const fake = fakeClient();
    const session = new TerminalSession(fake.client, "ws");
    session.start();
    fake.resolveStart("t1");
    await flush();
    fake.emit("workspace:error", {
      workspace_id: "ws",
      task_id: "t1",
      error: "Runner offline",
    });
    expect(session.getSnapshot()).toMatchObject({
      status: "error",
      error: "Runner offline",
    });
    session.start();
    fake.resolveStart("t2");
    await flush();
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "mine",
      task_id: "t2",
    });
    fake.reconnect();
    expect(session.getSnapshot().status).toBe("starting");
    expect(fake.terminal.start).toHaveBeenCalledTimes(3);
  });

  it("marks the shell ended when its terminal closes", async () => {
    const fake = fakeClient();
    const session = new TerminalSession(fake.client, "ws");
    session.start();
    fake.resolveStart("t1");
    await flush();
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "mine",
      task_id: "t1",
    });
    fake.emit("terminal:closed", { workspace_id: "ws", terminal_id: "mine" });
    expect(session.getSnapshot().status).toBe("ended");
    expect(session.write("x")).toBe(false);
  });

  it("keeps one session per workspace until the client is released", async () => {
    const fake = fakeClient();
    const first = getTerminalSession(fake.client, "ws");
    expect(getTerminalSession(fake.client, "ws")).toBe(first);
    first.start();
    fake.resolveStart("t1");
    await flush();
    fake.emit("terminal:started", {
      workspace_id: "ws",
      terminal_id: "mine",
      task_id: "t1",
    });
    releaseTerminalSessions(fake.client);
    expect(fake.terminal.close).toHaveBeenCalledWith("ws", "mine");
    expect(getTerminalSession(fake.client, "ws")).not.toBe(first);
  });

  it("trims retained output at a line start", () => {
    expect(trimTerminalHistory("aaa\nbbb\nccc", 6)).toBe("ccc");
  });
});
