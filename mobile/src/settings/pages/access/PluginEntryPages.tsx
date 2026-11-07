import React from "react";
import {
  PLACEHOLDER_EXAMPLE,
  emptyKeyValueRow,
  entryErrors,
  mcpLabel,
  removeEntry,
  requirementLabel,
  setMcpAuthentication,
  setMcpOAuthRequirement,
  setMcpTransport,
  setRequirementService,
  skillLabel,
  updateEntry,
  validatePluginForm,
  type KeyValueRow,
  type PluginFormModel,
  type PluginMcpForm,
  type PluginRequirementForm,
  type PluginSkillForm,
} from "@opencuria/chat-core/pluginForms";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { serviceTypeLabel } from "./credentialServices";
import { ACTIVE_SERVICES, loadServices } from "./resources";
import { usePluginDraft } from "./usePluginDraft";

const REMOVED = {
  title: "Removed",
  description: "This entry is no longer part of the plugin.",
  symbol: "trash" as const,
};

/** Validation of the whole draft, for the errors of one entry. */
function useDraftErrors(form: PluginFormModel | undefined): string[] {
  const { client, resources } = useSettings();
  const services = useResource(resources, ACTIVE_SERVICES, () =>
    loadServices(client),
  );
  return form
    ? validatePluginForm({ ...form, availableServices: services.data ?? [] })
    : [];
}

/** A "Remove …" button that drops the entry and goes back. */
function removeSection(title: string, onPress: () => void): NativeFormSection {
  return {
    id: "remove",
    rows: [
      {
        kind: "button",
        id: "remove",
        title,
        symbol: "trash",
        destructive: true,
        onPress,
      },
    ],
  };
}

/** One skill of a plugin draft: name and Markdown body. */
export function PluginSkillPage({
  draftKey,
  uid,
}: {
  draftKey: string;
  uid: string;
}): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const { draft, update } = usePluginDraft(draftKey);
  const errors = useDraftErrors(draft?.form);
  const index =
    draft?.form.skills.findIndex((entry) => entry.uid === uid) ?? -1;
  const skill = index >= 0 ? draft!.form.skills[index] : undefined;
  const edit = (next: Partial<PluginSkillForm>) =>
    update((form) =>
      updateEntry(form, "skills", uid, (entry) => ({ ...entry, ...next })),
    );
  const problems = skill ? entryErrors(errors, skillLabel(index)) : [];
  return (
    <SettingsPage
      title={skill?.name.trim() || "Skill"}
      sections={
        skill
          ? [
              {
                id: "skill",
                footer:
                  problems.join(" ") || "Markdown appended to harness prompts.",
                footerTone: problems.length ? "danger" : "muted",
                rows: [
                  {
                    kind: "text",
                    id: "skill-name",
                    label: "Name",
                    placeholder: "Playwright basics",
                    value: skill.name,
                    onChange: (name) => edit({ name }),
                  },
                  {
                    kind: "text",
                    id: "skill-body",
                    label: "Body",
                    placeholder: "Use the browser tool to…",
                    value: skill.body,
                    multiline: true,
                    prose: true,
                    lines: [6, 16],
                    onChange: (body) => edit({ body }),
                  },
                ],
              },
              removeSection("Remove skill", () => {
                navigation.pop();
                update((form) => removeEntry(form, "skills", uid));
              }),
            ]
          : []
      }
      empty={REMOVED}
      testID="settings-plugin-skill"
    />
  );
}

