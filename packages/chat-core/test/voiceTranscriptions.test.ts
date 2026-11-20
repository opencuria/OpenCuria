import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  VoiceTranscriptionError,
  VoiceTranscriptionQueue,
  runVoiceTranscription,
  type VoiceClip,
  type VoiceClipStore,
  type VoiceConnectivity,
} from "../src/voiceTranscriptions";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function network(initial = true) {
  let online = initial;
  const listeners = new Set<(online: boolean) => void>();
  const connectivity: VoiceConnectivity = {
    online: () => online,
    watch: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return {
    connectivity,
    set(value: boolean) {
      online = value;
      for (const listener of [...listeners]) listener(value);
    },
    listeners,
  };
}

const offline = () => new TypeError("Network request failed");
const status = (code: number) =>
  Object.assign(new Error(`HTTP ${code}`), { status: code });

async function flush(): Promise<void> {
  for (let index = 0; index < 60; index++) await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("runVoiceTranscription", () => {
  it("waits for the network and retries as soon as it is back", async () => {
    const net = network();
    const transcribe = vi
      .fn()
      .mockRejectedValueOnce(offline())
      .mockResolvedValueOnce({ text: "Hello." });
    const waits: unknown[] = [];
    const result = runVoiceTranscription({
      transcribe,
      connectivity: net.connectivity,
      onWait: (wait) => waits.push(wait?.failure.kind ?? null),
    });
    await flush();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(waits).toEqual([null, "offline"]);
    // Before the first pause ends, the network returns.
    net.set(true);
    await expect(result).resolves.toEqual({ text: "Hello." });
    expect(transcribe).toHaveBeenCalledTimes(2);
  });

  it("sends nothing while the device is offline", async () => {
    const net = network(false);
    const transcribe = vi.fn().mockResolvedValue({ text: "Later." });
    const result = runVoiceTranscription({
      transcribe,
      connectivity: net.connectivity,
    });
    await vi.advanceTimersByTimeAsync(600_000);
    expect(transcribe).not.toHaveBeenCalled();
    net.set(true);
    await expect(result).resolves.toEqual({ text: "Later." });
  });

  it("keeps retrying a lost connection with growing pauses", async () => {
    const transcribe = vi
      .fn()
      .mockRejectedValueOnce(offline())
      .mockRejectedValueOnce(offline())
      .mockResolvedValueOnce({ text: "Third." });
    const result = runVoiceTranscription({
      transcribe,
      retryDelaysMs: [1_000, 5_000],
    });
    await vi.advanceTimersByTimeAsync(999);
    expect(transcribe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(transcribe).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(result).resolves.toEqual({ text: "Third." });
  });

  it("retries a busy service a few times, then asks the user", async () => {
    const transcribe = vi.fn().mockRejectedValue(status(503));
    const result = runVoiceTranscription({
      transcribe,
      retryDelaysMs: [10],
      serviceRetries: 2,
    });
    const settled = expect(result).rejects.toBeInstanceOf(
      VoiceTranscriptionError,
    );
    await vi.advanceTimersByTimeAsync(100);
    await settled;
    expect(transcribe).toHaveBeenCalledTimes(3);
  });

  it("never repeats a recording the server refused", async () => {
    const transcribe = vi.fn().mockRejectedValue(status(409));
    await expect(runVoiceTranscription({ transcribe })).rejects.toMatchObject({
      failure: { kind: "rejected" },
    });
    expect(transcribe).toHaveBeenCalledTimes(1);
  });

  it("ends an attempt at the timeout and leaves the decision to the user", async () => {
    let attempt!: AbortSignal;
    const transcribe = vi.fn(
      (signal: AbortSignal) =>
        new Promise((_, reject) => {
          attempt = signal;
          signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const result = runVoiceTranscription({ transcribe, timeoutMs: 1_000 });
    const settled = expect(result).rejects.toMatchObject({
      failure: { kind: "timeout" },
    });
    await vi.advanceTimersByTimeAsync(999);
    expect(attempt.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await settled;
    expect(transcribe).toHaveBeenCalledTimes(1);
  });

  it("abandons a stalled upload when the network drops and repeats it", async () => {
    const net = network();
    const stalled = deferred<{ text: string }>();
    const transcribe = vi
      .fn()
      .mockImplementationOnce((signal: AbortSignal) => {
        signal.addEventListener("abort", () =>
          stalled.reject(new DOMException("Aborted", "AbortError")),
        );
        return stalled.promise;
      })
      .mockResolvedValueOnce({ text: "Done." });
    const result = runVoiceTranscription({
      transcribe,
      connectivity: net.connectivity,
    });
    await flush();
    net.set(false);
    await flush();
    expect(transcribe.mock.calls[0]![0].aborted).toBe(true);
    net.set(true);
    await expect(result).resolves.toEqual({ text: "Done." });
  });

  it("stops for good when cancelled", async () => {
    const abort = new AbortController();
    const transcribe = vi.fn().mockRejectedValue(offline());
    const result = runVoiceTranscription({ transcribe, signal: abort.signal });
    await flush();
    abort.abort();
    await expect(result).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(600_000);
    expect(transcribe).toHaveBeenCalledTimes(1);
  });
});

function memoryStore(initial: VoiceClip[] = []) {
  let saved = initial;
  const removed: string[] = [];
  const store: VoiceClipStore = {
    load: vi.fn(async () => saved),
    save: vi.fn(async (clips: VoiceClip[]) => {
      saved = clips;
    }),
    remove: vi.fn(async (clip: VoiceClip) => {
      removed.push(clip.id);
    }),
  };
  return { store, saved: () => saved, removed };
}

function clip(id: string, extra: Partial<VoiceClip> = {}): VoiceClip {
  return {
    id,
    workspaceId: "ws-1",
    target: "chat-a",
    mimeType: "audio/mp4",
    durationMs: 1500,
    bytes: 1000,
    createdAt: new Date().toISOString(),
    ...extra,
  };
}

describe("VoiceTranscriptionQueue", () => {
  it("saves a recording before sending it and delivers in recording order", async () => {
    const memory = memoryStore();
    const first = deferred<{ text: string }>();
    const order: string[] = [];
    const transcribe = vi
      .fn()
      .mockImplementationOnce(() => {
        // The recording is on the device before any request.
        expect(memory.saved().map((item) => item.id)).toEqual(["a"]);
        return first.promise;
      })
      .mockResolvedValueOnce({ text: "Second." });
    const queue = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe,
      deliver: (item, transcript) => {
        order.push(`${item.id}:${transcript.text}`);
      },
    });
    await queue.add(clip("a"));
    await queue.add(clip("b"));
    expect(queue.getSnapshot().map((entry) => entry.status.kind)).toEqual([
      "transcribing",
      "queued",
    ]);
    first.resolve({ text: "First." });
    await flush();
    expect(order).toEqual(["a:First.", "b:Second."]);
    expect(queue.getSnapshot()).toEqual([]);
    expect(memory.saved()).toEqual([]);
    expect(memory.removed).toEqual(["a", "b"]);
  });

  it("keeps recordings across restarts and never sends a restored one", async () => {
    const memory = memoryStore();
    const net = network(false);
    const first = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe: vi.fn(),
      deliver: vi.fn(),
      connectivity: net.connectivity,
    });
    await first.add(clip("a", { send: true }));
    expect(first.getSnapshot()[0]!.status).toMatchObject({
      kind: "waiting",
      failure: { kind: "offline" },
      retryAt: null,
    });
    first.dispose();
    expect(memory.saved().map((item) => item.id)).toEqual(["a"]);

    const deliver = vi.fn();
    const second = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe: vi.fn().mockResolvedValue({ text: "Kept." }),
      deliver,
      connectivity: network(true).connectivity,
    });
    await second.ready;
    await flush();
    expect(deliver).toHaveBeenCalledWith(
      expect.objectContaining({ id: "a", send: false }),
      { text: "Kept." },
    );
    expect(memory.removed).toEqual(["a"]);
  });

  it("pays for a transcript once even when delivering it fails", async () => {
    const memory = memoryStore();
    const transcribe = vi.fn().mockResolvedValue({ text: "Once." });
    const deliver = vi
      .fn()
      .mockRejectedValueOnce(new Error("disk full"))
      .mockResolvedValueOnce(undefined);
    const queue = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe,
      deliver,
    });
    await queue.add(clip("a"));
    await flush();
    expect(queue.getSnapshot()[0]!.status.kind).toBe("failed");
    expect(memory.saved()[0]!.transcript).toEqual({ text: "Once." });
    queue.retry("a");
    await flush();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(deliver).toHaveBeenCalledTimes(2);
    expect(queue.getSnapshot()).toEqual([]);
  });

  it("lets the user retry a refused recording and wakes waiting ones", async () => {
    const memory = memoryStore();
    const transcribe = vi
      .fn()
      .mockRejectedValueOnce(status(409))
      .mockRejectedValueOnce(offline())
      .mockResolvedValueOnce({ text: "Finally." });
    const deliver = vi.fn();
    const queue = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe,
      deliver,
      retryDelaysMs: [60_000],
    });
    await queue.add(clip("a"));
    await flush();
    expect(queue.getSnapshot()[0]!.status).toMatchObject({
      kind: "failed",
      failure: { kind: "rejected" },
    });
    queue.retry("a");
    await flush();
    expect(queue.getSnapshot()[0]!.status.kind).toBe("waiting");
    // Retrying again ends the pause instead of waiting a minute.
    queue.retry();
    await flush();
    expect(deliver).toHaveBeenCalledOnce();
    expect(transcribe).toHaveBeenCalledTimes(3);
  });

  it("discards a recording mid-request without delivering it", async () => {
    const memory = memoryStore();
    let signal!: AbortSignal;
    const deliver = vi.fn();
    const queue = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe: (_clip, attempt) => {
        signal = attempt;
        return new Promise(() => undefined);
      },
      deliver,
    });
    await queue.add(clip("a"));
    await flush();
    await queue.discard("a");
    expect(signal.aborted).toBe(true);
    expect(queue.getSnapshot()).toEqual([]);
    expect(memory.saved()).toEqual([]);
    expect(memory.removed).toEqual(["a"]);
    expect(deliver).not.toHaveBeenCalled();
  });

  it("deletes recordings no one came back for", async () => {
    const old = clip("old", { createdAt: "2020-01-01T00:00:00.000Z" });
    const memory = memoryStore([old, clip("new")]);
    const queue = new VoiceTranscriptionQueue({
      store: memory.store,
      transcribe: () => new Promise(() => undefined),
      deliver: vi.fn(),
    });
    await queue.ready;
    expect(queue.getSnapshot().map((entry) => entry.clip.id)).toEqual(["new"]);
    expect(memory.removed).toEqual(["old"]);
    expect(memory.saved().map((item) => item.id)).toEqual(["new"]);
  });
});
