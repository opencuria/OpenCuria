import React, { useMemo, useState } from "react";
import {
  PLACEHOLDER_EXAMPLE,
  applySkillMarkdown,
  emptyRequirementForm,
  emptyVariableRow,
  entryErrors,
  isSavedSecret,
  looksSecret,
  mcpLabel,
  parseMcpConfig,
  parseSkillMarkdown,
  removeEntry,
  requirementLabel,
  setMcpAuthentication,
  setMcpKind,
  setMcpOAuthRequirement,
  setRequirementService,
  skillLabel,
  updateEntry,
  validatePluginForm,
  type McpKind,
  type McpUrlTransport,
  type PluginFormModel,
  type PluginMcpForm,
  type PluginRequirementForm,
  type PluginSkillForm,
  type SecretSource,
  type VariableRow,
} from "@opencuria/chat-core/pluginForms";
import type { Credential } from "@opencuria/client";
import type {
  NativeFormEmptyState,
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { errorRows } from "../../../ui/formStates";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { serviceTypeLabel } from "./credentialServices";
import { ACTIVE_SERVICES, CREDENTIALS, loadServices } from "./resources";
import { usePluginDraft } from "./usePluginDraft";

const REMOVED: NativeFormEmptyState = {
  title: "Removed",
  description: "This entry is no longer part of the plugin.",
  symbol: "trash",
};

const NO_DRAFT: NativeFormEmptyState = {
  title: "Draft unavailable",
  description: "Open the plugin again to edit it.",
  symbol: "exclamationmark.triangle",
};

/** Credential services, shared with the editor. */
function useServices() {
  const { client, resources } = useSettings();
  return useResource(resources, ACTIVE_SERVICES, () => loadServices(client));
}

/** Credentials a plugin may use: organization plugins share organization ones only. */
function useUsableCredentials(
  scope: PluginFormModel["scope"] | undefined,
): Credential[] {
  const { client, resources } = useSettings();
  const credentials = useResource(resources, CREDENTIALS, () =>
    client.credentials.list(),
  );
  return (credentials.data ?? []).filter(
    (credential) =>
      scope !== "organization" || credential.scope === "organization",
  );
}

const EACH_WORKSPACE = ":each-workspace";
const ENTER_VALUE = ":value";

/** Validation of the whole draft, for the errors of one entry. */
function useDraftErrors(form: PluginFormModel | undefined): string[] {
  const services = useServices();
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

/**
 * One skill of a plugin draft. Pasting a SKILL.md fills in an empty name
 * and description from its frontmatter.
 */
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
  if (!skill)
    return <SettingsPage title="Skill" sections={[]} empty={REMOVED} />;

  const edit = (change: (entry: PluginSkillForm) => PluginSkillForm) =>
    update((form) => updateEntry(form, "skills", uid, change));
  const problems = entryErrors(errors, skillLabel(index));
  return (
    <SettingsPage
      title={skill.name.trim() || "Skill"}
      sections={[
        {
          id: "skill",
          footer:
            "Agents see the description and load the instructions when a task matches it.",
          rows: [
            {
              kind: "text",
              id: "skill-name",
              label: "Name",
              value: skill.name,
              autoFocus: !skill.id && !skill.name,
              onChange: (name) => edit((entry) => ({ ...entry, name })),
            },
            {
              kind: "text",
              id: "skill-description",
              label: "Description",
              value: skill.description,
              multiline: true,
              prose: true,
              lines: [2, 4],
              onChange: (description) =>
                edit((entry) => ({ ...entry, description })),
            },
          ],
        },
        {
          id: "instructions",
          footer: problems.join(" ") || undefined,
          footerTone: "danger",
          rows: [
            {
              kind: "text",
              id: "skill-body",
              label: "Instructions",
              value: skill.body,
              multiline: true,
              prose: true,
              lines: [8, 20],
              onChange: (body) =>
                edit((entry) => {
                  // Only a real SKILL.md header; a leading "---" rule stays text.
                  const document = parseSkillMarkdown(body);
                  return document.name || document.description
                    ? applySkillMarkdown(entry, body)
                    : { ...entry, body };
                }),
            },
          ],
        },
        removeSection("Remove skill", () => {
          navigation.pop();
          update((form) => removeEntry(form, "skills", uid));
        }),
      ]}
      testID="settings-plugin-skill"
    />
  );
}

/**
 * Environment variables (command) or headers (URL), one card each: name,
 * value and whether the value is a secret. Saved secrets keep their value
 * while the field stays empty.
 */
function variableSections(
  mcp: PluginMcpForm,
  credentials: Credential[],
  choose: (row: VariableRow, onSelect: (value: string) => void) => void,
  onChange: (change: (variables: VariableRow[]) => VariableRow[]) => void,
): NativeFormSection[] {
  const command = mcp.kind === "command";
  const title = command ? "Environment variables" : "Headers";
  const noun = command ? "Variable" : "Header";
  const rows = mcp.variables;
  const set = (uid: string, change: (row: VariableRow) => VariableRow) =>
    onChange((current) =>
      current.map((row) => (row.uid === uid ? change(row) : row)),
    );
  const setSource = (uid: string, value: string) => {
    const credential = credentials.find((entry) => entry.id === value);
    const source: SecretSource = credential
      ? "credential"
      : value === EACH_WORKSPACE
        ? "per_user"
        : "value";
    set(uid, (current) =>
      current.source === source && !credential
        ? current
        : {
            ...current,
            source,
            value: "",
            credentialId: credential?.id ?? "",
            credentialName: credential?.name ?? "",
            serviceId: credential?.service_id ?? current.serviceId,
          },
    );
  };
  const sourceLabel = (row: VariableRow) =>
    row.source === "credential"
      ? row.credentialName || "Choose a credential"
      : row.source === "per_user"
        ? "Each workspace adds its own"
        : "Entered value";
  return [
    ...rows.map(
      (row, index): NativeFormSection => ({
        id: `variable-${row.uid}`,
        title: index === 0 ? title : undefined,
        rows: [
          {
            kind: "text",
            id: `variable-key-${row.uid}`,
            label: `${noun} ${index + 1} name`,
            placeholder: "Name",
            monospace: true,
            value: row.key,
            onChange: (key) =>
              set(row.uid, (current) => ({
                ...current,
                key,
                // A name that starts to look secret turns Secret on once.
                secret: current.secret || (!current.key && looksSecret(key)),
              })),
          },
          {
            kind: "toggle",
            id: `variable-secret-${row.uid}`,
            label: "Secret",
            value: row.secret,
            onChange: (secret) =>
              set(row.uid, (current) => ({
                ...current,
                secret,
                value: "",
                source: "value",
                credentialId: "",
                credentialName: "",
                ...(secret ? {} : { serviceId: "", requirementKey: "" }),
              })),
          },
          ...(row.secret
            ? [
                {
                  kind: "value" as const,
                  id: `variable-source-${row.uid}`,
                  label: "Comes from",
                  value: sourceLabel(row),
                  onPress: () =>
                    choose(row, (value) => setSource(row.uid, value)),
                },
              ]
            : []),
          ...(!row.secret || row.source === "value"
            ? [
                {
                  kind: "text" as const,
                  id: `variable-value-${row.uid}`,
                  label: `${noun} ${index + 1} value`,
                  placeholder: isSavedSecret(row)
                    ? `Saved · ${row.credentialName}`
                    : "Value",
                  secure: row.secret,
                  monospace: !row.secret,
                  value: row.value,
                  onChange: (value: string) =>
                    set(row.uid, (current) => ({ ...current, value })),
                },
              ]
            : []),
          {
            kind: "button",
            id: `variable-remove-${row.uid}`,
            title: `Remove ${row.key.trim() || noun.toLowerCase()}`,
            symbol: "minus.circle.fill",
            destructive: true,
            onPress: () =>
              onChange((current) =>
                current.filter((entry) => entry.uid !== row.uid),
              ),
          },
        ],
      }),
    ),
    {
      id: "variables",
      title: rows.length ? undefined : title,
      footer:
        "Secret values are kept as credentials of the plugin and never shown again.",
      rows: [
        {
          kind: "button",
          id: "add-variable",
          title: command ? "Add variable" : "Add header",
          symbol: "plus",
          onPress: () =>
            onChange((current) => [...current, emptyVariableRow()]),
        },
      ],
    },
  ];
}

/** Sign-in picker value while no credential is chosen; never a valid key. */
const UNCHOSEN = ":choose";

/**
 * One MCP server of a plugin draft: a command that runs in the workspace,
 * or a URL with optional sign-in, plus its variables or headers.
 */
export function PluginMcpPage({
  draftKey,
  uid,
}: {
  draftKey: string;
  uid: string;
}): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const services = useServices();
  const { draft, update } = usePluginDraft(draftKey);
  const credentials = useUsableCredentials(draft?.form.scope).filter(
    (credential) => credential.credential_type === "env",
  );
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
  const oauthRequirements = draft.form.requirements.filter(
    (requirement) =>
      requirement.required &&
      requirement.credentialType === "mcp_oauth" &&
      requirement.reqKey.trim(),
  );
  const signIn =
    mcp.authType === "oauth" ? mcp.oauthRequirementKey || UNCHOSEN : "";
  const signInOptions = [
    { value: "", label: "None" },
    ...oauthRequirements.map((requirement) => ({
      value: requirement.reqKey,
      label:
        services.data?.find((entry) => entry.id === requirement.serviceId)
          ?.name ?? requirement.reqKey,
    })),
  ];
  if (!signInOptions.some((option) => option.value === signIn))
    signInOptions.push({ value: signIn, label: "Choose a credential" });

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
          value: mcp.name,
          autoFocus: !mcp.id && !mcp.name,
          onChange: (name) => edit({ name }),
        },
        {
          kind: "segmented",
          id: "mcp-kind",
          label: "Connection",
          value: mcp.kind,
          options: [
            { value: "command", label: "Command" },
            { value: "url", label: "URL" },
          ],
          onChange: (kind) =>
            update((form) => setMcpKind(form, uid, kind as McpKind)),
        },
      ],
    },
    mcp.kind === "command"
      ? {
          id: "command",
          footer: "Runs inside the workspace, e.g. npx -y <package>.",
          rows: [
            {
              kind: "text",
              id: "mcp-command",
              label: "Command line",
              monospace: true,
              value: mcp.commandLine,
              onChange: (commandLine) => edit({ commandLine }),
            },
          ],
        }
      : {
          id: "endpoint",
          footer:
            mcp.authType === "oauth"
              ? "Each user signs in with their own account. The URL must match the OAuth service."
              : undefined,
          rows: [
            {
              kind: "text",
              id: "mcp-url",
              label: "Server URL",
              keyboard: "url",
              value: mcp.url,
              onChange: (url) => edit({ url }),
            },
            {
              kind: "picker",
              id: "mcp-transport",
              label: "Transport",
              value: mcp.transport,
              groups: [
                {
                  options: [
                    { value: "streamable_http", label: "Streamable HTTP" },
                    { value: "sse", label: "SSE" },
                  ],
                },
              ],
              onChange: (transport) =>
                edit({ transport: transport as McpUrlTransport }),
            },
            ...(oauthRequirements.length || mcp.authType === "oauth"
              ? [
                  {
                    kind: "picker" as const,
                    id: "mcp-sign-in",
                    label: "Sign-in",
                    value: signIn,
                    groups: [{ options: signInOptions }],
                    onChange: (key: string) =>
                      update((form) => {
                        if (!key)
                          return setMcpAuthentication(form, uid, "none");
                        const next = setMcpAuthentication(form, uid, "oauth");
                        return key === UNCHOSEN
                          ? next
                          : setMcpOAuthRequirement(
                              next,
                              uid,
                              key,
                              services.data ?? [],
                            );
                      }),
                  },
                ]
              : []),
          ],
        },
    ...variableSections(
      mcp,
      credentials,
      (row, onSelect) =>
        navigation.choose({
          title: row.key.trim() || "Secret",
          value:
            row.source === "credential"
              ? row.credentialId
              : row.source === "per_user"
                ? EACH_WORKSPACE
                : ENTER_VALUE,
          groups: [
            {
              options: [
                { value: ENTER_VALUE, label: "Enter a value" },
                { value: EACH_WORKSPACE, label: "Each workspace adds its own" },
              ],
            },
            ...(credentials.length
              ? [
                  {
                    title: "Use a credential",
                    options: credentials.map((credential) => ({
                      value: credential.id,
                      label: credential.name,
                      subtitle: credential.service_name,
                    })),
                  },
                ]
              : []),
          ],
          onSelect,
        }),
      (change) =>
        update((form) =>
          updateEntry(form, "mcps", uid, (entry) => ({
            ...entry,
            variables: change(entry.variables),
          })),
        ),
    ),
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

