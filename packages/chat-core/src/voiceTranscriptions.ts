/**
 * Transcription that survives a lost connection, shared by the web and native
 * composers. A recording is never thrown away because the network failed:
 * `runVoiceTranscription` waits for the connection and tries again, and
 * `VoiceTranscriptionQueue` keeps recordings in client storage until their
 * text has reached the draft, also across app restarts.
 */
import {
  VOICE_OFFLINE_MESSAGE,
  VOICE_TRANSCRIBE_TIMEOUT_MS,
  isVoiceAbort,
  voiceFailure,
  type VoiceFailure,
} from "./voiceInput";

/** Text of a recording as the server returns it (see `@opencuria/client`). */
export interface VoiceTranscriptText {
  text: string;
  /** The text is the verbatim transcript because cleanup failed. */
  cleanup_warning?: string;
  /** Timed source phrases; kept for recordings with a visual timeline. */
  segments?: Array<{
    start_ms: number;
    end_ms: number;
    text: string;
    raw_text?: string;
  }>;
  timing?: "provider" | "approximate";
  raw_text?: string;
  speech_model?: string;
  cleanup_model?: string;
  /** The saved timeline of a recording with visual references. */
  recording_path?: string;
}

/**
 * The device's network as the platform reports it. `watch` listeners get
 * `true` whenever retrying now is worthwhile: the network came back or the
 * app returned to the foreground.
 */
export interface VoiceConnectivity {
  online(): boolean;
  watch(listener: (online: boolean) => void): () => void;
}

/** A pause before the next attempt; `retryAt` is null while offline. */
export interface VoiceWait {
  failure: VoiceFailure;
  retryAt: number | null;
}

/** A transcription that needs the user: retry or discard. */
export class VoiceTranscriptionError extends Error {
  readonly failure: VoiceFailure;

  constructor(failure: VoiceFailure) {
    super(failure.message);
    this.name = "VoiceTranscriptionError";
    this.failure = failure;
  }
}

/** Pauses after failed attempts; the last one repeats. */
export const VOICE_RETRY_DELAYS_MS: readonly number[] = [
  2_000, 5_000, 15_000, 30_000, 60_000,
];
/** Automatic attempts after the speech service failed, before asking the user. */
export const VOICE_SERVICE_RETRIES = 3;

export interface VoiceTranscriptionOptions<T> {
  /** One attempt; `signal` aborts it. */
  transcribe: (signal: AbortSignal) => Promise<T>;
  /** Cancels the transcription; it then rejects with the abort reason. */
  signal?: AbortSignal;
  connectivity?: VoiceConnectivity | null;
  /** A pause began (`VoiceWait`) or an attempt starts (`null`). */
  onWait?: (wait: VoiceWait | null) => void;
  timeoutMs?: number;
  retryDelaysMs?: readonly number[];
  serviceRetries?: number;
  now?: () => number;
}

function abortError(): Error {
  const error = new Error("Voice transcription was cancelled.");
  error.name = "AbortError";
  return error;
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? abortError();
}

/** Resolve after `delay` ms (never when null) or a connectivity nudge. */
function pause(
  delay: number | null,
  connectivity: VoiceConnectivity | null | undefined,
  signal: AbortSignal | undefined,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unwatch: (() => void) | undefined;
    const finish = (error?: unknown) => {
      if (timer !== undefined) clearTimeout(timer);
      unwatch?.();
      signal?.removeEventListener("abort", onAbort);
      if (error === undefined) resolve();
      else reject(error);
    };
    const onAbort = () => finish(abortReason(signal!));
    if (signal?.aborted) return onAbort();
    signal?.addEventListener("abort", onAbort, { once: true });
    if (delay !== null) timer = setTimeout(() => finish(), delay);
    unwatch = connectivity?.watch((online) => {
      if (online) finish();
    });
  });
}

/**
 * Transcribe one recording, retrying until it succeeds, is cancelled or
 * needs the user. Lost connections are retried without limit, as soon as
 * the network returns and otherwise after growing pauses; a busy service a
 * few times; anything else rejects with `VoiceTranscriptionError`. An
 * attempt that is in flight when the device goes offline is abandoned and
 * repeated, since a stalled upload would otherwise only end at the timeout.
 */
