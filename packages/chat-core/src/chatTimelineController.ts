/**
 * Shared owner of asynchronous transcript loading and retention.
 *
 * Framework adapters expose storage and notification ports. Request identity,
 * stale-response rejection, live/snapshot reconciliation and cache policy live
 * here, so Vue and React execute the same state transitions.
 */
import type { HarnessMessage, HarnessPart, HarnessSession } from "./harness";
import {
  mergeBusyFetchedMessages,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
} from "./harnessReducer";
import { hydrateHarnessPart } from "./toolDisplay";

/** The owner changed while transport was in flight; the response is obsolete. */
export class StaleChatRequestError extends Error {
  constructor() {
    super("Conversation changed while loading part details");
  }
}

export interface ChatTimelineSnapshot {
  messages: HarnessMessage[];
}

export interface TimelineRequest<T> {
  /** Navigation/auth identity. Different contexts never share a request. */
  context?: string;
  variant?: string;
  isCurrent?: () => boolean;
  /** A terminal event requires a snapshot requested after the existing flight. */
  refreshAfterExisting?: boolean;
  /** Capture request-scoped metadata before transport starts. */
  onSnapshot?: (snapshot: T) => void | Promise<void>;
  prepare?: () => (snapshot: T) => void | Promise<void>;
}

export interface ChatTimelinePorts<T extends ChatTimelineSnapshot> {
  load: (sessionId: string) => Promise<T>;
  loadDetail?: (sessionId: string, partId: string) => Promise<HarnessPart>;
  read: (sessionId: string) => HarnessMessage[] | undefined;
  write: (sessionId: string, messages: HarnessMessage[]) => void;
  status: (sessionId: string) => HarnessSession["status"] | undefined;
  discardMessage?: (sessionId: string, messageId: string) => boolean;
  onError?: (error: unknown) => void;
  onSettled?: () => void;
  /** All adapter-specific pins (selection, busy sessions, gates, todo fetches). */
  protectedSessions: () => Set<string>;
  forget: (sessionId: string) => void;
  /** Vue passes toRaw so identity accounting does not depend on proxies. */
  unwrap?: (value: object) => object;
  detailLimit?: number;
  transcriptLimit?: number;
  transcriptBudgetBytes?: number;
}

interface Flight {
  sessionId: string;
  context: string;
  promise: Promise<void>;
}

function clonePart(part: HarnessPart): HarnessPart {
  return {
    ...part,
    ...(part.input ? { input: { ...part.input } } : {}),
    ...(part.meta ? { meta: { ...part.meta } } : {}),
  };
}

function cloneMessage(message: HarnessMessage): HarnessMessage {
  return { ...message, parts: message.parts.map(clonePart) };
}

/** Retain the latest live answer until a post-idle snapshot has arrived. */
function preserveLatestStream(
  previous: HarnessMessage[],
  incoming: HarnessMessage[],
): HarnessMessage[] {
  const live = [...previous]
    .reverse()
    .find((message) => message.role === "assistant");
  if (!live) return settleOpenStreamParts(incoming);
  const index = incoming.findIndex((message) => message.id === live.id);
  const next = [...incoming];
  if (index < 0) {
    if (live.completed_at == null) next.push(cloneMessage(live));
  } else {
    const server = cloneMessage(next[index]!);
    if (server.completed_at == null) {
      if (server.content.length < live.content.length)
        server.content = live.content;
      const parts = new Map(server.parts.map((part) => [part.id, part]));
      for (const part of live.parts) {
        const stored = parts.get(part.id);
        if (!stored) server.parts.push(clonePart(part));
        else if (stored.output.length < part.output.length)
          Object.assign(stored, clonePart(part));
      }
      server.parts = sortHarnessParts(server.parts);
      server.model ||= live.model;
      server.reasoning_effort ||= live.reasoning_effort;
      next[index] = server;
    }
  }
  return sortHarnessMessages(next);
}

export class ChatTimelineController<T extends ChatTimelineSnapshot> {
  private epoch = 0;
  private readonly versions = new Map<string, number>();
  private readonly flights = new Map<string, Flight>();
  private readonly detailVersions = new Map<string, number>();
  private readonly detailFlights = new Map<
    string,
    { sessionId: string; context: string; promise: Promise<HarnessPart> }
  >();
  private readonly details = new Map<string, HarnessPart>();
  private readonly pins = new Set<string>();
  private readonly transcripts = new Map<string, number>();

  constructor(private readonly ports: ChatTimelinePorts<T>) {}

  generation(sessionId: string): number {
    return this.versions.get(sessionId) ?? 0;
  }

