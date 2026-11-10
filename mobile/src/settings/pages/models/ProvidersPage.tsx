import React, { useEffect, useState } from "react";
import {
  resolveCatalogModel,
  snapEffort,
} from "@opencuria/chat-core/harnessModels";
import {
  PROVIDER_ACCOUNT_PROVIDERS,
  providerAccountsSummary,
  providerModelCount,
} from "@opencuria/chat-core/providerAccounts";
import type { ProviderConfig } from "@opencuria/client";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { autosaveSection, loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { useAutosave } from "../../useAutosave";
import {
  ENGINE_MODELS,
  NATIVE_MODELS,
  PROVIDER_ACCOUNTS,
  PROVIDER_CONFIG,
  effortGroups,
  loadEngineModels,
  loadNativeModels,
  loadProviderConfig,
  modelChoice,
  modelLabel,
} from "./catalog";

type DefaultsForm = Required<ProviderConfig>;

function toForm(config: ProviderConfig): DefaultsForm {
  return {
    small_model: config.small_model ?? "",
    small_effort: config.small_effort ?? "",
    speech_model: config.speech_model ?? "",
    voice_cleanup_model: config.voice_cleanup_model ?? "",
  };
}

/**
 * Provider & Models: the provider accounts, then (for admins) the
 * organization's small model and dictation models, which save automatically.
 */
export function ProvidersPage(): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const accounts = useResource(resources, PROVIDER_ACCOUNTS, () =>
    client.providerAccounts.list(),
  );
  const native = useResource(resources, NATIVE_MODELS, () =>
    loadNativeModels(client),
  );
  const engines = useResource(resources, ENGINE_MODELS, () =>
    loadEngineModels(client),
  );
  // Organization-wide defaults are admin settings; members only see accounts.
  const config = useResource(
    resources,
    PROVIDER_CONFIG,
    () => loadProviderConfig(client),
    { enabled: isAdmin },
  );
  const voice = useResource(
    resources,
    "voice-settings",
    () => client.agentSettings.getVoiceSettings(),
    { enabled: isAdmin },
  );
  const [form, setForm] = useState<DefaultsForm | null>(null);
  useEffect(() => {
    if (config.data && !form) setForm(toForm(config.data));
  }, [config.data, form]);

  const autosave = useAutosave(
    form,
    async (value) => {
      const saved = await client.harness.saveDefaultConfig({
        small_model: value.small_model.trim(),
        small_effort: value.small_effort.trim(),
        speech_model: value.speech_model.trim(),
        voice_cleanup_model: value.voice_cleanup_model.trim(),
      });
      config.set(saved);
    },
    { enabled: isAdmin, failure: "Failed to save default models" },
  );

  const catalog = native.data ?? [];
  const edit = (next: Partial<DefaultsForm>) =>
    setForm((current) => (current ? { ...current, ...next } : current));
  const all = accounts.data ?? [];
  const connected = all.some(
    (account) => account.is_active && account.connected,
  );

  const providerRows: NativeFormRow[] = PROVIDER_ACCOUNT_PROVIDERS.map(
    (provider) => {
      const own = all.filter((account) => account.provider === provider.id);
      const models = providerModelCount(provider, [
        catalog,
        ...(engines.data ?? []),
      ]);
      return {
        kind: "link",
        id: `provider-${provider.id}`,
        testID: `provider-${provider.id}`,
        title: provider.name,
        subtitle: providerAccountsSummary(provider, own),
        subtitleTone: own.some((account) => account.is_active)
          ? "success"
          : "muted",
        value: models ? `${models} models` : undefined,
        route: route("provider", provider.id),
      };
    },
  );

  const modelRow = (
    id: string,
    label: string,
    value: string,
    none: string,
    onSelect: (model: string) => void,
    options = catalog,
  ): NativeFormRow =>
    options.length
      ? {
          kind: "value",
          id,
          label,
          value: modelLabel(options as typeof catalog, value, none),
          onPress: () =>
            navigation.choose(
              modelChoice({
                title: label,
                models: options,
                value,
                onSelect,
                none,
              }),
            ),
        }
      : {
          kind: "text",
          id,
          label,
          placeholder: `${label} (provider/model-id)`,
          value,
          onChange: onSelect,
        };

  const small = form
    ? resolveCatalogModel(catalog, form.small_model)
    : undefined;
  const providers: NativeFormSection = {
    id: "providers",
    title: "Providers",
    footer: isAdmin
      ? "Manage personal and organization-shared accounts. Provider secrets are encrypted at rest."
      : "Connect personal accounts or use organization-shared ones. Provider secrets are encrypted at rest.",
    rows: providerRows,
  };
  const sections: NativeFormSection[] = form
    ? [
        providers,
        {
          id: "small-model",
          title: "Small model",
          footer:
            "Background tasks like session titles and compaction. Primary and subagent models are configured under Agents.",
          rows: [
            ...(connected
              ? []
              : [
                  {
                    kind: "note" as const,
                    id: "no-provider",
                    text: "No provider connected yet. Connect an account above to browse available models.",
                    tone: "warning" as const,
                  },
                ]),
            modelRow("small-model", "Model", form.small_model, "None", (id) => {
              // An uncatalogued id keeps the effort; no model has none.
              const model = resolveCatalogModel(catalog, id);
              edit({
                small_model: id,
                small_effort: !id
                  ? ""
                  : model
                    ? snapEffort(model, form.small_effort)
                    : form.small_effort,
              });
            }),
            ...(small?.reasoning_efforts.length
              ? [
                  {
                    kind: "picker" as const,
                    id: "small-effort",
                    label: "Effort",
                    value: form.small_effort,
                    groups: effortGroups(small),
                    onChange: (effort: string) =>
                      edit({ small_effort: effort }),
                  },
                ]
              : []),
          ],
        },
        {
          id: "voice",
          title: "Voice input",
          footer: voice.error
            ? "Speech models could not be loaded. You can enter a model ID manually."
            : "Models used for dictation. Audio is sent to your selected speech provider.",
          footerTone: voice.error ? "warning" : "muted",
          rows: [
            modelRow(
              "speech-model",
              "Speech recognition",
              form.speech_model,
              "Automatic",
              (model) => edit({ speech_model: model }),
              (voice.data?.models ?? []).map((model) => ({
                ...model,
                provider: model.id.split("/")[0],
                reasoning_efforts: [],
                default_effort: "",
                supports_tools: false,
                context_length: 0,
                max_output_tokens: 0,
              })),
            ),
            modelRow(
              "cleanup-model",
              "Text cleanup",
              form.voice_cleanup_model,
              "Small model / Build default",
              (model) => edit({ voice_cleanup_model: model }),
            ),
          ],
        },
        autosaveSection(autosave),
      ]
    : [];
  const ready = isAdmin ? Boolean(form) : Boolean(accounts.data);
  const visible = isAdmin ? sections : ready ? [providers] : [];

  const error = accounts.error ?? (isAdmin ? config.error : null);
  return (
    <SettingsPage
      title="Provider & Models"
      sections={
        error && !ready
          ? loadErrorSections(error, () => {
              void accounts.reload();
              if (isAdmin) void config.reload();
            })
          : visible
      }
      loading={!ready && !error}
      onRefresh={async () => {
        await Promise.allSettled([
          accounts.reload(),
          native.reload(),
          engines.reload(),
        ]);
      }}
      testID="settings-providers"
    />
  );
}
