import React, { useState } from "react";
import { Alert } from "react-native";
import {
  canTogglePlugin,
  pluginAvailability,
} from "@opencuria/chat-core/plugins";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { loadFailure } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { mcpServerSubtitle, requirementSubtitle } from "./plugins";
import { PLUGINS } from "./resources";
import { openPluginEditor } from "./usePluginDraft";

/**
 * One plugin: its skills, MCP servers and credentials, its availability in
 * the organization (admins, shared plugins) and editing where allowed.
 */
export function PluginPage({
  pluginId,
}: {
  pluginId: string;
}): React.JSX.Element {
  const { client, resources, isAdmin, refreshWorkspaces } = useSettings();
  const navigation = useSettingsNavigation();
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const plugin = plugins.data?.find((entry) => entry.id === pluginId);
  const [busy, setBusy] = useState<"available" | "delete" | null>(null);

  if (!plugin)
    return (
      <SettingsPage
        title="Plugin"
        sections={[]}
        loading={!plugins.data && !plugins.error}
        empty={
          loadFailure("plugin", plugins) ?? {
            title: "Plugin unavailable",
            description:
              "This plugin no longer exists or isn’t shared with you.",
            symbol: "exclamationmark.triangle",
          }
        }
        testID="settings-plugin"
      />
    );

  const setAvailable = async (available: boolean) => {
    setBusy("available");
    try {
      const saved = await client.plugins.setActive(plugin.id, available);
      plugins.set((current) =>
        (current ?? []).map((entry) => (entry.id === saved.id ? saved : entry)),
      );
      // The composer offers the skills of available plugins.
      void refreshWorkspaces();
    } catch (cause) {
      showToast({
        title: "Update failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };
  const remove = () =>
    Alert.alert(
      `Delete ${plugin.name}?`,
      "Its skills and MCP servers are removed from every workspace. This cannot be undone.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: async () => {
            setBusy("delete");
            try {
              await client.plugins.delete(plugin.id);
              plugins.set((current) =>
                (current ?? []).filter((entry) => entry.id !== plugin.id),
              );
              void refreshWorkspaces();
              showToast({ title: "Plugin deleted", tone: "success" });
              navigation.pop();
            } catch (cause) {
              showToast({
                title: "Deletion failed",
                message: errorMessage(cause, "Please try again."),
                tone: "error",
              });
            } finally {
              setBusy(null);
            }
          },
        },
      ],
    );

  const skills = [...(plugin.skills ?? [])].sort(
    (a, b) => a.position - b.position,
  );
  const servers = plugin.mcp_servers ?? [];
  const requirements = plugin.credential_requirements ?? [];
  // Releasing shared plugins for the organization is an admin setting.
  const releasable = isAdmin && plugin.scope !== "personal";
  const sections: NativeFormSection[] = [
    ...(releasable || plugin.description
      ? [
          {
            id: "plugin",
            footer: plugin.description || undefined,
            rows: releasable
              ? [
                  {
                    kind: "toggle" as const,
                    id: "available",
                    testID: "plugin-available",
                    label: "Available in this organization",
                    subtitle:
                      pluginAvailability(plugin) === "Unpublished"
                        ? "Unpublished"
                        : undefined,
                    value: plugin.org_enabled,
                    disabled: busy === "available" || !canTogglePlugin(plugin),
                    onChange: (value: boolean) => void setAvailable(value),
                  },
                ]
              : [],
          },
        ]
      : []),
    ...(skills.length
      ? [
          {
            id: "skills",
            title: "Skills",
            rows: skills.map((skill) => ({
              kind: "link" as const,
              id: `skill-${skill.id}`,
              title: skill.name,
              subtitle: skill.description || undefined,
              route: route("plugin-skill-view", plugin.id, skill.id),
            })),
          },
        ]
      : []),
    ...(servers.length
      ? [
          {
            id: "servers",
            title: "MCP servers",
            rows: servers.map((server) => ({
              kind: "item" as const,
              id: `server-${server.id}`,
              title: server.name,
              subtitle: mcpServerSubtitle(server),
            })),
          },
        ]
      : []),
    ...(!skills.length && !servers.length
      ? [
          {
            id: "contents",
            rows: [
              {
                kind: "note" as const,
                id: "empty",
                text: "No skills or MCP servers yet.",
              },
            ],
          },
        ]
      : []),
    ...(requirements.length
      ? [
          {
            id: "credentials",
            title: "Credentials",
            footer: requirements.some(
              (requirement) => !requirement.credential_id,
            )
              ? "Tap a service to add a credential for it."
              : undefined,
            rows: requirements.map((requirement) => ({
              kind: "item" as const,
              id: `requirement-${requirement.id}`,
              title: requirement.service_name,
              subtitle: requirementSubtitle(requirement),
              onPress: requirement.credential_id
                ? undefined
                : () =>
                    navigation.push(
                      route("credential-new", requirement.service_id),
                    ),
            })),
          },
        ]
      : []),
    ...(plugin.editable
      ? [
          {
            id: "danger",
            rows: [
              {
                kind: "button" as const,
                id: "delete",
                title: "Delete plugin",
                symbol: "trash" as const,
                destructive: true,
                busy: busy === "delete",
                disabled: Boolean(busy),
                onPress: remove,
              },
            ],
          },
        ]
      : []),
  ];

  return (
    <SettingsPage
      title={plugin.name}
      sections={sections}
      primaryAction={
        plugin.editable
          ? {
              title: "Edit",
              onPress: () => openPluginEditor(resources, navigation, plugin),
              disabled: Boolean(busy),
              testID: "edit-plugin",
            }
          : undefined
      }
      testID="settings-plugin"
    />
  );
}
