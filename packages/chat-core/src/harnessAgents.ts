/**
 * Agent defaults shared by the web and native settings: which agents can be
 * configured, how a subagent inherits its model and effort, and which rows
 * a save has to send.
 */

export type AgentMode = "primary" | "subagent";
export type EffortStrategy =
  | "fixed"
  | "inherit"
  | "lowest"
  | "medium"
  | "highest";

/** Stored defaults of one agent, as returned by `GET /agent-configs/`. */
export interface AgentConfig {
  agent: string;
  mode: AgentMode;
  description: string;
  model: string;
  effort: string;
  inherit_model: boolean;
  effort_strategy: EffortStrategy;
}

export interface AgentConfigInput {
  agent: string;
  description?: string;
  model?: string;
  effort?: string;
  inherit_model?: boolean;
  effort_strategy?: string;
}

export type AgentConfigId =
  | "build"
  | "plan"
  | "general"
  | "explore"
  | "computeruse";

export const CONFIGURABLE_AGENTS: AgentConfigId[] = [
  "build",
  "plan",
  "general",
  "explore",
  "computeruse",
];

export const PRIMARY_AGENTS: AgentConfigId[] = ["build", "plan"];

export const SUBAGENT_IDS: AgentConfigId[] = [
  "general",
  "explore",
  "computeruse",
];

export interface EffortStrategyOption {
  value: EffortStrategy;
  label: string;
  hint: string;
}

export const EFFORT_STRATEGIES: EffortStrategyOption[] = [
  { value: "fixed", label: "Fixed", hint: "Always use the configured effort." },
  { value: "inherit", label: "Inherit", hint: "Use the parent run effort." },
  {
    value: "lowest",
    label: "Lowest",
    hint: "Use the lowest supported effort.",
  },
  {
    value: "medium",
    label: "Medium",
    hint: "Use the middle supported effort.",
  },
  {
    value: "highest",
    label: "Highest",
    hint: "Use the highest supported effort.",
  },
];

/** Effort choices of a subagent that inherits its parent's model. */
export const INHERITED_EFFORT_OPTIONS: Array<{
  value: EffortStrategy;
  label: string;
}> = [
  { value: "inherit", label: "Inherit" },
  { value: "lowest", label: "Lowest" },
  { value: "medium", label: "Medium" },
  { value: "highest", label: "Highest" },
];

/** One-line purpose of each configurable agent. */
export const AGENT_HINTS: Record<AgentConfigId, string> = {
  build: "Write and run code.",
  plan: "Plan before making changes.",
  general: "Delegated tasks.",
  explore: "Read-only research.",
  computeruse: "Desktop automation.",
};

const AGENT_DISPLAY_NAMES: Record<string, string> = {
  build: "Build",
  plan: "Plan",
  general: "General",
  explore: "Explore",
  computeruse: "Computer Use",
};

/** Human-readable label for an agent id. */
export function agentDisplayName(agent: string): string {
  return AGENT_DISPLAY_NAMES[agent] ?? agent;
}

/** True when the mode (or config) describes a subagent. */
export function isSubagent(
  modeOrConfig: AgentMode | string | Pick<AgentConfig, "mode">,
): boolean {
  const mode =
    typeof modeOrConfig === "string" ? modeOrConfig : (modeOrConfig.mode ?? "");
  return mode === "subagent";
}

/**
 * Preview the strategy-resolved effort for a model's effort list.
 * Mirrors `backend/apps/harness/agents/effort_strategy.py`:
 * lowest = efforts[0], medium = efforts[floor(n/2)], highest = last,
 * anything else (fixed/inherit/empty) resolves to ''.
 */
export function resolvePreviewEffort(
  efforts: string[],
  strategy: string,
): string {
  if (!efforts || efforts.length === 0) return "";
  switch (strategy) {
    case "lowest":
      return efforts[0] ?? "";
    case "medium":
      return efforts[Math.floor(efforts.length / 2)] ?? "";
    case "highest":
      return efforts[efforts.length - 1] ?? "";
    default:
      return "";
  }
}

/** Editable defaults of one agent; a subagent may inherit its parent's model. */
export interface AgentState {
  model: string;
  effort: string;
  inherit: boolean;
  /** Effort strategy while inheriting. */
  strategy: EffortStrategy;
}

/** The editable state of every configurable agent; unstored subagents inherit. */
export function agentStatesFromConfigs(
  configs: AgentConfig[],
): Record<AgentConfigId, AgentState> {
  const byAgent = new Map(configs.map((config) => [config.agent, config]));
  return Object.fromEntries(
    CONFIGURABLE_AGENTS.map((agent) => {
      const config = byAgent.get(agent);
      return [
        agent,
        {
          model: config?.model ?? "",
          effort: config?.effort ?? "",
          inherit: config?.inherit_model ?? SUBAGENT_IDS.includes(agent),
          strategy:
            config?.effort_strategy && config.effort_strategy !== "fixed"
              ? config.effort_strategy
              : "inherit",
        },
      ];
    }),
  ) as Record<AgentConfigId, AgentState>;
}

/**
 * The rows a save has to send: only agents whose model, effort, inheritance
 * or strategy differ from the stored (or default) configuration.
 */
export function changedAgentConfigs(
  loaded: AgentConfig[],
  states: Record<AgentConfigId, AgentState>,
): AgentConfigInput[] {
  const byAgent = new Map(loaded.map((config) => [config.agent, config]));
  return CONFIGURABLE_AGENTS.map((agent): AgentConfigInput => {
    const current = states[agent];
    const inherit = !PRIMARY_AGENTS.includes(agent) && current.inherit;
    return {
      agent,
      model: inherit ? "" : current.model.trim(),
      effort: inherit ? "" : current.effort.trim(),
      inherit_model: inherit,
      effort_strategy: inherit ? current.strategy : "fixed",
    };
  }).filter((next) => {
    const previous = byAgent.get(next.agent);
    const inherited =
      previous?.inherit_model ??
      SUBAGENT_IDS.includes(next.agent as AgentConfigId);
    return (
      next.model !== (previous?.model ?? "") ||
      next.effort !== (previous?.effort ?? "") ||
      next.inherit_model !== inherited ||
      next.effort_strategy !==
        (previous?.effort_strategy ?? (inherited ? "inherit" : "fixed"))
    );
  });
}

/** A save is valid when every changed primary agent names a model. */
export function agentConfigsValid(changed: AgentConfigInput[]): boolean {
  return changed.every(
    (config) =>
      !PRIMARY_AGENTS.includes(config.agent as AgentConfigId) ||
      Boolean(config.model),
  );
}

/** Subagent nesting depth: a whole number from 1 to 2147483647, or `null`. */
export function parseMaxDepth(raw: string | number): number | null {
  const text = String(raw).trim();
  const value = Number(text);
  return /^\d+$/.test(text) &&
    Number.isSafeInteger(value) &&
    value >= 1 &&
    value <= 2147483647
    ? value
    : null;
}
