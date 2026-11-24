import React from "react";
import {
  PLUGIN_SCOPES,
  pluginsOfScope,
  type PluginScope,
} from "@opencuria/chat-core/plugins";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadFailure, refreshError } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { pluginSummary } from "./plugins";
import { PLUGINS } from "./resources";
import { openPluginEditor } from "./usePluginDraft";

const SECTION_TITLES: Record<PluginScope, string> = {
  personal: "Your plugins",
  organization: "Organization",
  builtin: "Built-in",
};

/** The user's own, the organization's and the built-in plugins. */
export function PluginsPage(): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const sections: NativeFormSection[] = PLUGIN_SCOPES.map((scope) => ({
    id: scope,
    title: SECTION_TITLES[scope],
    rows: pluginsOfScope(plugins.data ?? [], scope).map((plugin) => {
      const summary = pluginSummary(plugin, isAdmin);
      return {
        kind: "item" as const,
        id: `plugin-${plugin.id}`,
        testID: `plugin-${plugin.id}`,
        title: plugin.name,
        subtitle: summary.text || undefined,
        subtitleTone: summary.tone,
        onPress: () => navigation.push(route("plugin", plugin.id)),
      };
    }),
  })).filter((section) => section.rows.length);
  if (sections.length)
    sections[sections.length - 1]!.footer = "Enable plugins per workspace.";
  const failure = plugins.data ? undefined : loadFailure("plugins", plugins);
  const create = {
    title: "New plugin",
    symbol: "plus" as const,
    onPress: () => openPluginEditor(resources, navigation, null),
  };
  return (
    <SettingsPage
      title="Plugins"
      sections={
        failure ? [] : [...refreshError("plugins", plugins), ...sections]
      }
      loading={!plugins.data && !plugins.error}
      onRefresh={plugins.reload}
      empty={
        failure ?? {
          title: "No plugins yet",
          description:
            "Plugins add skills and tools to your agents. Create one, or ask an agent to build one for you.",
          symbol: "puzzlepiece.extension",
          action: { ...create, testID: "empty-new-plugin" },
        }
      }
      primaryAction={{ ...create, testID: "new-plugin" }}
      testID="settings-plugins"
    />
  );
}
