import type { HarnessSession } from "@opencuria/chat-core/harness";
import { formatSubagentType } from "@opencuria/chat-core/harnessSubtaskActivity";

/** What the chat header shows while a subagent's conversation is open. */
export interface SubagentHeader {
  /** Conversation the back button returns to. */
  parentId: string;
  /** Top-level chat, set only when the subagent runs below another subagent. */
  rootId: string | null;
  title: string;
  /** Agent type, plus "Working…" while it runs. */
  subtitle: string;
  /** Parent chat's name for the back button. */
  backTitle: string;
}

/**
 * Header of a subagent conversation; `null` for top-level chats.
 * `lineage` runs from the top-level chat to `session`.
 */
export function subagentHeader(
  session: HarnessSession | null,
  lineage: readonly HarnessSession[],
): SubagentHeader | null {
  const parentId = session?.parent_id;
  if (!session || !parentId) return null;
  const parent = lineage.find((row) => row.id === parentId);
  const root = lineage[0];
  const agent = formatSubagentType(session.agent_name) ?? "Subagent";
  return {
    parentId,
    rootId:
      root && root.id !== parentId && root.id !== session.id ? root.id : null,
    title: session.title.trim() || agent,
    subtitle: session.status === "busy" ? `${agent} · Working…` : agent,
    backTitle: parent?.title.trim() || "parent chat",
  };
}