/**
 * Adds the MCP servers of a pasted configuration: the `mcpServers` JSON of
 * Claude, Cursor or VS Code, a command line or a URL.
 */
export function PluginMcpImportPage({
  draftKey,
}: {
  draftKey: string;
}): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const { draft, update } = usePluginDraft(draftKey);
  const [text, setText] = useState("");
  const parsed = useMemo(
    () => (text.trim() ? parseMcpConfig(text) : null),
    [text],
  );
  if (!draft)
    return (
      <SettingsPage
        title="Paste configuration"
        sections={[]}
        empty={NO_DRAFT}
      />
    );

  const servers = parsed && !parsed.error ? parsed.servers : [];
  const add = () => {
    if (!servers.length) return;
    update((form) => ({ ...form, mcps: [...form.mcps, ...servers] }));
    navigation.pop();
  };
  return (
    <SettingsPage
      title="Paste configuration"
      sections={[
        {
          id: "configuration",
          footer: parsed
            ? (parsed.error ??
              `Adds ${servers.map((server) => server.name || "an MCP server").join(", ")}.`)
            : "Paste mcpServers JSON from a README, a command or a URL.",
          footerTone: parsed?.error ? "danger" : "muted",
          rows: [
            {
              kind: "text",
              id: "mcp-configuration",
              label: "Configuration",
              monospace: true,
              multiline: true,
              lines: [8, 18],
              autoFocus: true,
              value: text,
              onChange: setText,
            },
          ],
        },
      ]}
      primaryAction={{
        title: "Add",
        onPress: add,
        disabled: !servers.length,
        testID: "add-pasted-servers",
      }}
      testID="settings-plugin-mcp-import"
    />
  );
}

