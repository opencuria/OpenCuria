import React, { useEffect, useMemo, useState } from "react";
import { View } from "react-native";
import {
  formatContextLength,
  formatEffort,
  providerDisplayName,
  resolveCatalogModel,
  type ProviderModel,
} from "@opencuria/chat-core/harnessModels";
import type {
  HarnessId,
  HarnessSessionMode,
} from "@opencuria/chat-core/harness";
import type { NativeSymbol } from "../ui/controls.types";
import { NativeList, type NativeListSection } from "../ui/NativeList";
import { NativeSheet } from "../ui/NativeSheet";
import { NativeTextField } from "../ui/NativeTextField";

/** Matches the webapp model picker: recents first, full catalog on demand. */
const QUICK_MODEL_COUNT = 6;

export interface ModelSheetAgent {
  engine: HarnessId;
  mode: HarnessSessionMode;
  label: string;
  disabled?: boolean;
  /** Why the agent cannot be chosen, e.g. a disconnected engine. */
  note?: string;
}

export interface ModelSheetProps {
  visible: boolean;
  onClose: () => void;
  models: ProviderModel[];
  /** Recently used model ids, newest first. */
  recentModelIds: string[];
  model: string;
  effort: string;
  agents: ModelSheetAgent[];
  engine: HarnessId;
  mode: HarnessSessionMode;
  /** Shown instead of model rows when the engine catalog is empty. */
  unavailableMessage?: string;
  onModelChange: (id: string) => void;
  onEffortChange: (effort: string) => void;
  onAgentChange: (engine: HarnessId, mode: HarnessSessionMode) => void;
}

/** One sheet for the composer's model, reasoning effort and agent choices. */
export function ModelSheet({
  visible,
  onClose,
  models,
  recentModelIds,
  model,
  effort,
  agents,
  engine,
  mode,
  unavailableMessage,
  onModelChange,
  onEffortChange,
  onAgentChange,
}: ModelSheetProps): React.JSX.Element {
  const [showAll, setShowAll] = useState(false);
  const [query, setQuery] = useState("");
  useEffect(() => {
    if (visible) return;
    setShowAll(false);
    setQuery("");
  }, [visible]);

  const selected = resolveCatalogModel(models, model);
  const quick = useMemo(
    () => quickModels(models, recentModelIds, model),
    [models, recentModelIds, model],
  );
  const term = query.trim().toLowerCase();
  const matching = useMemo(
    () =>
      term
        ? models.filter((item) =>
            [item.name, item.id, providerDisplayName(item.provider)]
              .join(" ")
              .toLowerCase()
              .includes(term),
          )
        : models,
    [models, term],
  );
  const modelRow = (item: ProviderModel) => ({
    id: `model-${item.id}`,
    title: item.name,
    subtitle: modelDetail(item),
    selected: item.id === model,
    onPress: () => {
      onModelChange(item.id);
      setShowAll(false);
      setQuery("");
    },
  });

  const sections: NativeListSection[] = showAll
    ? [
        {
          id: "back",
          rows: [
            {
              id: "recent-models",
              title: "Recent models",
              symbol: "chevron.left",
              onPress: () => {
                setShowAll(false);
                setQuery("");
              },
            },
          ],
        },
        {
          id: "all-models",
          title: "All models",
          rows: matching.length
            ? matching.map(modelRow)
            : [{ id: "no-models-found", title: "No models found" }],
        },
      ]
    : [
        {
          id: "model",
          title: "Model",
          rows: models.length
            ? [
                ...quick.map(modelRow),
                ...(models.length > quick.length
                  ? [
                      {
                        id: "show-all-models",
                        title: "All models",
                        subtitle: `${models.length} available`,
                        symbol: "list.bullet" as const,
                        onPress: () => setShowAll(true),
                      },
                    ]
                  : []),
              ]
            : [
                {
                  id: "models-unavailable",
                  title: unavailableMessage ?? "No models available",
                },
              ],
        },
        ...(selected?.reasoning_efforts.length
          ? [
              {
                id: "effort",
                title: "Effort",
                rows: selected.reasoning_efforts.map((value) => ({
                  id: `effort-${value}`,
                  title: formatEffort(value),
                  symbol: effortSymbol(value),
                  selected: value === effort,
                  onPress: () => onEffortChange(value),
                })),
              },
            ]
          : []),
        {
          id: "agent",
          title: "Agent",
          rows: agents.map((agent) => ({
            id: `agent-${agent.engine}-${agent.mode}`,
            title: agent.label,
            subtitle: agent.note,
            symbol:
              agent.mode === "plan"
                ? ("list.bullet.clipboard" as const)
                : ("hammer" as const),
            selected: agent.engine === engine && agent.mode === mode,
            disabled: agent.disabled,
            onPress: () => onAgentChange(agent.engine, agent.mode),
          })),
        },
      ];

  return (
    <NativeSheet
      visible={visible}
      onClose={onClose}
      keyboard={showAll}
      testID="model-sheet"
    >
      {showAll ? (
        <View style={{ paddingHorizontal: 16, paddingBottom: 4 }}>
          <NativeTextField
            label="Search models"
            value={query}
            onChangeText={setQuery}
            placeholder="Search models"
          />
        </View>
      ) : null}
      <NativeList sections={sections} />
    </NativeSheet>
  );
}

/**
 * Recents, then catalog order, capped like the webapp picker. A selection from
 * the full catalog leads the list so the current choice is always visible.
 */
function quickModels(
  models: ProviderModel[],
  recentIds: string[],
  selectedId: string,
): ProviderModel[] {
  const byId = new Map(models.map((item) => [item.id, item]));
  const ids: string[] = [];
  for (const id of [...recentIds, ...models.map((item) => item.id)]) {
    if (ids.length >= QUICK_MODEL_COUNT) break;
    if (byId.has(id) && !ids.includes(id)) ids.push(id);
  }
  if (byId.has(selectedId) && !ids.includes(selectedId))
    ids.splice(0, ids.length, selectedId, ...ids.slice(0, -1));
  return ids.map((id) => byId.get(id)!);
}

function modelDetail(item: ProviderModel): string {
  const context = formatContextLength(item.context_length);
  return [
    providerDisplayName(item.provider),
    context ? `${context} context` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function effortSymbol(effort: string): NativeSymbol {
  switch (effort) {
    case "none":
      return "circle.slash";
    case "minimal":
      return "gauge.with.dots.needle.0percent";
    case "low":
      return "gauge.with.dots.needle.33percent";
    case "medium":
      return "gauge.with.dots.needle.50percent";
    case "high":
      return "gauge.with.dots.needle.67percent";
    default:
      return "gauge.with.dots.needle.100percent";
  }
}