  pendingSessionIds(): string[] {
    return [
      ...new Set(
        [...this.flights.values(), ...this.detailFlights.values()].map(
          (flight) => flight.sessionId,
        ),
      ),
    ];
  }

  pinnedSessionIds(): string[] {
    return [
      ...new Set(
        [...this.pins].map((key) => key.slice(0, key.lastIndexOf(":"))),
      ),
    ];
  }

  /** Invalidate without cancelling transport; late results have no authority. */
  invalidate(sessionId: string): void {
    this.versions.set(sessionId, this.generation(sessionId) + 1);
    for (const [key, flight] of this.flights) {
      if (flight.sessionId === sessionId) this.flights.delete(key);
    }
  }

  async fetch(
    sessionId: string,
    request: TimelineRequest<T> = {},
  ): Promise<void> {
    const context = request.context ?? "";
    const key = `${sessionId}:${request.variant ?? ""}`;
    const existing = this.flights.get(key);
    if (existing?.context === context) {
      if (!request.refreshAfterExisting) return existing.promise;
      await existing.promise.catch(() => undefined);
      if (request.isCurrent && !request.isCurrent()) return;
      return this.fetch(sessionId, { ...request, refreshAfterExisting: false });
    }
    if (request.isCurrent && !request.isCurrent()) return;
    const epoch = this.epoch;
    const generation = this.generation(sessionId) + 1;
    this.versions.set(sessionId, generation);
    const statusAtRequest = this.ports.status(sessionId);
    const onSnapshot = request.prepare?.() ?? request.onSnapshot;
    const current = () =>
      epoch === this.epoch &&
      generation === this.generation(sessionId) &&
      (!request.isCurrent || request.isCurrent());
    const flight: Flight = { sessionId, context, promise: Promise.resolve() };
    flight.promise = (async () => {
      try {
        const snapshot = await this.ports.load(sessionId);
        if (!current()) return;
        this.apply(sessionId, snapshot.messages, statusAtRequest);
        await onSnapshot?.(snapshot);
      } catch (error) {
        if (!current()) return;
        if (this.ports.onError) this.ports.onError(error);
        else throw error;
      }
    })().finally(() => {
      if (this.flights.get(key) === flight) this.flights.delete(key);
      this.trim();
      this.ports.onSettled?.();
    });
    this.flights.set(key, flight);
    return flight.promise;
  }

  /** Refresh only after every pre-terminal request for this session settles. */
  async refreshAfterIdle(
    sessionId: string,
    request: TimelineRequest<T> = {},
  ): Promise<void> {
    await Promise.allSettled(
      [...this.flights.values()]
        .filter((flight) => flight.sessionId === sessionId)
        .map((flight) => flight.promise),
    );
    if (!request.isCurrent || request.isCurrent())
      await this.fetch(sessionId, request);
  }

  private apply(
    sessionId: string,
    messages: HarnessMessage[],
    statusAtRequest: HarnessSession["status"] | undefined,
  ): void {
    const previous = this.ports.read(sessionId) ?? [];
    const previousParts = new Map(
      previous.flatMap((message) =>
        message.parts.map((part) => [part.id, part] as const),
      ),
    );
    const incoming = sortHarnessMessages(
      messages
        .filter(
          (message) => !this.ports.discardMessage?.(sessionId, message.id),
        )
        .map((message) => {
          const parts = (message.parts ?? []).map((raw) => {
            const prior = previousParts.get(raw.id);
            const cached = prior?.detail_loaded
              ? prior
              : this.cachedDetail(sessionId, raw.id);
            const merged = cached
              ? {
                  ...raw,
                  input: cached.input,
                  output: cached.output,
                  meta: cached.meta,
                  detail_loaded: true,
                  ...(cached.display ? { display: cached.display } : {}),
                }
              : raw;
            return hydrateHarnessPart({
              ...merged,
              session_id: sessionId,
              message_id: message.id,
            });
          });
          const content =
            message.role === "assistant" && !message.content
              ? parts
                  .filter((part) => part.type === "text")
                  .map((part) => part.output)
                  .join("")
              : message.content;
          return { ...message, content, session_id: sessionId, parts };
        }),
    );
    const reconciled =
      this.ports.status(sessionId) === "busy"
        ? mergeBusyFetchedMessages(previous.map(cloneMessage), incoming)
        : statusAtRequest === "busy"
          ? preserveLatestStream(previous, incoming)
          : settleOpenStreamParts(incoming);
    for (const message of reconciled) {
      message.session_id = sessionId;
      for (const part of message.parts) {
        part.session_id = sessionId;
        part.message_id = message.id;
      }
    }
    this.ports.write(sessionId, reconciled);
    // Remember after publication: eviction must also lighten this new snapshot.
    for (const message of reconciled) {
      for (const part of message.parts) this.rememberDetail(sessionId, part);
    }
    this.touch(sessionId, true);
  }

