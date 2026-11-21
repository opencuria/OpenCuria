/**
 * Optimistic prompts: a sent message shows in the transcript at once, before
 * the server has stored it.
 *
 * The row has a local id (`local-user-*`) until a snapshot holds the server's
 * copy; `reconcileOptimisticPrompts` then hands its `client_key` to that copy.
 * Lists key rows by `messageRenderKey`, so the bubble is never replaced.
 */
import type { HarnessId, HarnessMessage } from "./harness";
import { isLocalId, isOptimisticPrompt } from "./harnessReducer";

let fallbackSequence = 0;

function localSuffix(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (typeof uuid === "string" && uuid) return uuid;
  } catch {
    // Fall through to the counter below.
  }
  fallbackSequence += 1;
  return `${Date.now().toString(36)}-${fallbackSequence}`;
}

/** The transcript row for a prompt that is on its way to the server. */
export function createOptimisticPrompt(input: {
  sessionId: string;
  content: string;
  skillIds?: string[];
  harnessId?: HarnessId;
  now?: Date;
}): HarnessMessage {
  const id = `local-user-${localSuffix()}`;
  return {
    id,
    client_key: id,
    session_id: input.sessionId,
    ...(input.harnessId ? { harness_id: input.harnessId } : {}),
    role: "user",
    // The server stores prompts stripped; its echo must read the same.
    content: input.content.trim(),
    skill_ids: [...(input.skillIds ?? [])],
    parts: [],
    created_at: (input.now ?? new Date()).toISOString(),
  };
}

/** List key of a row: a sent prompt keeps its key once the server has it. */
export function messageRenderKey(message: HarnessMessage): string {
  return message.client_key ?? message.id;
}

/** A prompt sent from this client, on its way or already stored. */
export function isClientPrompt(message: HarnessMessage): boolean {
  return message.role === "user" && isLocalId(messageRenderKey(message));
}

/**
 * Drop a prompt the server did not accept. A prompt it already stored has a
 * server id and stays.
 */
export function withoutOptimisticPrompt(
  messages: HarnessMessage[],
  promptId: string,
): HarnessMessage[] {
  return messages.some(
    (message) => message.id === promptId && isOptimisticPrompt(message),
  )
    ? messages.filter((message) => message.id !== promptId)
    : messages;
}
