import React, { useEffect, useMemo, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import {
  formatContextLength,
  formatEffort,
  formatEffortShort,
  providerDisplayName,
  resolveCatalogModel,
  type HarnessEngine,
  type ProviderModel,
} from "@opencuria/chat-core/harnessModels";
import type {
  HarnessId,
  HarnessSessionMode,
} from "@opencuria/chat-core/harness";
import { NativeList, type NativeListSection } from "../ui/NativeList";
import { NativeSheet } from "../ui/NativeSheet";
import { NativeTextField } from "../ui/NativeTextField";
import { PickerPill } from "../ui/PickerPill";
import { NativeSegmentedPicker } from "../ui/selection";
import { useTheme } from "../ui/theme";

/** Matches the webapp model picker: recents first, full catalog on demand. */
const QUICK_MODEL_COUNT = 6;
const MODE_LABELS: Record<HarnessSessionMode, string> = {
  build: "Build",
  plan: "Plan",
};

export interface ModelSheetEngine {
  id: HarnessId;
  name: string;
  /** Supported modes in display order. */
  modes: HarnessSessionMode[];
  disabled?: boolean;
  /** Why the engine cannot be chosen, e.g. "Not connected". */
  note?: string;
}

/** Engines as the sheet offers them; an unconnected engine stays visible but disabled. */
export function modelSheetEngines(
  engines: HarnessEngine[],
): ModelSheetEngine[] {
  return engines.map((item) => ({
    id: item.id,
    name: item.name,
    modes: (["build", "plan"] as const).filter((value) =>
      item.modes.includes(value),
    ),
    disabled: !item.connected,
    note: item.connected ? undefined : "Not connected",
  }));
}

export interface ModelSheetProps {
  visible: boolean;
  onClose: () => void;
  models: ProviderModel[];
  /** Recently used model ids, newest first. */
  recentModelIds: string[];
  model: string;
  effort: string;
  /** Engines offered for this chat; an existing chat offers only its own. */
  engines: ModelSheetEngine[];
  engine: HarnessId;
  mode: HarnessSessionMode;
  /** Shown instead of model rows when the engine catalog is empty. */
  unavailableMessage?: string;
  onModelChange: (id: string) => void;
  onEffortChange: (effort: string) => void;
  onAgentChange: (engine: HarnessId, mode: HarnessSessionMode) => void;
}

/**
 * One sheet for the composer's agent, model and reasoning effort: mode and
 * engine on top, recent models in the middle and the effort pinned below, so
 * every choice is visible without scrolling. The full catalog opens as a
 * search in place.
 */
export function ModelSheet({
  visible,
  onClose,
  models,
  recentModelIds,
  model,
  effort,
  engines,
  engine,
  mode,
  unavailableMessage,
  onModelChange,
  onEffortChange,
  onAgentChange,
}: ModelSheetProps): React.JSX.Element {
  const { theme } = useTheme();
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
  const hasRecent = useMemo(
    () => recentModelIds.some((id) => models.some((item) => item.id === id)),
    [models, recentModelIds],
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
  const closeSearch = () => {
    setShowAll(false);
    setQuery("");
  };
  const modelRow = (item: ProviderModel) => ({
    id: `model-${item.id}`,
    title: item.name,
    subtitle: modelDetail(item),
    selected: item.id === model,
    onPress: () => {
      onModelChange(item.id);
      closeSearch();
    },
  });

  if (showAll)
    return (
      <NativeSheet
        visible={visible}
        onClose={onClose}
        keyboard
        testID="model-sheet"
      >
        <View style={styles.search}>
          <NativeTextField
            label="Search models"
            value={query}
            onChangeText={setQuery}
            placeholder="Search models"
          />
        </View>
        <NativeList
          sections={[
            {
              id: "back",
              rows: [
                {
                  id: "recent-models",
                  title: "Recent models",
                  symbol: "chevron.left",
                  onPress: closeSearch,
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
          ]}
        />
      </NativeSheet>
    );

  const current = engines.find((item) => item.id === engine);
  const modes = current?.modes ?? [];
  const selectEngine = (id: string) => {
    const next = engines.find((item) => item.id === id);
    if (!next || next.disabled || next.id === engine) return;
    onAgentChange(
      next.id,
      next.modes.includes(mode) ? mode : (next.modes[0] ?? mode),
    );
  };
  const modelSection: NativeListSection = models.length
    ? {
        id: "model",
        title: hasRecent ? "Recent" : "Models",
        rows: [
          ...quick.map(modelRow),
          ...(models.length > quick.length
            ? [
                {
                  id: "show-all-models",
                  title: "All models",
                  subtitle: `${models.length} available`,
                  symbol: "magnifyingglass" as const,
                  onPress: () => setShowAll(true),
                },
              ]
            : []),
        ],
      }
    : {
        id: "model",
        rows: [
          {
            id: "models-unavailable",
            title: unavailableMessage ?? "No models available",
          },
        ],
      };
  const efforts = selected?.reasoning_efforts ?? [];

  return (
    <NativeSheet visible={visible} onClose={onClose} testID="model-sheet">
      {modes.length > 1 || engines.length > 1 ? (
        <View style={styles.agent}>
          {modes.length > 1 ? (
            <View style={styles.modes}>
              <NativeSegmentedPicker
                label="Mode"
                value={mode}
                options={modes.map((value) => ({
                  value,
                  label: MODE_LABELS[value],
                }))}
                onChange={(value) =>
                  onAgentChange(engine, value as HarnessSessionMode)
                }
                testID="model-sheet-mode"
              />
            </View>
          ) : null}
          {engines.length > 1 ? (
            <PickerPill
              value={current?.name ?? engine}
              accessibilityLabel={`Engine: ${current?.name ?? engine}`}
              menu={{
                title: "Engine",
                actions: engines.map((item) => ({
                  id: item.id,
                  title: item.name,
                  subtitle: item.note,
                  disabled: item.disabled,
                  selected: item.id === engine,
                })),
                onSelect: selectEngine,
              }}
              testID="model-sheet-engine"
            />
          ) : null}
        </View>
      ) : null}
      <NativeList plain sections={[modelSection]} />
      {efforts.length ? (
        <View style={[styles.effort, { borderTopColor: theme.border }]}>
          <View style={styles.effortHeader}>
            <Text style={[styles.effortTitle, { color: theme.text }]}>
              Effort
            </Text>
            {effort ? (
              <Text style={[styles.effortValue, { color: theme.primary }]}>
                {formatEffort(effort)}
              </Text>
            ) : null}
          </View>
          <NativeSegmentedPicker
            label="Effort"
            value={effort}
            options={efforts.map((value) => ({
              value,
              label: formatEffortShort(value),
              accessibilityLabel: formatEffort(value),
            }))}
            onChange={onEffortChange}
            testID="model-sheet-effort"
          />
        </View>
      ) : null}
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

const styles = StyleSheet.create({
  search: { paddingHorizontal: 16, paddingBottom: 4 },
  agent: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 16,
    paddingBottom: 8,
  },
  modes: { flex: 1 },
  effort: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 12,
    gap: 10,
  },
  effortHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "baseline",
    paddingHorizontal: 4,
  },
  effortTitle: { fontSize: 15, fontWeight: "600" },
  effortValue: { fontSize: 15 },
});
