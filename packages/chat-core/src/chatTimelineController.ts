/**
 * Shared owner of asynchronous transcript loading and retention.
 *
 * Framework adapters expose storage and notification ports. Request identity,
 * stale-response rejection, live/snapshot reconciliation and cache policy live
 * here, so Vue and React execute the same state transitions.
 */
import type { HarnessMessage, HarnessPart, HarnessSession } from "./harness";
import {
  isOptimisticPrompt,
  mergeBusyFetchedMessages,
  messagePosition,
  reconcileOptimisticPrompts,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
} from "./harnessReducer";
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
} from "./harnessSubtaskActivity";
import { withEditedPrompt } from "./optimisticPrompts";
import { hydrateHarnessPart } from "./toolDisplay";

/** The owner changed while transport was in flight; the response is obsolete. */
export class StaleChatRequestError extends Error {
  constructor() {
    super("Conversation changed while loading part details");
  }
}

export interface ChatTimelineSnapshot {
  messages: HarnessMessage[];
  /**
   * Messages preceding the returned ones. Absent when the transport ignored
   * the range and returned every message.
   */
  older_count?: number;
}

/**
 * Messages one request returns, by message position: the newest `limit` of
 * the range from `since` (inclusive) up to `before` (exclusive).
 */
export interface TimelineRange {
  limit?: number;
  before?: number;
  since?: number;
}

export interface TimelineRequest<T> {
  /** Navigation/auth identity. Different contexts never share a request. */
  context?: string;
  variant?: string;
  isCurrent?: () => boolean;
  /** A terminal event requires a snapshot requested after the existing flight. */
  refreshAfterExisting?: boolean;
  /** Reload every message even when a range would do. */
  full?: boolean;
  /** Capture request-scoped metadata before transport starts. */
  onSnapshot?: (snapshot: T) => void | Promise<void>;
  prepare?: () => (snapshot: T) => void | Promise<void>;
}

export interface ChatTimelinePorts<T extends ChatTimelineSnapshot> {
  load: (sessionId: string, range?: TimelineRange) => Promise<T>;
  loadDetail?: (sessionId: string, partId: string) => Promise<HarnessPart>;
  read: (sessionId: string) => HarnessMessage[] | undefined;
  write: (sessionId: string, messages: HarnessMessage[]) => void;
  /** Known sessions of the open scope: current status and parent lineage. */
  sessions: () => HarnessSession[];
  onError?: (error: unknown) => void;
  onSettled?: () => void;
  /** Sessions the adapter still shows or waits on: selection, gates, todo fetches. */
  retainedSessionIds: () => Iterable<string | null | undefined>;
  forget: (sessionId: string) => void;
  /** Vue passes toRaw so identity accounting does not depend on proxies. */
  unwrap?: (value: object) => object;
  detailLimit?: number;
  transcriptLimit?: number;
  transcriptBudgetBytes?: number;
  /**
   * Opt in to ranged loading: a chat opens with its newest `tailLimit`
   * messages, the rest follows in `backfill` pages, and a reload asks only for
   * the messages that can have changed. Without it every load is complete.
   */
  tailLimit?: number;
  /** Messages per backfill page; defaults to 100. */
  pageLimit?: number;
  /** Older messages were added to a transcript outside a fetch. */
  onChange?: (sessionId: string) => void;
}

