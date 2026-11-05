import React, { useEffect, useState } from "react";
import {
  AGENT_HINTS,
  INHERITED_EFFORT_OPTIONS,
  PRIMARY_AGENTS,
  SUBAGENT_IDS,
  agentConfigsValid,
  agentDisplayName,
  agentStatesFromConfigs,
  changedAgentConfigs,
  type AgentConfig,
  type AgentConfigId,
  type AgentState,
  type EffortStrategy,
} from "@opencuria/chat-core/harnessAgents";
import {
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from "@opencuria/chat-core/harnessModels";
import type { ComputerUseConfig } from "@opencuria/client";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { autosaveText, useAutosave, type Autosave } from "../../useAutosave";
import {
  NATIVE_MODELS,
  effortGroups,
  loadNativeModels,
  modelChoice,
  modelLabel,
} from "./catalog";

type States = Record<AgentConfigId, AgentState>;

interface ComputerUseForm {
  maxSteps: string;
  screenshot: string;
  recording: boolean;
}

/** A whole number within the bounds, or `null`. */
export function parseBoundedInteger(
  text: string,
  min: number,
  max: number,
): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const value = Number(text.trim());
  return value >= min && value <= max ? value : null;
}

/** The fields of the Computer Use form that differ from the stored config. */
export function computerUseChanges(
  saved: ComputerUseConfig,
  form: ComputerUseForm,
): Partial<ComputerUseConfig> {
  const changes: Partial<ComputerUseConfig> = {};
  const steps = parseBoundedInteger(form.maxSteps, 1, 1000);
  const size = parseBoundedInteger(form.screenshot, 320, 7680);
  if (steps !== null && steps !== saved.max_steps) changes.max_steps = steps;
  if (size !== null && size !== saved.screenshot_max_dimension)
    changes.screenshot_max_dimension = size;
  if (form.recording !== saved.enable_recording)
    changes.enable_recording = form.recording;
  return changes;
}

/** The most important state of several autosaves, for one shared footer. */
function combined(autosaves: Autosave[]): Autosave {
  const order = ["error", "invalid", "saving", "waiting", "saved"] as const;
  const status =
    order.find((entry) => autosaves.some((item) => item.status === entry)) ??
    "saved";
  return {
    status,
    error: autosaves.find((item) => item.error)?.error ?? null,
    retry: () =>
      autosaves
        .filter((item) => item.status === "error")
        .forEach((item) => item.retry()),
  };
}

/**
 * Agents: models and efforts of the primary agents and subagents, the
 * subagent nesting depth and Computer Use limits; everything saves
 * automatically, as in the webapp.
 */