  /** Load details under the same freshness and immutable storage contract. */
  async requestDetail(
    sessionId: string,
    partId: string,
    request: {
      context?: string;
      isCurrent?: () => boolean;
    } = {},
  ): Promise<HarnessPart> {
    if (request.isCurrent && !request.isCurrent())
      throw new Error("Part is outside the active conversation");
    const cached = this.cachedDetail(sessionId, partId);
    if (cached) return cached;
    const key = `${sessionId}:${partId}`;
    const context = request.context ?? "";
    const existing = this.detailFlights.get(key);
    if (existing?.context === context) return existing.promise;
    const find = () =>
      this.ports
        .read(sessionId)
        ?.flatMap((message) => message.parts)
        .find((part) => part.id === partId);
    const part = find();
    if (!part) throw new Error("Part is no longer available");
    if (part.state === "pending" || part.state === "running")
      throw new Error("Details are not available until the part is complete");
    if (part.detail_loaded) {
      this.rememberDetail(sessionId, part);
      return clonePart(part);
    }
    if (!this.ports.loadDetail)
      throw new Error("Part detail transport is not configured");
    const version = this.detailVersions.get(sessionId) ?? 0;
    const epoch = this.epoch;
    const flight = { sessionId, context, promise: Promise.resolve(part) };
    flight.promise = (async () => {
      const detail = await this.ports.loadDetail!(sessionId, partId);
      if (
        epoch !== this.epoch ||
        version !== (this.detailVersions.get(sessionId) ?? 0) ||
        (request.isCurrent && !request.isCurrent())
      )
        throw new StaleChatRequestError();
      const current = find();
      if (
        !current ||
        current.state === "pending" ||
        current.state === "running"
      )
        throw new Error("Part changed while loading details");
      const merged = {
        ...current,
        ...detail,
        session_id: sessionId,
        message_id: current.message_id,
        detail_loaded: true,
      };
      const messages = this.ports.read(sessionId)!;
      this.ports.write(
        sessionId,
        messages.map((message) =>
          !message.parts.some((item) => item.id === partId)
            ? message
            : {
                ...message,
                parts: message.parts.map((item) =>
                  item.id === partId ? clonePart(merged) : item,
                ),
              },
        ),
      );
      this.rememberDetail(sessionId, merged);
      this.touch(sessionId, true);
      return clonePart(merged);
    })().finally(() => {
      if (this.detailFlights.get(key) === flight)
        this.detailFlights.delete(key);
      this.trimDetails();
      this.trim();
    });
    this.detailFlights.set(key, flight);
    return flight.promise;
  }

  invalidateDetails(sessionId: string, clear = false, lighten = false): void {
    this.detailVersions.set(
      sessionId,
      (this.detailVersions.get(sessionId) ?? 0) + 1,
    );
    for (const [key, flight] of this.detailFlights)
      if (flight.sessionId === sessionId) this.detailFlights.delete(key);
    if (clear) this.clearDetails(sessionId, lighten);
  }

  cachedDetail(sessionId: string, partId: string): HarnessPart | undefined {
    const key = `${sessionId}:${partId}`;
    const part = this.details.get(key);
    if (part) {
      this.details.delete(key);
      this.details.set(key, part);
    }
    return part && clonePart(part);
  }

  pinDetail(sessionId: string, partId: string, pinned: boolean): void {
    const key = `${sessionId}:${partId}`;
    if (pinned) {
      this.pins.add(key);
      this.cachedDetail(sessionId, partId);
    } else {
      this.pins.delete(key);
      this.trimDetails();
      this.trim();
    }
  }

  rememberDetail(sessionId: string, part: HarnessPart): void {
    if (
      !part.detail_loaded ||
      !["tool", "reasoning", "patch", "compaction"].includes(part.type)
    )
      return;
    const key = `${sessionId}:${part.id}`;
    this.details.delete(key);
    this.details.set(key, clonePart({ ...part, session_id: sessionId }));
    this.trimDetails();
  }