/** Held messages a reload checks for a reply that is still streaming. */
const RECENT_MESSAGES = 6;

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
  /** Rows an edit replaced, per session; they never come back. */
  private readonly discarded = new Map<string, Set<string>>();
  /** Messages the server holds before the oldest one shown, per session. */
  private readonly older = new Map<string, number>();
  private readonly backfills = new Map<string, Promise<void>>();

  constructor(private readonly ports: ChatTimelinePorts<T>) {}

  generation(sessionId: string): number {
    return this.versions.get(sessionId) ?? 0;
  }

  private status(sessionId: string): HarnessSession["status"] | undefined {
    return this.ports.sessions().find((session) => session.id === sessionId)
      ?.status;
  }

  private pendingSessionIds(): string[] {
    return [
      ...new Set(
        [...this.flights.values(), ...this.detailFlights.values()].map(
          (flight) => flight.sessionId,
        ),
      ),
    ];
  }

  private pinnedSessionIds(): string[] {
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
    const statusAtRequest = this.status(sessionId);
    const onSnapshot = request.prepare?.() ?? request.onSnapshot;
    const current = () =>
      epoch === this.epoch &&
      generation === this.generation(sessionId) &&
      (!request.isCurrent || request.isCurrent());
    const flight: Flight = { sessionId, context, promise: Promise.resolve() };
    flight.promise = (async () => {
      try {
        const range = this.rangeFor(sessionId, request.full);
        let snapshot = await (range
          ? this.ports.load(sessionId, range)
          : this.ports.load(sessionId));
        if (!current()) return;
        let messages = this.joinRange(sessionId, snapshot, range);
        if (!messages) {
          // The messages held no longer match the server's: start over.
          snapshot = await this.ports.load(sessionId);
          if (!current()) return;
          this.older.delete(sessionId);
          messages = snapshot.messages;
        }
        this.apply(sessionId, messages, statusAtRequest);
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

  /**
   * What a load needs to ask for. The first load of a chat takes its newest
   * messages. A reload takes everything from the last two held messages, or
   * from the first recent one still streaming, on: older ones are final.
   */
  private rangeFor(
    sessionId: string,
    full: boolean | undefined,
  ): TimelineRange | undefined {
    const limit = this.ports.tailLimit;
    if (!limit || full) return undefined;
    const held = (this.ports.read(sessionId) ?? [])
      .filter((message) => !isOptimisticPrompt(message))
      .flatMap((message) => {
        const position = messagePosition(message);
        return position === undefined ? [] : [{ message, position }];
      })
      .sort((a, b) => a.position - b.position);
    if (!held.length) return { limit };
    // Only the newest messages can still be streaming; older ones that never
    // finished are leftovers of interrupted runs and stay as they are.
    const recent = held.slice(-RECENT_MESSAGES);
    const streaming = recent.find(
      ({ message }) =>
        message.role === "assistant" && message.completed_at == null,
    );
    return {
      since: Math.min(
        held[Math.max(0, held.length - 2)]!.position,
        streaming?.position ?? Number.POSITIVE_INFINITY,
      ),
    };
  }

  /**
   * The messages a ranged response stands for: every message of the session
   * up to its newest, or null when the held messages contradict the server's
   * count and only a complete load can be trusted.
   */
  private joinRange(
    sessionId: string,
    snapshot: T,
    range: TimelineRange | undefined,
  ): HarnessMessage[] | null {
    if (!range || snapshot.older_count === undefined) {
      this.older.delete(sessionId);
      return snapshot.messages;
    }
    if (range.since === undefined) {
      this.older.set(sessionId, snapshot.older_count);
      return snapshot.messages;
    }
    const retained = (this.ports.read(sessionId) ?? []).filter((message) => {
      const position = messagePosition(message);
      return (
        !isOptimisticPrompt(message) &&
        position !== undefined &&
        position < range.since!
      );
    });
    const missing = this.older.get(sessionId) ?? 0;
    return retained.length + missing === snapshot.older_count
      ? [...retained, ...snapshot.messages]
      : null;
  }

  /** Whether the server holds messages older than those shown. */
  hasOlder(sessionId: string): boolean {
    return (this.older.get(sessionId) ?? 0) > 0;
  }

  /**
   * Load the messages before the oldest one shown, a page at a time, until
   * the whole chat is there. Pages never touch the newer messages. Never
   * rejects: a failed page ends the run and `hasOlder` stays true.
   */
  backfill(
    sessionId: string,
    request: { isCurrent?: () => boolean } = {},
  ): Promise<void> {
    const existing = this.backfills.get(sessionId);
    if (existing) return existing;
    const epoch = this.epoch;
    const promise = (async () => {
      while (this.hasOlder(sessionId)) {
        if (epoch !== this.epoch || (request.isCurrent && !request.isCurrent()))
          return;
        const oldest = this.oldestPosition(sessionId);
        if (oldest === undefined) return;
        const snapshot = await this.ports.load(sessionId, {
          limit: this.ports.pageLimit ?? 100,
          before: oldest,
        });
        // A complete reload or forgotten chat leaves nothing to add.
        if (epoch !== this.epoch || !this.hasOlder(sessionId)) return;
        // The transcript was replaced meanwhile: the page no longer adjoins it.
        if (this.oldestPosition(sessionId) !== oldest) continue;
        if (!this.prepend(sessionId, snapshot)) {
          this.older.delete(sessionId);
          return;
        }
      }
    })()
      // A failed page leaves the shown messages as they are; the next
      // backfill carries on where this one stopped.
      .catch(() => undefined)
      .finally(() => {
        if (this.backfills.get(sessionId) === promise)
          this.backfills.delete(sessionId);
      });
    this.backfills.set(sessionId, promise);
    return promise;
  }

  private oldestPosition(sessionId: string): number | undefined {
    let oldest: number | undefined;
    for (const message of this.ports.read(sessionId) ?? []) {
      const position = messagePosition(message);
      if (position !== undefined && (oldest === undefined || position < oldest))
        oldest = position;
    }
    return oldest;
  }

  /** Put a page of older messages in front; false when it added nothing. */
  private prepend(sessionId: string, snapshot: T): boolean {
    const shown = this.ports.read(sessionId);
    const oldest = this.oldestPosition(sessionId);
    if (!shown || oldest === undefined) return false;
    const known = new Set(shown.map((message) => message.id));
    const page = snapshot.messages.filter(
      (message) =>
        !known.has(message.id) &&
        !this.isDiscarded(sessionId, message.id) &&
        (messagePosition(message) ?? Number.POSITIVE_INFINITY) < oldest,
    );
    if (!page.length) return false;
    this.ports.write(
      sessionId,
      sortHarnessMessages([
        ...page.map((message) => this.hydrate(sessionId, message, new Map())),
        ...shown,
      ]),
    );
    this.older.set(sessionId, snapshot.older_count ?? 0);
    this.touch(sessionId, true);
    this.ports.onChange?.(sessionId);
    return true;
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

  /** A row an edit replaced; its late events must not bring it back. */
  isDiscarded(sessionId: string, messageId?: string): boolean {
    return Boolean(messageId && this.discarded.get(sessionId)?.has(messageId));
  }

  /**
   * Send an edited prompt; `send` performs the request. The transcript ends
   * with the new text at once. The rows it replaces never return: snapshots
   * requested before the server answered lose their authority, and later ones
   * skip them. A rejected edit restores the transcript it replaced.
   */
  async edit<R>(
    sessionId: string,
    messageId: string,
    prompt: { content: string; skillIds?: string[] },
    send: () => Promise<R>,
  ): Promise<R> {
    const previous = this.ports.read(sessionId);
    const edited = previous && withEditedPrompt(previous, messageId, prompt);
    const discarded = this.discarded.get(sessionId) ?? new Set<string>();
    const added = (edited?.replaced ?? [])
      .map((message) => message.id)
      .filter((id) => !discarded.has(id));
    for (const id of added) discarded.add(id);
    if (discarded.size) this.discarded.set(sessionId, discarded);
    // Snapshots in flight, e.g. after Stop, still hold the replaced rows.
    this.invalidate(sessionId);
    this.invalidateDetails(sessionId);
    if (edited) {
      this.ports.write(sessionId, edited.messages);
      this.touch(sessionId, true);
    }
    try {
      const result = await send();
      // A snapshot requested during admission may predate the replacement.
      this.invalidate(sessionId);
      return result;
    } catch (error) {
      this.invalidate(sessionId);
      for (const id of added) discarded.delete(id);
      if (!discarded.size && this.discarded.get(sessionId) === discarded)
        this.discarded.delete(sessionId);
      // Only while the edit is still shown; a forgotten chat stays forgotten.
      const shown = this.ports.read(sessionId);
      if (
        previous &&
        edited &&
        shown?.some((message) => message.id === edited.prompt.id)
      ) {
        this.ports.write(sessionId, previous);
        this.touch(sessionId, true);
      }
      throw error;
    }
  }

  /** A server message as shown: stored details merged in, ids set. */
  private hydrate(
    sessionId: string,
    message: HarnessMessage,
    previousParts: Map<string, HarnessPart>,
  ): HarnessMessage {
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
        .filter((message) => !this.isDiscarded(sessionId, message.id))
        .map((message) => this.hydrate(sessionId, message, previousParts)),
    );
    // Prompts still on their way survive every snapshot until echoed.
    const reconciled =
      this.status(sessionId) === "busy"
        ? mergeBusyFetchedMessages(previous.map(cloneMessage), incoming)
        : reconcileOptimisticPrompts(
            previous,
            statusAtRequest === "busy"
              ? preserveLatestStream(previous, incoming)
              : settleOpenStreamParts(incoming),
          );
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

  /**
   * Keep the lineage family (ancestors and descendants) of every retained, busy,
   * loading or detail-pinned session, so the subtask UI around it stays whole.
   */
  private protectedSessionIds(): Set<string> {
    const sessions = this.ports.sessions();
    const roots = new Set<string>();
    for (const id of this.ports.retainedSessionIds()) if (id) roots.add(id);
    for (const session of sessions)
      if (session.status === "busy") roots.add(session.id);
    for (const id of this.pendingSessionIds()) roots.add(id);
    for (const id of this.pinnedSessionIds()) roots.add(id);
    const protectedIds = new Set<string>();
    for (const id of roots) {
      protectedIds.add(id);
      for (const ancestor of collectAncestorSessions(id, sessions))
        protectedIds.add(ancestor.id);
      for (const descendant of collectDescendantSessionIds(id, sessions))
        protectedIds.add(descendant);
    }
    return protectedIds;
  }

  trim(): void {
    const protectedIds = this.protectedSessionIds();
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
    this.discarded.delete(sessionId);
    this.older.delete(sessionId);
  }

  clear(): void {
    this.epoch += 1;
    this.discarded.clear();
    this.older.clear();
    this.backfills.clear();
    this.versions.clear();
    this.detailVersions.clear();
    this.detailFlights.clear();
    this.flights.clear();
    this.details.clear();
    this.pins.clear();
    this.transcripts.clear();
  }
}
