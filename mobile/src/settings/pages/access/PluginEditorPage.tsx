import React, { useState } from "react";
import {
  emptyMcpForm,
  emptyRequirementForm,
  emptySkillForm,
  entryErrors,
  formToCreateIn,
  formToUpdateIn,
  generalErrors,
  mcpLabel,
  requirementLabel,
  skillLabel,
  validatePluginForm,
} from "@opencuria/chat-core/pluginForms";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { transportLabel } from "./plugins";
import { ACTIVE_SERVICES, PLUGINS, loadServices } from "./resources";
import { usePluginDraft } from "./usePluginDraft";

/**
 * Creates or edits an organization plugin: its name and availability, then
 * one page per skill, MCP server and credential requirement. The plugin is
 * validated as in the webapp before it can be saved.
 */
export function PluginEditorPage({
  draftKey,
}: {
  draftKey: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const { draft, update } = usePluginDraft(draftKey);
  const services = useResource(resources, ACTIVE_SERVICES, () =>
    loadServices(client),
  );
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const [busy, setBusy] = useState(false);

  if (!draft)
    return (
      <SettingsPage
        title="Plugin"
        sections={[]}
        empty={{
          title: "Draft unavailable",
          description: "Open the plugin again to edit it.",
          symbol: "exclamationmark.triangle.fill",
        }}
      />
    );

  const { form, pluginId } = draft;
  const errors = validatePluginForm({
    ...form,
    availableServices: services.data ?? [],
  });
  const general = generalErrors(errors);
  const entryRoute = (kind: string, uid: string) => route(kind, draftKey, uid);

  const save = async () => {
    setBusy(true);
    try {
      if (pluginId) {
        const saved = await client.plugins.update(
          pluginId,
          formToUpdateIn(form),
        );
        plugins.set((current) =>
          (current ?? []).map((plugin) =>
            plugin.id === saved.id ? saved : plugin,
          ),
        );
        showToast({
          title: "Plugin updated",
          message: `"${saved.name}" was saved.`,
          tone: "success",
        });
        navigation.pop();
      } else {
        const created = await client.plugins.create(formToCreateIn(form));
        plugins.set((current) => [...(current ?? []), created]);
        showToast({
          title: "Plugin created",
          message: `"${created.name}" is ready.`,
          tone: "success",
        });
        navigation.replace(route("plugin", created.id));
      }
    } catch (cause) {
      showToast({
        title: pluginId ? "Update failed" : "Creation failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const sections: NativeFormSection[] = [
    {
      id: "plugin",
      footer: general[0],
      footerTone: general.length ? "danger" : "muted",
      rows: [
        {
          kind: "text",
          id: "name",
          label: "Name",
          placeholder: "Playwright Helper",
          value: form.name,
          onChange: (name) => update((current) => ({ ...current, name })),
        },
        {
          kind: "text",
          id: "description",
          label: "Description",
          placeholder: "What this plugin provides…",
          value: form.description,
          multiline: true,
          prose: true,
          lines: [2, 5],
          onChange: (description) =>
            update((current) => ({ ...current, description })),
        },
        {
          kind: "toggle",
          id: "published",
          label: "Published",
          value: form.published,
          onChange: (published) =>
            update((current) => ({ ...current, published })),
        },
        {
          kind: "toggle",
          id: "enabled",
          label: "Enabled",
          value: form.enabled,
          onChange: (enabled) => update((current) => ({ ...current, enabled })),
        },
      ],
    },
    {
      id: "skills",
      title: "Skills",
      footer:
        "Markdown fragments injected into harness prompts, in list order.",
      rows: [
        ...form.skills.map((skill, index) => {
          const problems = entryErrors(errors, skillLabel(index));
          return {
            kind: "link" as const,
            id: `skill-${skill.uid}`,
            title: skill.name.trim() || skillLabel(index),
            subtitle: problems[0],
            subtitleTone: "danger" as const,
            route: entryRoute("plugin-skill", skill.uid),
          };
        }),
        {
          kind: "button",
          id: "add-skill",
          title: "Add skill",
          symbol: "plus",
          onPress: () => {
            const skill = emptySkillForm();
            update((current) => ({
              ...current,
              skills: [...current.skills, skill],
            }));
            navigation.push(entryRoute("plugin-skill", skill.uid));
          },
        },
      ],
    },
    {
      id: "servers",
      title: "MCP servers",
      footer: "Run inside the workspace; localhost means the workspace.",
      rows: [
        ...form.mcps.map((mcp, index) => {
          const problems = entryErrors(errors, mcpLabel(mcp, index));
          return {
            kind: "link" as const,
            id: `server-${mcp.uid}`,
            title: mcp.name.trim() || `MCP server #${index + 1}`,
            subtitle: problems[0] ?? transportLabel(mcp.transport),
            subtitleTone: problems.length
              ? ("danger" as const)
              : ("muted" as const),
            route: entryRoute("plugin-mcp", mcp.uid),
          };
        }),
        {
          kind: "button",
          id: "add-server",
          title: "Add MCP server",
          symbol: "plus",
          onPress: () => {
            const mcp = emptyMcpForm();
            update((current) => ({ ...current, mcps: [...current.mcps, mcp] }));
            navigation.push(entryRoute("plugin-mcp", mcp.uid));
          },
        },
      ],
    },
    {
      id: "requirements",
      title: "Credential requirements",
      footer: services.error
        ? `Credential services could not be loaded: ${services.error}`
        : "Reference a requirement in environment variables and headers as {{credential.KEY}}.",
      footerTone: services.error ? "danger" : "muted",
      rows: [
        ...form.requirements.map((requirement, index) => {
          const problems = entryErrors(
            errors,
            requirementLabel(requirement, index),
          );
          const service = services.data?.find(
            (entry) => entry.id === requirement.serviceId,
          );
          return {
            kind: "link" as const,
            id: `requirement-${requirement.uid}`,
            title: requirement.reqKey.trim() || `Requirement #${index + 1}`,
            subtitle:
              problems[0] ??
              [service?.name, requirement.required ? "Required" : "Optional"]
                .filter(Boolean)
                .join(" · "),
            subtitleTone: problems.length
              ? ("danger" as const)
              : ("muted" as const),
            route: entryRoute("plugin-requirement", requirement.uid),
          };
        }),
        {
          kind: "button",
          id: "add-requirement",
          title: "Add requirement",
          symbol: "plus",
          onPress: () => {
            const requirement = emptyRequirementForm();
            update((current) => ({
              ...current,
              requirements: [...current.requirements, requirement],
            }));
            navigation.push(entryRoute("plugin-requirement", requirement.uid));
          },
        },
      ],
    },
  ];

  return (
    <SettingsPage
      title={pluginId ? "Edit plugin" : "New plugin"}
      sections={sections}
      primaryAction={{
        title: pluginId ? "Save" : "Create",
        onPress: () => void save(),
        busy,
        disabled: busy || errors.length > 0 || !services.data,
        testID: "save-plugin",
      }}
      testID="settings-plugin-editor"
    />
  );
}
