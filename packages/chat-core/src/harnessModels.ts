/**
 * Multi-provider catalog helpers for the composer model/effort picker.
 */

import type { HarnessId, HarnessSession } from "./harness";

export type ProviderId =
  | "openrouter"
  | "chatgpt"
  | "amazon-bedrock"
  | "openai-compatible";

export interface ProviderModel {
  id: string;
  name: string;
  provider?: string;
  reasoning_efforts: string[];
  default_effort: string;
  supports_tools: boolean;
  context_length: number;
  max_output_tokens: number;
}

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  openrouter: "OpenRouter",
  chatgpt: "ChatGPT",
  "amazon-bedrock": "Amazon Bedrock",
  "openai-compatible": "OpenAI Compatible",
  claude: "Claude",
};

/** Human-readable provider label for catalog rows and tooltips. */
export function providerDisplayName(provider?: string): string {
  if (!provider) return "";
  return PROVIDER_DISPLAY_NAMES[provider] ?? provider;
}

export const EFFORT_LABELS: Record<string, string> = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra High",
  max: "Max",
  ultra: "Ultra",
};

/** Compact labels for controls that show every effort side by side. */
export const EFFORT_SHORT_LABELS: Record<string, string> = {
  minimal: "Min",
  medium: "Med",
  xhigh: "X-High",
};

/** Human-readable label for a reasoning effort token. */
export function formatEffort(effort: string): string {
  const key = effort.trim().toLowerCase();
  return EFFORT_LABELS[key] ?? effort;
}

/** Compact effort label, e.g. for a segmented control; falls back to the full label. */
export function formatEffortShort(effort: string): string {
  return (
    EFFORT_SHORT_LABELS[effort.trim().toLowerCase()] ?? formatEffort(effort)
  );
}

/** Compact context size label (128000 → "128k"); empty when unknown. */
export function formatContextLength(contextLength: number): string {
  if (!Number.isFinite(contextLength) || contextLength <= 0) return "";
  if (contextLength >= 1_000_000) {
    const millions = contextLength / 1_000_000;
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}m`;
  }
  if (contextLength >= 1_000) return `${Math.round(contextLength / 1_000)}k`;
  return String(contextLength);
}

/**
 * Keep the current effort when the model still supports it; otherwise fall
 * back to the model's default (or first) effort.
 */
export function snapEffort(
  model: ProviderModel | undefined,
  current: string,
): string {
  if (!model || model.reasoning_efforts.length === 0) return "";
  const token = current.trim().toLowerCase();
  if (token && model.reasoning_efforts.includes(token)) return token;
  if (
    model.default_effort &&
    model.reasoning_efforts.includes(model.default_effort)
  ) {
    return model.default_effort;
  }
  return model.reasoning_efforts[0] ?? "";
}

/** Catalog row for the selected model id (empty model id returns undefined – Auto mode removed). */
export function resolveCatalogModel(
  models: ProviderModel[],
  modelId: string,
  defaultModel = "",
): ProviderModel | undefined {
  const effective = modelId.trim() || defaultModel.trim();
  if (!effective) return undefined;
  return models.find((item) => item.id === effective);
}

/** Compact "Model Effort" label for conversation cards (empty model id yields '' – Auto mode removed). */
export function formatHarnessModelEffort(
  modelId: string,
  effort: string,
  models: ProviderModel[],
  defaultModel = "",
): string {
  const trimmedModel = modelId.trim() || defaultModel.trim();
  if (!trimmedModel) return "";
  const modelName =
    resolveCatalogModel(models, trimmedModel)?.name ?? trimmedModel;
  const effortLabel = effort.trim() ? formatEffort(effort) : "";
  return effortLabel ? `${modelName} ${effortLabel}` : modelName;
}

/** Engine discovery DTO shared by web and mobile. Native uses agent defaults. */
export interface HarnessEngine {
  id: HarnessId;
  name: string;
  modes: string[];
  connected: boolean;
  default_model: string | null;
  default_effort: string | null;
}

export function harnessEngineLabel(
  id: string,
  engines: HarnessEngine[],
): string {
  return engines.find((engine) => engine.id === id)?.name ?? id;
}

/** Native defaults always come from the current mode's agent config. */
export function harnessEngineDefaults(
  id: HarnessId,
  engines: HarnessEngine[],
  native: { model: string; effort: string } = { model: "", effort: "" },
): { model: string; effort: string } {
  if (id === "native") return native;
  const engine = engines.find((engine) => engine.id === id);
  return {
    model: engine?.default_model ?? "",
    effort: engine?.default_effort ?? "",
  };
}

/**
 * Model and effort a new chat or task starts with for an engine and mode:
 * the engine's defaults (native: the mode's agent config), else the first
 * catalog model, with the effort snapped to what that model supports.
 */
export function harnessAgentDefaults(
  engine: HarnessId,
  mode: string,
  {
    engines,
    agentConfigs,
    models,
  }: {
    engines: HarnessEngine[];
    agentConfigs: Array<{ agent: string; model?: string; effort?: string }>;
    /** The engine's model catalog. */
    models: ProviderModel[];
  },
): { model: string; effort: string } {
  const config = agentConfigs.find((item) => item.agent === mode);
  const defaults = harnessEngineDefaults(engine, engines, {
    model: config?.model ?? "",
    effort: config?.effort ?? "",
  });
  const selected =
    models.find((item) => item.id === defaults.model) ?? models[0];
  return {
    model: selected?.id ?? defaults.model,
    effort: selected ? snapEffort(selected, defaults.effort) : defaults.effort,
  };
}

/** The session model is the public routing binding; never derive it from a draft model. */
export function sessionProvider(model: string): string {
  const separator = model.indexOf("/");
  return separator > 0 ? model.slice(0, separator) : "";
}

/** Compatibility is supplied by the engine catalog, not guessed from model names. */
export function harnessSessionModels(
  models: ProviderModel[],
  engine: HarnessId,
  boundModel?: string | null,
): ProviderModel[] {
  if (engine !== "codex" || boundModel == null) return models;
  const provider = sessionProvider(boundModel);
  return provider ? models.filter((model) => model.provider === provider) : [];
}

/** Reject routing changes before sending/editing an existing conversation. */
export function assertHarnessSessionSelection(
  session: Pick<HarnessSession, "harness_id" | "model">,
  selection: { harnessId?: HarnessId; model?: string },
): void {
  const engine = session.harness_id ?? "native";
  if (selection.harnessId && selection.harnessId !== engine) {
    throw new Error("Conversation harness cannot be changed");
  }
  if (
    engine === "codex" &&
    selection.model &&
    sessionProvider(selection.model) !== sessionProvider(session.model)
  ) {
    throw new Error("Codex conversation provider cannot be changed");
  }
}
