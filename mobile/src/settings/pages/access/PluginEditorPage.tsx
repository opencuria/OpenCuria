import React, { useState } from "react";
import {
  emptyMcpForm,
  emptySkillForm,
  entryErrors,
  formToCreateIn,
  formToUpdateIn,
  generalErrors,
  mcpLabel,
  requirementLabel,
  skillLabel,
  validatePluginForm,
  type PluginFormModel,
  keepUsableCredentials,
} from "@opencuria/chat-core/pluginForms";
import type { Credential } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { errorRows } from "../../../ui/formStates";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { mcpFormSubtitle } from "./plugins";
import {
  ACTIVE_SERVICES,
  CREDENTIALS,
  PLUGINS,
  loadServices,
} from "./resources";
import { usePluginDraft } from "./usePluginDraft";

/**
 * Creates or edits a plugin: name, description and who can use it, then one
 * page per skill and MCP server. Credential requirements sit under
 * Advanced. The draft is validated as in the webapp before it can be saved.
 */
export function PluginEditorPage({
  draftKey,
}: {
  draftKey: string;
}): React.JSX.Element {
  const { client, resources, isAdmin, refreshWorkspaces } = useSettings();
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
          symbol: "exclamationmark.triangle",
        }}
      />
    );

  const { form, pluginId } = draft;
  const errors = validatePluginForm({
    ...form,
    availableServices: services.data ?? [],
  });
  const general = generalErrors(errors);
  const entryRoute = (page: string, uid: string) => route(page, draftKey, uid);
  const requirementProblems = form.requirements.flatMap((requirement, index) =>
    entryErrors(errors, requirementLabel(requirement, index)),
  );

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
        showToast({ title: "Plugin saved", tone: "success" });
        navigation.pop();
      } else {
        const created = await client.plugins.create(formToCreateIn(form));
        plugins.set((current) => [...(current ?? []), created]);
        showToast({ title: "Plugin created", tone: "success" });
        navigation.replace(route("plugin", created.id));
      }
      // The composer offers the skills of usable plugins.
      void refreshWorkspaces();
    } catch (cause) {
      showToast({
        title: pluginId ? "Saving failed" : "Creation failed",
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
      footerTone: "danger",
      rows: [
        {
          kind: "text",
          id: "name",
          label: "Name",
          value: form.name,
          autoFocus: !pluginId,
          onChange: (name) => update((current) => ({ ...current, name })),
        },
        {
          kind: "text",
          id: "description",
          label: "Description",
          value: form.description,
          multiline: true,
          prose: true,
          lines: [2, 5],
          onChange: (description) =>
            update((current) => ({ ...current, description })),
        },
      ],
    },
    // Organization plugins need an admin; the scope is chosen once.
    ...(!pluginId && isAdmin
      ? [
          {
            id: "scope",
            footer:
              form.scope === "organization"
                ? "Members can use it once you make it available."
                : undefined,
            rows: [
              {
                kind: "picker" as const,
                id: "scope",
                label: "Available to",
                value: form.scope,
                groups: [
                  {
                    options: [
                      { value: "personal", label: "Only me" },
                      {
                        value: "organization",
                        label: "Everyone in the organization",
                      },
                    ],
                  },
                ],
                onChange: (scope: string) =>
                  update((current) =>
                    // Organization plugins share organization credentials only.
                    keepUsableCredentials(
                      { ...current, scope: scope as PluginFormModel["scope"] },
                      (id) =>
                        scope !== "organization" ||
                        // Choices were made from the credentials loaded by the server pages.
                        (
                          resources.get<Credential[]>(CREDENTIALS).data ?? []
                        ).some(
                          (credential) =>
                            credential.id === id &&
                            credential.scope === "organization",
                        ),
                    ),
                  ),
              },
            ],
          },
        ]
      : []),
    {
      id: "skills",
      title: "Skills",
      rows: [
        ...form.skills.map((skill, index) => {
          const problems = entryErrors(errors, skillLabel(index));
          return {
            kind: "link" as const,
            id: `skill-${skill.uid}`,
            title: skill.name.trim() || skillLabel(index),
            subtitle: problems[0] ?? (skill.description.trim() || undefined),
            subtitleTone: problems.length
              ? ("danger" as const)
              : ("muted" as const),
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
      rows: [
        ...form.mcps.map((mcp, index) => {
          const problems = entryErrors(errors, mcpLabel(mcp, index));
          return {
            kind: "link" as const,
            id: `server-${mcp.uid}`,
            title: mcp.name.trim() || `MCP server #${index + 1}`,
            subtitle: problems[0] ?? (mcpFormSubtitle(mcp) || undefined),
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
        {
          kind: "button",
          id: "paste-servers",
          title: "Paste configuration",
          symbol: "doc.on.clipboard",
          onPress: () => navigation.push(route("plugin-mcp-import", draftKey)),
        },
      ],
    },
    {
      id: "advanced",
      title: "Advanced",
      rows: [
        {
          kind: "link",
          id: "requirements",
          title: "More credentials",
          subtitle: requirementProblems[0],
          subtitleTone: "danger",
          value: form.requirements.length
            ? String(form.requirements.length)
            : undefined,
          route: route("plugin-requirements", draftKey),
        },
        ...(services.error
          ? errorRows(
              "services",
              `Couldn’t load credential services: ${services.error}`,
              () => void services.reload(),
              services.loading,
            )
          : []),
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
