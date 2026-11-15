import { useCallback } from "react";
import {
  emptyPluginForm,
  pluginToForm,
  type PluginFormModel,
} from "@opencuria/chat-core/pluginForms";
import type { Plugin } from "@opencuria/client";
import type { ResourceStore } from "../../resources";
import { useResource } from "../../resources";
import { route } from "../../routes";
import type { SettingsNavigation } from "../../SettingsContext";
import { useSettings } from "../../SettingsContext";
import { newDraftKey, type PluginDraft } from "./pluginDraft";

const draftResource = (key: string) => `plugin-draft:${key}`;

/** Starts editing a plugin (or a new one) and opens its editor. */
export function openPluginEditor(
  resources: ResourceStore,
  navigation: SettingsNavigation,
  plugin: Plugin | null,
): void {
  const key = newDraftKey();
  resources.set<PluginDraft>(draftResource(key), {
    pluginId: plugin?.id ?? null,
    form: plugin ? pluginToForm(plugin) : emptyPluginForm(),
  });
  navigation.push(route("plugin-editor", key));
}

/** The draft shared by the plugin editor and its entry pages. */
export function usePluginDraft(key: string): {
  draft: PluginDraft | undefined;
  update: (change: (form: PluginFormModel) => PluginFormModel) => void;
} {
  const { resources } = useSettings();
  const draft = useResource<PluginDraft>(
    resources,
    draftResource(key),
    async () => ({ pluginId: null, form: emptyPluginForm() }),
    // Drafts are created before their editor opens; never load one.
    { enabled: false },
  );
  const set = draft.set;
  const update = useCallback(
    (change: (form: PluginFormModel) => PluginFormModel) =>
      set((current) =>
        current ? { ...current, form: change(current.form) } : current!,
      ),
    [set],
  );
  return { draft: draft.data, update };
}