export async function runVoiceTranscription<T>(
  options: VoiceTranscriptionOptions<T>,
): Promise<T> {
  const {
    signal,
    connectivity,
    onWait,
    timeoutMs = VOICE_TRANSCRIBE_TIMEOUT_MS,
    retryDelaysMs = VOICE_RETRY_DELAYS_MS,
    serviceRetries = VOICE_SERVICE_RETRIES,
    now = Date.now,
  } = options;
  let failures = 0;
  let serviceFailures = 0;
  for (;;) {
    if (signal?.aborted) throw abortReason(signal);
    if (connectivity && !connectivity.online()) {
      onWait?.({
        failure: { kind: "offline", message: VOICE_OFFLINE_MESSAGE },
        retryAt: null,
      });
      await pause(null, connectivity, signal);
      continue;
    }
    onWait?.(null);
    const attempt = new AbortController();
    const ended: { cause: "timeout" | "offline" | null } = { cause: null };
    const onAbort = () => attempt.abort(abortReason(signal!));
    signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      ended.cause = "timeout";
      attempt.abort(abortError());
    }, timeoutMs);
    const unwatch = connectivity?.watch((online) => {
      if (online) return;
      ended.cause = "offline";
      attempt.abort(abortError());
    });
    try {
      return await options.transcribe(attempt.signal);
    } catch (error) {
      if (signal?.aborted) throw abortReason(signal);
      const failure: VoiceFailure =
        ended.cause === "timeout"
          ? voiceFailure({ status: 0 })
          : ended.cause === "offline" || isVoiceAbort(error)
            ? { kind: "offline", message: VOICE_OFFLINE_MESSAGE }
            : voiceFailure(error);
      const retry =
        failure.kind === "offline" ||
        (failure.kind === "unavailable" && serviceFailures++ < serviceRetries);
      if (!retry) throw new VoiceTranscriptionError(failure);
      const delay =
        retryDelaysMs[Math.min(failures++, retryDelaysMs.length - 1)] ?? 0;
      onWait?.({ failure, retryAt: now() + delay });
      // Ends early when the network returns or the user retries.
      await pause(delay, connectivity, signal);
    } finally {
      clearTimeout(timer);
      unwatch?.();
      signal?.removeEventListener("abort", onAbort);
    }
  }
}

/** A recording waiting for its text. `target` says where the text goes. */
export interface VoiceClip<Target = string> {
  id: string;
  workspaceId: string;
  target: Target;
  mimeType: string;
  durationMs: number;
  bytes: number;
  /** ISO time of the recording. */
  createdAt: string;
  /**
   * Send the text right away instead of leaving it in the draft. Only the
   * session that recorded the clip sends; a restored clip never does.
   */
  send?: boolean;
  /** Kept once known, so a delivery that did not finish never pays twice. */
  transcript?: VoiceTranscriptText;
  /**
   * Visual references recorded with the audio (frames, taps, logs of an open
   * app), stored by the client next to the audio; `attach` saves them.
   */
  visual?: { kind: "app"; recordingId: string };
}

export type VoiceClipStatus =
  | { kind: "queued" }
  | { kind: "transcribing" }
  | {
      kind: "waiting";
      failure: VoiceFailure;
      /** Null while offline: the clip goes once the network is back. */
      retryAt: number | null;
    }
  | { kind: "failed"; failure: VoiceFailure };

export interface VoiceClipEntry<Target = string> {
  clip: VoiceClip<Target>;
  status: VoiceClipStatus;
}

/** Where a client keeps the clips and their audio. */
export interface VoiceClipStore<Target = string> {
  load(): Promise<VoiceClip<Target>[]>;
  /** Replace the saved list. */
  save(clips: VoiceClip<Target>[]): Promise<void>;
  /** Delete the clip's audio. */
  remove(clip: VoiceClip<Target>): Promise<void>;
}