  private lighten(sessionId: string, partId: string): void {
    const messages = this.ports.read(sessionId);
    if (!messages) return;
    this.ports.write(
      sessionId,
      messages.map((message) => {
        if (
          !message.parts.some(
            (part) => part.id === partId && part.detail_loaded,
          )
        )
          return message;
        return {
          ...message,
          parts: message.parts.map((part) =>
            part.id !== partId
              ? part
              : {
                  ...part,
                  input: part.type === "tool" ? { tool: part.tool ?? "" } : {},
                  output:
                    part.state === "error" && part.output === "Tool failed"
                      ? part.output
                      : "",
                  meta:
                    typeof part.meta?.step === "number"
                      ? { step: part.meta.step }
                      : {},
                  detail_loaded: false,
                },
          ),
        };
      }),
    );
  }

  private trimDetails(): void {
    while (this.details.size > (this.ports.detailLimit ?? 100)) {
      const key = [...this.details.keys()].find(
        (candidate) =>
          !this.pins.has(candidate) && !this.detailFlights.has(candidate),
      );
      if (!key) break;
      const part = this.details.get(key)!;
      this.details.delete(key);
      this.lighten(part.session_id, part.id);
    }
  }

  clearDetails(sessionId: string, lighten = false): void {
    for (const [key, part] of this.details) {
      if (part.session_id !== sessionId) continue;
      this.details.delete(key);
      this.pins.delete(key);
      if (lighten) this.lighten(sessionId, part.id);
    }
    for (const key of this.pins)
      if (key.startsWith(`${sessionId}:`)) this.pins.delete(key);
  }

  touch(sessionId: string, recalculate = false): void {
    const messages = this.ports.read(sessionId);
    if (!messages) return;
    const bytes =
      recalculate || !this.transcripts.has(sessionId)
        ? this.estimateBytes(messages)
        : this.transcripts.get(sessionId)!;
    this.transcripts.delete(sessionId);
    this.transcripts.set(sessionId, bytes);
  }

  private estimateBytes(messages: HarnessMessage[]): number {
    const stack: Array<{ value: unknown; multiplier: number }> = [
      { value: messages, multiplier: 1 },
    ];
    const visited = new WeakMap<object, number>();
    let bytes = 0;
    while (stack.length) {
      const { value, multiplier } = stack.pop()!;
      if (typeof value === "string") bytes += value.length * 2 * multiplier;
      else if (
        typeof value === "number" ||
        typeof value === "boolean" ||
        typeof value === "bigint"
      )
        bytes += 8 * multiplier;
      else if (value && typeof value === "object") {
        const raw = this.ports.unwrap?.(value) ?? value;
        const previous = visited.get(raw) ?? 0;
        if (previous >= multiplier) continue;
        const detailed =
          !Array.isArray(raw) &&
          "detail_loaded" in raw &&
          raw.detail_loaded === true &&
          "type" in raw &&
          ["tool", "reasoning", "patch", "compaction"].includes(
            String(raw.type),
          );
        const effective = detailed ? multiplier * 2 : multiplier;
        visited.set(raw, effective);
        if (Array.isArray(raw)) {
          bytes += (24 + raw.length * 8) * (effective - previous);
          for (const item of raw)
            stack.push({ value: item, multiplier: effective });
        } else {
          bytes += 32 * (effective - previous);
          for (const [key, item] of Object.entries(raw)) {
            bytes += (key.length * 2 + 8) * (effective - previous);
            stack.push({ value: item, multiplier: effective });
          }
        }
      }
    }
    return bytes;
  }

  trim(): void {
    const protectedIds = this.ports.protectedSessions();
    for (const id of this.pendingSessionIds()) protectedIds.add(id);
    for (const id of this.pinnedSessionIds()) protectedIds.add(id);
    const candidates = [...this.transcripts].filter(
      ([id]) => !protectedIds.has(id),
    );
    let count = candidates.length;
    let bytes = candidates.reduce((total, [, size]) => total + size, 0);
    for (const [id, size] of candidates) {
      if (
        count <= (this.ports.transcriptLimit ?? 4) &&
        bytes <= (this.ports.transcriptBudgetBytes ?? 16 * 1024 * 1024)
      )
        break;
      this.forget(id);
      this.ports.forget(id);
      count -= 1;
      bytes -= size;
    }
  }

  forget(sessionId: string): void {
    this.invalidate(sessionId);
    this.invalidateDetails(sessionId, true);
    this.transcripts.delete(sessionId);
  }

  clear(): void {
    this.epoch += 1;
    this.versions.clear();
    this.detailVersions.clear();
    this.detailFlights.clear();
    this.flights.clear();
    this.details.clear();
    this.pins.clear();
    this.transcripts.clear();
  }
}
