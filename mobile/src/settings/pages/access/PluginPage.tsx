import React, { useState } from "react";
import { Alert } from "react-native";
import { canTogglePlugin } from "@opencuria/chat-core/plugins";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import {
  mcpSubtitle,
  pluginOwner,
  pluginStatus,
  requirementSubtitle,
} from "./plugins";
import { PLUGINS } from "./resources";
import { openPluginEditor } from "./usePluginDraft";

/**
 * One plugin: its skills, MCP servers and credential services, activation
 * for the organization (admins), and editing of organization plugins.
 */
export function PluginPage({
  pluginId,
}: {
  pluginId: string;
}): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const plugin = plugins.data?.find((entry) => entry.id === pluginId);
  const [busy, setBusy] = useState<string | null>(null);

  if (!plugin)
    return (
      <SettingsPage
        title="Plugin"
        sections={[]}
        loading={!plugins.data && !plugins.error}
        empty={{
          title: "Plugin unavailable",
          description:
            plugins.error ??
            "This plugin is unavailable in the current organization or no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-plugin"
      />
    );

  const editable = isAdmin && !plugin.is_global;
  const setActive = async (active: boolean) => {
    setBusy("active");
    try {
      const saved = await client.plugins.setActive(plugin.id, active);
      plugins.set((current) =>
        (current ?? []).map((entry) => (entry.id === saved.id ? saved : entry)),
      );
      showToast({
        title: active ? "Plugin enabled" : "Plugin disabled",
        message: `"${plugin.name}" is now ${active ? "enabled" : "disabled"} for this organization.`,
        tone: "success",
      });
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
      "Its skills, MCP servers, and requirements will be removed. This cannot be undone.",
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
  const sections: NativeFormSection[] = [
    {
      id: "plugin",
      footer: plugin.description || "No description provided.",
      rows: [
        {
          kind: "value",
          id: "status",
          label: "Status",
          value: `${pluginStatus(plugin)} · ${pluginOwner(plugin)}`,
          tone: plugin.org_enabled ? "success" : "muted",
        },
        ...(isAdmin
          ? [
              {
                kind: "toggle" as const,
                id: "enabled",
                testID: "plugin-enabled",
                label: "Enabled for this organization",
                subtitle:
                  !plugin.enabled || !plugin.published
                    ? "Unpublished plugins cannot be enabled."
                    : undefined,
                value: plugin.org_enabled,
                disabled: busy === "active" || !canTogglePlugin(plugin),
                onChange: (active: boolean) => void setActive(active),
              },
            ]
          : []),
      ],
    },
    {
      id: "skills",
      title: `Skills (${skills.length})`,
      rows: skills.length
        ? skills.map((skill) => ({
            kind: "link" as const,
            id: `skill-${skill.id}`,
            title: skill.name,
            route: route("plugin-skill-view", plugin.id, skill.id),
          }))
        : [{ kind: "note" as const, id: "no-skills", text: "No skills." }],
    },
    {
      id: "servers",
      title: `MCP servers (${servers.length})`,
      rows: servers.length
        ? servers.map((server) => ({
            kind: "item" as const,
            id: `server-${server.id}`,
            title: server.name,
            subtitle: mcpSubtitle(server),
          }))
        : [
            {
              kind: "note" as const,
              id: "no-servers",
              text: "No MCP servers.",
            },
          ],
    },
    {
      id: "services",
      title: `Credential services (${plugin.credential_requirements.length})`,
      footer: plugin.credential_requirements.length
        ? "Tap a service to add a credential for it."
        : undefined,
      rows: plugin.credential_requirements.length
        ? plugin.credential_requirements.map((requirement) => ({
            kind: "item" as const,
            id: `requirement-${requirement.id}`,
            title: requirement.service_name,
            subtitle: requirementSubtitle(requirement),
            onPress: () =>
              navigation.push(route("credential-new", requirement.service_id)),
          }))
        : [
            {
              kind: "note" as const,
              id: "no-services",
              text: "No credential services required.",
            },
          ],
    },
  ];

  return (
    <SettingsPage
      title={plugin.name}
      sections={sections}
      actions={
        editable
          ? [
              { id: "edit", title: "Edit", symbol: "pencil" },
              {
                id: "delete",
                title: "Delete",
                symbol: "trash",
                destructive: true,
                disabled: Boolean(busy),
              },
            ]
          : undefined
      }
      onAction={(id) => {
        if (id === "edit") openPluginEditor(resources, navigation, plugin);
        else if (id === "delete") remove();
      }}
      testID="settings-plugin"
    />
  );
}