/** Key/value rows (environment variables or headers) with add and remove. */
function keyValueRows(
  id: string,
  rows: KeyValueRow[],
  labels: {
    key: string;
    value: string;
    add: string;
    keyPlaceholder: string;
    valuePlaceholder: string;
  },
  onChange: (rows: KeyValueRow[]) => void,
): NativeFormRow[] {
  const set = (uid: string, next: Partial<KeyValueRow>) =>
    onChange(rows.map((row) => (row.uid === uid ? { ...row, ...next } : row)));
  return [
    ...rows.flatMap((row, index): NativeFormRow[] => [
      {
        kind: "text",
        id: `${id}-key-${row.uid}`,
        label: `${labels.key} ${index + 1}`,
        placeholder: labels.keyPlaceholder,
        monospace: true,
        value: row.key,
        onChange: (key) => set(row.uid, { key }),
      },
      {
        kind: "text",
        id: `${id}-value-${row.uid}`,
        label: `${labels.value} ${index + 1}`,
        placeholder: labels.valuePlaceholder,
        monospace: true,
        value: row.value,
        onChange: (value) => set(row.uid, { value }),
      },
      {
        kind: "button",
        id: `${id}-remove-${row.uid}`,
        title: `Remove ${row.key.trim() || `${labels.key.toLowerCase()} ${index + 1}`}`,
        symbol: "minus.circle.fill",
        destructive: true,
        onPress: () => onChange(rows.filter((entry) => entry.uid !== row.uid)),
      },
    ]),
    {
      kind: "button",
      id: `${id}-add`,
      title: labels.add,
      symbol: "plus",
      onPress: () => onChange([...rows, emptyKeyValueRow()]),
    },
  ];
}

/** Timeout in seconds as a number field; empty input is invalid (0). */
function timeoutRow(
  id: string,
  label: string,
  value: number,
  onChange: (value: number) => void,
): NativeFormRow {
  return {
    kind: "text",
    id,
    label,
    placeholder: `${label} (1–600)`,
    keyboard: "number",
    value: Number.isFinite(value) && value > 0 ? String(value) : "",
    onChange: (text) =>
      onChange(/^\d+$/.test(text.trim()) ? Number(text.trim()) : 0),
  };
}

/**
 * One MCP server of a plugin draft. stdio runs a single executable in the
 * workspace; HTTP and SSE connect to a URL, optionally with MCP OAuth.
 */
