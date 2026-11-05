import {
  formatEffort,
  providerDisplayName,
  resolveCatalogModel,
  type ProviderModel,
} from "@opencuria/chat-core/harnessModels";
import { ApiRequestError, type OpenCuriaClient } from "@opencuria/client";
import type { ProviderConfig } from "@opencuria/client";
import type { NativeFormOptionGroup } from "../../../ui/NativeForm.types";
import type { SettingsChoice } from "../../SettingsContext";

/** Resource keys shared by the model pages. */
export const NATIVE_MODELS = "models:native";
export const ENGINE_MODELS = "models:engines";
export const PROVIDER_ACCOUNTS = "provider-accounts";
export const PROVIDER_CONFIG = "provider-config";

/** The native catalog; an unavailable catalog is empty, not an error. */
export function loadNativeModels(
  client: OpenCuriaClient,
): Promise<ProviderModel[]> {
  return client.harness.listEngineModels("native").catch(() => []);
}

/** Claude and Codex catalogs, only counted on the provider rows. */
export async function loadEngineModels(
  client: OpenCuriaClient,
): Promise<ProviderModel[][]> {
  return Promise.all(
    ["claude", "codex"].map((engine) =>
      client.harness.listEngineModels(engine).catch(() => []),
    ),
  );
}

/** Organization defaults; a server without stored defaults has none. */
export async function loadProviderConfig(
  client: OpenCuriaClient,
): Promise<ProviderConfig> {
  try {
    return await client.harness.getDefaultConfig();
  } catch (error) {
    if (
      (error instanceof ApiRequestError && error.status === 404) ||
      (error instanceof Error && /not found/i.test(error.message))
    )
      return {
        small_model: "",
        small_effort: "",
        speech_model: "",
        voice_cleanup_model: "",
      };
    throw error;
  }
}

/** The catalog name of a model, its raw id, or `empty` when none is set. */
export function modelLabel(
  models: ProviderModel[],
  id: string,
  empty: string,
): string {
  if (!id.trim()) return empty;
  return resolveCatalogModel(models, id)?.name ?? id;
}

/**
 * A searchable choice of catalog models grouped by provider, optionally led
 * by a "no model" entry such as "Inherit" or "None".
 */
export function modelChoice({
  title,
  models,
  value,
  onSelect,
  none,
}: {
  title: string;
  models: Array<Pick<ProviderModel, "id" | "name" | "provider">>;
  value: string;
  onSelect: (id: string) => void;
  /** Label of the empty choice; omitted when a model is required. */
  none?: string;
}): SettingsChoice {
  const groups = new Map<string, SettingsChoice["groups"][number]>();
  for (const model of models) {
    const provider = providerDisplayName(model.provider) || "Other";
    const group = groups.get(provider) ?? { title: provider, options: [] };
    group.options.push({
      value: model.id,
      label: model.name,
      subtitle: model.id,
    });
    groups.set(provider, group);
  }
  return {
    title,
    value,
    onSelect,
    searchPlaceholder: "Search models",
    emptyText: "No models are available. Connect a provider account first.",
    groups: [
      ...(none ? [{ options: [{ value: "", label: none }] }] : []),
      ...groups.values(),
    ],
  };
}

/** "Provider default" (or `empty`) and the reasoning efforts of a model. */
export function effortGroups(
  model: ProviderModel | undefined,
  empty = "Provider default",
): NativeFormOptionGroup[] {
  return [
    {
      options: [
        { value: "", label: empty },
        ...(model?.reasoning_efforts ?? []).map((effort) => ({
          value: effort,
          label: formatEffort(effort),
        })),
      ],
    },
  ];
}