export interface VoiceQueuePorts<Target = string> {
  store: VoiceClipStore<Target>;
  /** One attempt for a clip; reads its audio and asks the server. */
  transcribe: (
    clip: VoiceClip<Target>,
    signal: AbortSignal,
  ) => Promise<VoiceTranscriptText>;
  /** Hand the text to the clip's target; the clip is deleted afterwards. */
  deliver: (
    clip: VoiceClip<Target>,
    transcript: VoiceTranscriptText,
  ) => Promise<void> | void;
  /**
   * One attempt to store what a clip recorded beside its audio (frames, a
   * timeline) once its text is known; resolves to the transcript with
   * `recording_path`. Retried like transcription; skipped for clips that
   * need none (resolve to the transcript unchanged).
   */
  attach?: (
    clip: VoiceClip<Target>,
    transcript: VoiceTranscriptText,
    signal: AbortSignal,
  ) => Promise<VoiceTranscriptText>;
  connectivity?: VoiceConnectivity | null;
}

export interface VoiceQueueOptions {
  /** Clips older than this are deleted when the queue starts. */
  maxAgeMs?: number;
  now?: () => number;
  timeoutMs?: number;
  retryDelaysMs?: readonly number[];
}

const DAY_MS = 24 * 60 * 60 * 1000;
/** Recordings no one came back for go after a month, like drafts. */
export const VOICE_CLIP_MAX_AGE_MS = 30 * DAY_MS;

/**
 * Recordings waiting for their text, transcribed one at a time in recording
 * order so dictations into one draft keep their order. The list is saved
 * before any request, and a clip leaves storage only once its text was
 * delivered or the user discarded it. Snapshots are immutable.
 */
export class VoiceTranscriptionQueue<Target = string> {
  /** Settles once the saved clips are back in the queue. */
  readonly ready: Promise<void>;
  private entries: VoiceClipEntry<Target>[] = [];
  private readonly listeners = new Set<() => void>();
  private readonly nudges = new Set<(online: boolean) => void>();
  private running: { id: string; abort: AbortController } | null = null;
  private disposed = false;
  private saving: Promise<void> = Promise.resolve();
  private readonly connectivity: VoiceConnectivity;
  private readonly now: () => number;

