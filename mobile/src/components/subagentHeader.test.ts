import { describe, expect, it } from "vitest";
import type { HarnessSession } from "@opencuria/chat-core/harness";
import { subagentHeader } from "./subagentHeader";

function session(
  id: string,
  overrides: Partial<HarnessSession> = {},
): HarnessSession {
  return {
    id,
    workspace_id: "workspace",
    title: id,
    mode: "build",
    agent_name: "build",
    model: "model",
    status: "idle",
    cost: 0,
    tokens: {},
    ...overrides,
  };
}

const root = session("root", { title: "Audit auth flow" });
const explorer = session("explorer", {
  parent_id: "root",
  title: "Map the auth middleware",
  agent_name: "explore",
  status: "busy",
});

describe("subagentHeader", () => {
  it("leaves top-level chats with their regular header", () => {
    expect(subagentHeader(null, [])).toBeNull();
    expect(subagentHeader(root, [root])).toBeNull();
  });

  it("names the subagent, its type and state, and the chat it returns to", () => {
    expect(subagentHeader(explorer, [root, explorer])).toEqual({
      parentId: "root",
      rootId: null,
      title: "Map the auth middleware",
      subtitle: "Explorer · Working…",
      backTitle: "Audit auth flow",
    });
  });

  it("offers the top-level chat below a nested subagent", () => {
    const nested = session("nested", {
      parent_id: "explorer",
      title: "  ",
      agent_name: "general",
    });
    expect(subagentHeader(nested, [root, explorer, nested])).toEqual({
      parentId: "explorer",
      rootId: "root",
      title: "General",
      subtitle: "General",
      backTitle: "Map the auth middleware",
    });
  });

  it("still leads back when the parent is not loaded", () => {
    const orphan = session("orphan", { parent_id: "gone", agent_name: "" });
    expect(subagentHeader(orphan, [orphan])).toMatchObject({
      parentId: "gone",
      rootId: null,
      subtitle: "Subagent",
      backTitle: "parent chat",
    });
  });
});