export function AgentsPage(): React.JSX.Element {
  const { client, resources, onCatalogChanged } = useSettings();
  const navigation = useSettingsNavigation();
  const configs = useResource(resources, "agent-configs", () =>
    client.harness.listAgentConfigs(),
  );
  const depth = useResource(resources, "subagent-config", () =>
    client.agentSettings.getSubagentConfig(),
  );
  const computerUse = useResource(resources, "computer-use-config", () =>
    client.agentSettings.getComputerUseConfig(),
  );
  const native = useResource(resources, NATIVE_MODELS, () =>
    loadNativeModels(client),
  );

  const [loaded, setLoaded] = useState<AgentConfig[] | null>(null);
  const [states, setStates] = useState<States | null>(null);
  const [maxDepth, setMaxDepth] = useState<number | null>(null);
  const [savedComputerUse, setSavedComputerUse] =
    useState<ComputerUseConfig | null>(null);
  const [cu, setCu] = useState<ComputerUseForm | null>(null);
  useEffect(() => {
    if (configs.data && !states) {
      setLoaded(configs.data);
      setStates(agentStatesFromConfigs(configs.data));
    }
  }, [configs.data, states]);
  useEffect(() => {
    if (depth.data && maxDepth === null) setMaxDepth(depth.data.max_depth);
  }, [depth.data, maxDepth]);
  useEffect(() => {
    if (computerUse.data && !cu) {
      setSavedComputerUse(computerUse.data);
      setCu({
        maxSteps: String(computerUse.data.max_steps),
        screenshot: String(computerUse.data.screenshot_max_dimension),
        recording: computerUse.data.enable_recording,
      });
    }
  }, [computerUse.data, cu]);

  const changed = loaded && states ? changedAgentConfigs(loaded, states) : [];
  const agentsSave = useAutosave(
    states,
    async () => {
      const saved = await client.harness.saveAgentConfigs(changed);
      setLoaded(saved);
      configs.set(saved);
      onCatalogChanged();
    },
    {
      valid: agentConfigsValid(changed),
      failure: "Failed to save agent models",
    },
  );
  const depthSave = useAutosave(
    maxDepth,
    async (value) => {
      depth.set(
        await client.agentSettings.saveSubagentConfig({ max_depth: value }),
      );
    },
    { failure: "Failed to save agent settings" },
  );
  const steps = cu ? parseBoundedInteger(cu.maxSteps, 1, 1000) : null;
  const size = cu ? parseBoundedInteger(cu.screenshot, 320, 7680) : null;
  const computerUseSave = useAutosave(
    cu,
    async (value) => {
      const changes = computerUseChanges(savedComputerUse!, value);
      if (!Object.keys(changes).length) return;
      const saved = await client.agentSettings.saveComputerUseConfig(changes);
      setSavedComputerUse(saved);
      computerUse.set(saved);
    },
    {
      valid: steps !== null && size !== null,
      failure: "Failed to save Computer Use settings",
    },
  );

  const catalog = native.data ?? [];
  const setAgent = (agent: AgentConfigId, next: Partial<AgentState>) =>
    setStates((current) =>
      current
        ? { ...current, [agent]: { ...current[agent], ...next } }
        : current,
    );

  const agentRows = (agent: AgentConfigId): NativeFormRow[] => {
    const state = states![agent];
    const primary = PRIMARY_AGENTS.includes(agent);
    const inherit = !primary && state.inherit;
    const model = resolveCatalogModel(catalog, state.model);
    const select = (id: string) => {
      if (!primary && !id.trim()) {
        setAgent(agent, { model: "", effort: "", inherit: true });
        return;
      }
      const next: ProviderModel | undefined = resolveCatalogModel(catalog, id);
      setAgent(agent, {
        model: id,
        inherit: false,
        effort: next ? snapEffort(next, state.effort) : state.effort,
      });
    };
    const name = agentDisplayName(agent);
    return [
      catalog.length
        ? {
            kind: "value",
            id: `${agent}-model`,
            testID: `${agent}-model`,
            label: "Model",
            value: inherit
              ? "Inherit"
              : modelLabel(catalog, state.model, "Select a model"),
            tone: primary && !state.model ? "warning" : "muted",
            onPress: () =>
              navigation.choose(
                modelChoice({
                  title: `${name} model`,
                  models: catalog,
                  value: inherit ? "" : state.model,
                  onSelect: select,
                  none: primary ? undefined : "Inherit",
                }),
              ),
          }
        : {
            kind: "text",
            id: `${agent}-model`,
            label: "Model",
            placeholder: primary
              ? "provider/model-id"
              : "Inherit (provider/model-id)",
            value: inherit ? "" : state.model,
            onChange: select,
          },
      inherit
        ? {
            kind: "picker",
            id: `${agent}-strategy`,
            label: "Effort",
            value: state.strategy,
            groups: [{ options: INHERITED_EFFORT_OPTIONS }],
            onChange: (value) =>
              setAgent(agent, {
                strategy: (value || "inherit") as EffortStrategy,
              }),
          }
        : {
            kind: "picker",
            id: `${agent}-effort`,
            label: "Effort",
            value: state.effort,
            disabled: !model?.reasoning_efforts.length,
            groups: effortGroups(model),
            onChange: (effort) => setAgent(agent, { effort }),
          },
    ];
  };

  const status = combined([agentsSave, depthSave, computerUseSave]);
  const ready = states && maxDepth !== null && cu;
  const sections: NativeFormSection[] = ready
    ? [
        ...PRIMARY_AGENTS.map((agent) => ({
          id: agent,
          title: agentDisplayName(agent),
          footer: !states[agent].model.trim()
            ? `${AGENT_HINTS[agent]} Select a model.`
            : AGENT_HINTS[agent],
          footerTone: states[agent].model.trim()
            ? ("muted" as const)
            : ("warning" as const),
          rows: agentRows(agent),
        })),
        {
          id: "subagents",
          title: "Subagents",
          footer:
            "Main agent is depth 0. Applies to new runs. Subagents inherit the parent's model unless you choose one.",
          rows: [
            {
              kind: "stepper",
              id: "max-depth",
              label: "Maximum nesting depth",
              value: maxDepth,
              min: 1,
              max: 2147483647,
              step: 1,
              format: (value) => String(value),
              onChange: setMaxDepth,
            },
          ],
        },
        ...SUBAGENT_IDS.filter((agent) => agent !== "computeruse").map(
          (agent) => ({
            id: agent,
            title: agentDisplayName(agent),
            footer: AGENT_HINTS[agent],
            rows: agentRows(agent),
          }),
        ),
        {
          id: "computeruse",
          title: "Computer Use",
          footer:
            steps === null
              ? "Enter a step limit from 1 to 1000."
              : size === null
                ? "Enter a screenshot size from 320 to 7680 pixels."
                : "Desktop automation. Activate the Computer Use plugin in a workspace to control its desktop.",
          footerTone: steps === null || size === null ? "danger" : "muted",
          rows: [
            ...agentRows("computeruse"),
            {
              kind: "text",
              id: "max-steps",
              label: "Step limit",
              placeholder: "Step limit (1–1000)",
              keyboard: "number",
              value: cu.maxSteps,
              onChange: (maxSteps) => setCu({ ...cu, maxSteps }),
            },
            {
              kind: "text",
              id: "screenshot-size",
              label: "Maximum screenshot size",
              placeholder: "Pixels (320–7680)",
              keyboard: "number",
              value: cu.screenshot,
              onChange: (screenshot) => setCu({ ...cu, screenshot }),
            },
            {
              kind: "toggle",
              id: "recording",
              label: "Record desktop sessions",
              subtitle: "Save a video of automated desktop activity.",
              value: cu.recording,
              onChange: (recording) => setCu({ ...cu, recording }),
            },
          ],
        },
        {
          id: "save-status",
          footer: autosaveText(status),
          footerTone: status.status === "error" ? "danger" : "muted",
          rows:
            status.status === "error"
              ? [
                  {
                    kind: "button",
                    id: "retry",
                    title: "Retry",
                    symbol: "arrow.clockwise",
                    onPress: status.retry,
                  },
                ]
              : [],
        },
      ]
    : [];

  const error = configs.error ?? depth.error ?? computerUse.error;
  return (
    <SettingsPage
      title="Agents"
      sections={
        error && !ready
          ? loadErrorSections(error, () => {
              void configs.reload();
              void depth.reload();
              void computerUse.reload();
            })
          : sections
      }
      loading={!ready && !error}
      testID="settings-agents"
    />
  );
}