export function PluginMcpPage({
  draftKey,
  uid,
}: {
  draftKey: string;
  uid: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useResource(resources, ACTIVE_SERVICES, () =>
    loadServices(client),
  );
  const { draft, update } = usePluginDraft(draftKey);
  const errors = useDraftErrors(draft?.form);
  const index = draft?.form.mcps.findIndex((entry) => entry.uid === uid) ?? -1;
  const mcp = index >= 0 ? draft!.form.mcps[index] : undefined;
  if (!mcp || !draft)
    return <SettingsPage title="MCP server" sections={[]} empty={REMOVED} />;

  const edit = (next: Partial<PluginMcpForm>) =>
    update((form) =>
      updateEntry(form, "mcps", uid, (entry) => ({ ...entry, ...next })),
    );
  const problems = entryErrors(errors, mcpLabel(mcp, index));
  const stdio = mcp.transport === "stdio";
  const oauthRequirements = draft.form.requirements.filter(
    (requirement) =>
      requirement.required &&
      requirement.credentialType === "mcp_oauth" &&
      requirement.reqKey.trim(),
  );

  const sections: NativeFormSection[] = [
    {
      id: "server",
      footer: problems.join(" ") || undefined,
      footerTone: "danger",
      rows: [
        {
          kind: "text",
          id: "mcp-name",
          label: "Name",
          placeholder: "Playwright",
          value: mcp.name,
          onChange: (name) => edit({ name }),
        },
        {
          kind: "picker",
          id: "mcp-transport",
          label: "Transport",
          value: mcp.transport,
          groups: [
            {
              options: [
                { value: "stdio", label: "stdio (command)" },
                { value: "streamable_http", label: "Streamable HTTP (URL)" },
                { value: "sse", label: "SSE (URL)" },
              ],
            },
          ],
          onChange: (transport) =>
            update((form) =>
              setMcpTransport(
                form,
                uid,
                transport as PluginMcpForm["transport"],
              ),
            ),
        },
      ],
    },
    ...(stdio
      ? [
          {
            id: "command",
            title: "Command",
            footer:
              "A single executable without shell metacharacters; one argument per line. localhost means the workspace.",
            rows: [
              {
                kind: "text" as const,
                id: "mcp-command",
                label: "Command",
                placeholder: "npx",
                monospace: true,
                value: mcp.command,
                onChange: (command: string) => edit({ command }),
              },
              {
                kind: "text" as const,
                id: "mcp-args",
                label: "Arguments",
                placeholder: "-y\n@playwright/mcp@latest",
                monospace: true,
                multiline: true,
                lines: [2, 8] as [number, number],
                value: mcp.argsText,
                onChange: (argsText: string) => edit({ argsText }),
              },
              {
                kind: "text" as const,
                id: "mcp-cwd",
                label: "Working directory",
                placeholder: "/workspace",
                monospace: true,
                value: mcp.cwd,
                onChange: (cwd: string) => edit({ cwd }),
              },
              {
                kind: "picker" as const,
                id: "mcp-desktop",
                label: "Managed desktop",
                value: mcp.desktop,
                groups: [
                  {
                    options: [
                      { value: "none", label: "None" },
                      { value: "server_start", label: "Start with server" },
                      { value: "first_tool", label: "Start on first tool" },
                    ],
                  },
                ],
                onChange: (desktop: string) =>
                  edit({ desktop: desktop as PluginMcpForm["desktop"] }),
              },
            ],
          },
          {
            id: "env",
            title: "Environment",
            footer: `No secret values here — reference requirement keys as ${PLACEHOLDER_EXAMPLE}.`,
            rows: keyValueRows(
              "env",
              mcp.env,
              {
                key: "Variable",
                value: "Value",
                add: "Add variable",
                keyPlaceholder: "KEY",
                valuePlaceholder: `value or ${PLACEHOLDER_EXAMPLE}`,
              },
              (env) => edit({ env }),
            ),
          },
        ]
      : [
          {
            id: "endpoint",
            title: "Endpoint",
            footer:
              mcp.authType === "oauth"
                ? "The URL must match the endpoint of the OAuth credential service exactly."
                : undefined,
            rows: [
              {
                kind: "text" as const,
                id: "mcp-url",
                label: "URL",
                placeholder: "https://mcp.example.com/mcp",
                keyboard: "url" as const,
                value: mcp.url,
                onChange: (url: string) => edit({ url }),
              },
              {
                kind: "picker" as const,
                id: "mcp-auth",
                label: "Authentication",
                value: mcp.authType,
                groups: [
                  {
                    options: [
                      { value: "none", label: "None" },
                      { value: "oauth", label: "OAuth 2.0 (MCP)" },
                    ],
                  },
                ],
                onChange: (authType: string) =>
                  update((form) =>
                    setMcpAuthentication(
                      form,
                      uid,
                      authType as "none" | "oauth",
                    ),
                  ),
              },
              ...(mcp.authType === "oauth"
                ? [
                    {
                      kind: "picker" as const,
                      id: "mcp-oauth-requirement",
                      label: "OAuth credential",
                      value: mcp.oauthRequirementKey,
                      disabled: !oauthRequirements.length,
                      groups: [
                        {
                          options: [
                            { value: "", label: "Choose" },
                            ...oauthRequirements.map((requirement) => ({
                              value: requirement.reqKey,
                              label: requirement.reqKey,
                            })),
                          ],
                        },
                      ],
                      onChange: (key: string) =>
                        update((form) =>
                          setMcpOAuthRequirement(
                            form,
                            uid,
                            key,
                            services.data ?? [],
                          ),
                        ),
                    },
                  ]
                : []),
            ],
          },
          {
            id: "headers",
            title: "Headers",
            footer: `Reference requirement keys as ${PLACEHOLDER_EXAMPLE}.`,
            rows: keyValueRows(
              "header",
              mcp.headers,
              {
                key: "Header",
                value: "Value",
                add: "Add header",
                keyPlaceholder: "Authorization",
                valuePlaceholder: `Bearer token or ${PLACEHOLDER_EXAMPLE}`,
              },
              (headers) => edit({ headers }),
            ),
          },
        ]),
    {
      id: "timeouts",
      title: "Timeouts",
      footer: "Seconds, from 1 to 600.",
      rows: [
        timeoutRow(
          "mcp-startup",
          "Startup timeout",
          mcp.startupTimeout,
          (startupTimeout) => edit({ startupTimeout }),
        ),
        timeoutRow(
          "mcp-request",
          "Request timeout",
          mcp.requestTimeout,
          (requestTimeout) => edit({ requestTimeout }),
        ),
      ],
    },
    removeSection("Remove MCP server", () => {
      navigation.pop();
      update((form) => removeEntry(form, "mcps", uid));
    }),
  ];

  return (
    <SettingsPage
      title={mcp.name.trim() || "MCP server"}
      sections={sections}
      testID="settings-plugin-mcp"
    />
  );
}

/** One credential requirement of a plugin draft and its service. */
export function PluginRequirementPage({
  draftKey,
  uid,
}: {
  draftKey: string;
  uid: string;
}): React.JSX.Element {
  const { client, resources, organizationId } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useResource(resources, ACTIVE_SERVICES, () =>
    loadServices(client),
  );
  const { draft, update } = usePluginDraft(draftKey);
  const errors = useDraftErrors(draft?.form);
  const index =
    draft?.form.requirements.findIndex((entry) => entry.uid === uid) ?? -1;
  const requirement = index >= 0 ? draft!.form.requirements[index] : undefined;
  if (!requirement)
    return <SettingsPage title="Requirement" sections={[]} empty={REMOVED} />;

  const available = (services.data ?? []).filter(
    (service) =>
      !service.organization_id || service.organization_id === organizationId,
  );
  const service = available.find((entry) => entry.id === requirement.serviceId);
  const problems = entryErrors(errors, requirementLabel(requirement, index));
  const edit = (next: Partial<PluginRequirementForm>) =>
    update((form) =>
      updateEntry(form, "requirements", uid, (entry) => ({
        ...entry,
        ...next,
      })),
    );

  return (
    <SettingsPage
      title={requirement.reqKey.trim() || "Requirement"}
      sections={[
        {
          id: "service",
          footer:
            problems.join(" ") ||
            "Uses an existing credential service. Create organization services under Credential services.",
          footerTone: problems.length ? "danger" : "muted",
          rows: [
            {
              kind: "value",
              id: "requirement-service",
              label: "Credential service",
              value: service?.name ?? "Choose",
              onPress: () =>
                navigation.choose({
                  title: "Credential service",
                  value: requirement.serviceId,
                  emptyText:
                    services.error ?? "No credential services are available.",
                  groups: [
                    {
                      options: available.map((entry) => ({
                        value: entry.id,
                        label: entry.name,
                        subtitle: `${serviceTypeLabel(entry.credential_type)}${entry.is_active ? "" : " (inactive)"}`,
                      })),
                    },
                  ],
                  onSelect: (id) => {
                    const chosen = available.find((entry) => entry.id === id);
                    if (chosen)
                      update((form) =>
                        setRequirementService(form, uid, chosen),
                      );
                  },
                }),
            },
            {
              kind: "text",
              id: "requirement-key",
              label: "Key",
              placeholder: "api_key",
              monospace: true,
              value: requirement.reqKey,
              onChange: (reqKey) => edit({ reqKey }),
            },
            {
              kind: "text",
              id: "requirement-description",
              label: "Description",
              placeholder: "Used for API access",
              value: requirement.description,
              onChange: (description) => edit({ description }),
            },
            {
              kind: "toggle",
              id: "requirement-required",
              label: "Required",
              value: requirement.required,
              onChange: (required) => edit({ required }),
            },
          ],
        },
        removeSection("Remove requirement", () => {
          navigation.pop();
          update((form) => removeEntry(form, "requirements", uid));
        }),
      ]}
      testID="settings-plugin-requirement"
    />
  );
}