/** Credentials the plugin needs besides its secret variables. */
export function PluginRequirementsPage({
  draftKey,
}: {
  draftKey: string;
}): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const services = useServices();
  const { draft, update } = usePluginDraft(draftKey);
  const errors = useDraftErrors(draft?.form);
  if (!draft)
    return (
      <SettingsPage title="More credentials" sections={[]} empty={NO_DRAFT} />
    );

  const entryRoute = (uid: string) =>
    route("plugin-requirement", draftKey, uid);
  return (
    <SettingsPage
      title="More credentials"
      sections={[
        {
          id: "requirements",
          footer: `Credentials besides secret variables, such as an OAuth sign-in. Use them in values as ${PLACEHOLDER_EXAMPLE}.`,
          rows: [
            ...draft.form.requirements.map((requirement, index) => {
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
                  [
                    service?.name,
                    requirement.required ? "Required" : "Optional",
                    requirement.credentialId ? "Own credential" : "",
                  ]
                    .filter(Boolean)
                    .join(" · "),
                subtitleTone: problems.length
                  ? ("danger" as const)
                  : ("muted" as const),
                route: entryRoute(requirement.uid),
              };
            }),
            ...(services.error
              ? errorRows(
                  "services",
                  `Couldn’t load credential services: ${services.error}`,
                  () => void services.reload(),
                  services.loading,
                )
              : []),
            {
              kind: "button",
              id: "add-requirement",
              title: "Add credential",
              symbol: "plus",
              onPress: () => {
                const requirement = emptyRequirementForm();
                update((form) => ({
                  ...form,
                  requirements: [...form.requirements, requirement],
                }));
                navigation.push(entryRoute(requirement.uid));
              },
            },
          ],
        },
      ]}
      testID="settings-plugin-requirements"
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
  const { organizationId } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useServices();
  const { draft, update } = usePluginDraft(draftKey);
  const usable = useUsableCredentials(draft?.form.scope);
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
  const placeholder = `{{credential.${requirement.reqKey.trim() || "KEY"}}}`;
  const ofService = usable.filter(
    (credential) => credential.service_id === requirement.serviceId,
  );
  const linked = ofService.find(
    (credential) => credential.id === requirement.credentialId,
  );

  const rows: NativeFormRow[] = [
    {
      kind: "value",
      id: "requirement-service",
      label: "Credential service",
      value: service?.name ?? "Choose",
      onPress: () =>
        navigation.choose({
          title: "Credential service",
          value: requirement.serviceId,
          emptyText: services.error ?? "No credential services are available.",
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
              update((form) => setRequirementService(form, uid, chosen));
          },
        }),
    },
    {
      kind: "value",
      id: "requirement-credential",
      label: "Credential",
      value: requirement.credentialId
        ? (linked?.name ?? "Chosen credential")
        : "Each workspace adds its own",
      onPress: requirement.serviceId
        ? () =>
            navigation.choose({
              title: "Credential",
              value: requirement.credentialId || EACH_WORKSPACE,
              groups: [
                {
                  options: [
                    {
                      value: EACH_WORKSPACE,
                      label: "Each workspace adds its own",
                    },
                  ],
                },
                ...(ofService.length
                  ? [
                      {
                        title: "Always use",
                        options: ofService.map((credential) => ({
                          value: credential.id,
                          label: credential.name,
                        })),
                      },
                    ]
                  : []),
              ],
              onSelect: (value) =>
                edit({
                  credentialId: value === EACH_WORKSPACE ? "" : value,
                }),
            })
        : undefined,
    },
    {
      kind: "text",
      id: "requirement-key",
      label: "Key",
      monospace: true,
      value: requirement.reqKey,
      onChange: (reqKey) => edit({ reqKey }),
    },
    {
      kind: "text",
      id: "requirement-description",
      label: "Description",
      prose: true,
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
  ];
  return (
    <SettingsPage
      title={requirement.reqKey.trim() || "Requirement"}
      sections={[
        {
          id: "service",
          footer: problems.join(" ") || `Use it in values as ${placeholder}.`,
          footerTone: problems.length ? "danger" : "muted",
          rows,
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
