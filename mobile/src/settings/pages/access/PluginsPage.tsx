import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { pluginStatus, pluginSubtitle } from "./plugins";
import { PLUGINS } from "./resources";
import { openPluginEditor } from "./usePluginDraft";

/** Skills and MCP server bundles available to this organization. */
export function PluginsPage(): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const sections: NativeFormSection[] = plugins.data?.length
    ? [
        {
          id: "plugins",
          footer:
            "Plugins bundle skills, MCP servers and the credentials they need. Activate a plugin for the organization, then enable it per workspace.",
          rows: plugins.data.map((plugin) => {
            const status = pluginStatus(plugin);
            return {
              kind: "item" as const,
              id: `plugin-${plugin.id}`,
              testID: `plugin-${plugin.id}`,
              title: plugin.name,
              subtitle: `${status} · ${pluginSubtitle(plugin)}`,
              subtitleTone: status === "Active" ? "success" : "muted",
              onPress: () => navigation.push(route("plugin", plugin.id)),
            };
          }),
        },
      ]
    : [];
  return (
    <SettingsPage
      title="Plugins"
      sections={
        plugins.error && !plugins.data
          ? loadErrorSections(plugins.error, () => void plugins.reload())
          : sections
      }
      loading={!plugins.data && !plugins.error}
      onRefresh={plugins.reload}
      empty={{
        title: "No plugins yet",
        description: "Plugins available to this organization appear here.",
        symbol: "puzzlepiece.extension",
      }}
      primaryAction={
        isAdmin
          ? {
              title: "New plugin",
              symbol: "plus",
              onPress: () => openPluginEditor(resources, navigation, null),
              testID: "new-plugin",
            }
          : undefined
      }
      testID="settings-plugins"
    />
  );
}