  constructor(
    private readonly ports: VoiceQueuePorts<Target>,
    private readonly options: VoiceQueueOptions = {},
  ) {
    this.now = options.now ?? Date.now;
    const external = ports.connectivity ?? null;
    // Manual retries wake a waiting attempt like a returning network does.
    this.connectivity = {
      online: () => external?.online() ?? true,
      watch: (listener) => {
        this.nudges.add(listener);
        const unwatch = external?.watch(listener);
        return () => {
          this.nudges.delete(listener);
          unwatch?.();
        };
      },
    };
    this.ready = this.restore();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly VoiceClipEntry<Target>[] => this.entries;

  /**
   * Queue a finished recording; it is saved before it is sent anywhere. A
   * disposed queue still saves it, for the next queue of its account.
   */
  async add(clip: VoiceClip<Target>): Promise<void> {
    await this.ready;
    this.publish([...this.entries, { clip, status: { kind: "queued" } }]);
    await this.persist();
    this.pump();
  }

  /** Try a failed clip again, or every waiting clip now instead of later. */
  retry(id?: string): void {
    if (this.disposed) return;
    let changed = false;
    const next = this.entries.map((entry) => {
      if (entry.status.kind !== "failed" || (id && entry.clip.id !== id))
        return entry;
      changed = true;
      return { ...entry, status: { kind: "queued" } as const };
    });
    if (changed) this.publish(next);
    for (const nudge of [...this.nudges]) nudge(true);
    this.pump();
  }

  /** Delete a clip and its audio, also while it is being transcribed. */
  async discard(id: string): Promise<void> {
    const entry = this.entries.find((item) => item.clip.id === id);
    if (!entry) return;
    if (this.running?.id === id) {
      this.running.abort.abort();
      this.running = null;
    }
    this.publish(this.entries.filter((item) => item.clip.id !== id));
    await this.persist();
    await this.ports.store.remove(entry.clip).catch(() => undefined);
    this.pump();
  }

  /** Stop working; saved clips stay for the next queue of this account. */
  dispose(): void {
    this.disposed = true;
    this.running?.abort.abort();
    this.running = null;
    this.listeners.clear();
  }

  private async restore(): Promise<void> {
    let clips: VoiceClip<Target>[] = [];
    try {
      clips = await this.ports.store.load();
    } catch {
      return;
    }
    const maxAge = this.options.maxAgeMs ?? VOICE_CLIP_MAX_AGE_MS;
    const now = this.now();
    const expired = clips.filter(
      (clip) => !(now - Date.parse(clip.createdAt) < maxAge),
    );
    if (this.disposed) return;
    this.publish([
      ...clips
        .filter((clip) => !expired.includes(clip))
        .map((clip) => ({
          clip: { ...clip, send: false },
          status: { kind: "queued" } as const,
        })),
      ...this.entries,
    ]);
    if (expired.length) {
      await this.persist();
      await Promise.all(
        expired.map((clip) =>
          this.ports.store.remove(clip).catch(() => undefined),
        ),
      );
    }
    this.pump();
  }

  private publish(entries: VoiceClipEntry<Target>[]): void {
    this.entries = entries;
    for (const listener of [...this.listeners]) listener();
  }

  private update(id: string, change: Partial<VoiceClipEntry<Target>>): void {
    if (!this.entries.some((entry) => entry.clip.id === id)) return;
    this.publish(
      this.entries.map((entry) =>
        entry.clip.id === id ? { ...entry, ...change } : entry,
      ),
    );
  }

  /** Saves run one after another, each with the list as it is then. */
  private persist(): Promise<void> {
    const run = this.saving.then(() =>
      this.ports.store.save(this.entries.map((entry) => entry.clip)),
    );
    this.saving = run.catch(() => undefined);
    return run.catch(() => undefined);
  }

  private pump(): void {
    if (this.running || this.disposed) return;
    const next = this.entries.find((entry) => entry.status.kind === "queued");
    if (!next) return;
    const abort = new AbortController();
    this.running = { id: next.clip.id, abort };
    void this.process(next.clip.id, abort.signal).finally(() => {
      if (this.running?.abort === abort) this.running = null;
      this.pump();
    });
  }

  private async process(id: string, signal: AbortSignal): Promise<void> {
    const current = () =>
      this.entries.find((entry) => entry.clip.id === id)?.clip;
    try {
      let transcript = current()?.transcript;
      if (!transcript) {
        transcript = await runVoiceTranscription({
          transcribe: (attempt) => this.ports.transcribe(current()!, attempt),
          signal,
          connectivity: this.connectivity,
          timeoutMs: this.options.timeoutMs,
          retryDelaysMs: this.options.retryDelaysMs,
          now: this.now,
          onWait: (wait) =>
            this.update(id, {
              status: wait
                ? { kind: "waiting", ...wait }
                : { kind: "transcribing" },
            }),
        });
        if (signal.aborted || !current()) return;
        this.update(id, { clip: { ...current()!, transcript } });
        await this.persist();
      }
      const attach = this.ports.attach;
      if (attach && transcript.text.trim() && !transcript.recording_path) {
        const known = transcript;
        transcript = await runVoiceTranscription({
          transcribe: (attempt) => attach(current()!, known, attempt),
          signal,
          connectivity: this.connectivity,
          timeoutMs: this.options.timeoutMs,
          retryDelaysMs: this.options.retryDelaysMs,
          now: this.now,
          onWait: (wait) =>
            this.update(id, {
              status: wait
                ? { kind: "waiting", ...wait }
                : { kind: "transcribing" },
            }),
        });
        if (signal.aborted || !current()) return;
        if (transcript !== known) {
          this.update(id, { clip: { ...current()!, transcript } });
          await this.persist();
        }
      }
      if (signal.aborted || !current()) return;
      await this.ports.deliver(current()!, transcript);
      const clip = current();
      if (!clip) return;
      this.publish(this.entries.filter((entry) => entry.clip.id !== id));
      await this.persist();
      await this.ports.store.remove(clip).catch(() => undefined);
    } catch (error) {
      if (signal.aborted || !current()) return;
      this.update(id, {
        status: {
          kind: "failed",
          failure:
            error instanceof VoiceTranscriptionError
              ? error.failure
              : {
                  kind: "rejected",
                  message:
                    "The text could not be added to your draft. Retry or discard the recording.",
                },
        },
      });
    }
  }
}
