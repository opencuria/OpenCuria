/**
 * Form sections shared by "New workspace" and the workspace detail screen:
 * plugins with their credential requirements, credentials, and resources.
 */
import {
  evaluateWorkspacePluginSelection,
  autoAttachCredentials,
  isCredentialConnected,
  pluginsUsingCredential,
  toggleWorkspaceCredentialSelection,
  workspaceSelectionSummary,
  type WorkspaceSelectionSummary,
} from "@opencuria/chat-core/workspaceConfiguration";
import {
  areQemuResourcesValid,
  type QemuResourceLimits,
  type QemuResources,
} from "@opencuria/chat-core/workspaceCreation";
import type { Credential, Plugin } from "@opencuria/client";
import { errorRows } from "../ui/formStates";
import type { NativeFormRow, NativeFormSection } from "../ui/NativeForm.types";
import type { WorkspaceCatalog } from "./useWorkspaceCatalog";
import {
  diskStep,
  formatDisk,
  formatMemory,
  formatVcpus,
  memoryStep,
} from "./workspacePresentation";

const DESKTOP_PRESETS: Array<[number, number]> = [
  [1280, 800],
  [1440, 900],
  [1920, 1080],
  [2560, 1440],
  [3840, 2160],
];

function pluginSubtitle(
  plugin: Plugin,
  selected: boolean,
  credentials: Credential[],
  selectedCredentials: Credential[],
): { text?: string; tone?: "muted" | "warning" } {
  // Credentials the plugin brings itself need nothing from the workspace.
  const required = plugin.credential_requirements.filter(
    (requirement) => requirement.required && !requirement.credential_id,
  );
  if (!selected || !required.length)
    return { text: plugin.description || undefined };
  const missing = required.filter(
    (requirement) =>
      !selectedCredentials.some(
        (credential) =>
          credential.service_id === requirement.service_id &&
          isCredentialConnected(credential),
      ),
  );
  if (!missing.length)
    return {
      text: `Uses ${required.map((requirement) => requirement.service_name).join(", ")}`,
    };
  const available = missing.filter((requirement) =>
    credentials.some(
      (credential) =>
        credential.service_id === requirement.service_id &&
        isCredentialConnected(credential),
    ),
  );
  const names = missing
    .map((requirement) => requirement.service_name)
    .join(", ");
  return {
    text:
      available.length === missing.length
        ? `Choose a credential for ${names}`
        : `Add a credential for ${names} in Settings → Credentials`,
    tone: "warning",
  };
}

const CHOOSE = "";
const NONE = "__none__";

/**
 * Rows below an enabled plugin, one per credential it takes from the
 * workspace: a picker of the matching credentials, or how to get one.
 */
function requirementRows(
  plugin: Plugin,
  credentials: Credential[],
  credentialIds: string[],
  onCredentialIds: (ids: string[]) => void,
  disabled?: boolean,
): NativeFormRow[] {
  return plugin.credential_requirements
    .filter((requirement) => !requirement.credential_id)
    .flatMap((requirement): NativeFormRow[] => {
      const ofService = credentials.filter(
        (credential) => credential.service_id === requirement.service_id,
      );
      const choices = ofService.filter(isCredentialConnected);
      const id = `requirement-${plugin.id}-${requirement.id}`;
      const label = requirement.required
        ? requirement.service_name
        : `${requirement.service_name} (optional)`;
      if (!choices.length) {
        if (!requirement.required) return [];
        const disconnected = ofService[0];
        return [
          {
            kind: "note",
            id,
            symbol: "key.horizontal",
            tone: "warning",
            text: disconnected
              ? `Reconnect ${disconnected.name} in the web app`
              : `Add a credential for ${requirement.service_name} in Settings → Credentials`,
          },
        ];
      }
      const attached = choices.find((credential) =>
        credentialIds.includes(credential.id),
      );
      return [
        {
          kind: "picker",
          id,
          label,
          symbol: "key.horizontal",
          value: attached?.id ?? (requirement.required ? CHOOSE : NONE),
          groups: [
            {
              options: [
                requirement.required
                  ? { value: CHOOSE, label: "Choose", disabled: true }
                  : { value: NONE, label: "None" },
                ...choices.map((credential) => ({
                  value: credential.id,
                  label: credential.name,
                })),
              ],
            },
          ],
          onChange: (value) => {
            const others = credentialIds.filter(
              (entry) =>
                !ofService.some((credential) => credential.id === entry),
            );
            onCredentialIds(
              value && value !== NONE ? [...others, value] : others,
            );
          },
          disabled,
        },
      ];
    });
}

/** Plugin and credential sections plus whether the selection can be saved. */
export function pluginCredentialSections({
  catalog,
  organizationId,
  pluginIds,
  credentialIds,
  onPluginIds,
  onCredentialIds,
  disabled,
}: {
  catalog: WorkspaceCatalog;
  organizationId: string | null;
  pluginIds: string[];
  credentialIds: string[];
  onPluginIds: (ids: string[]) => void;
  onCredentialIds: (ids: string[]) => void;
  disabled?: boolean;
}): {
  sections: NativeFormSection[];
  valid: boolean;
  /** One line for a row linking to these sections; `null` while loading. */
  summary: WorkspaceSelectionSummary | null;
} {
  if (catalog.error)
    return {
      valid: false,
      summary: { text: "Couldn’t load", issue: catalog.error },
      sections: [
        {
          id: "catalog",
          title: "Plugins & credentials",
          rows: errorRows(
            "catalog",
            catalog.error,
            () => void catalog.reload(),
            catalog.loading,
          ),
        },
      ],
    };
  if (!catalog.ready)
    return {
      valid: false,
      summary: null,
      sections: [
        {
          id: "catalog",
          title: "Plugins & credentials",
          rows: [
            {
              kind: "note",
              id: "catalog-loading",
              text: "Loading plugins and credentials…",
              busy: true,
            },
          ],
        },
      ],
    };
  const selection = evaluateWorkspacePluginSelection({
    plugins: catalog.plugins,
    credentials: catalog.credentials,
    organizationId,
    pluginIds,
    credentialIds,
  });
  const togglePlugin = (id: string) => {
    if (pluginIds.includes(id)) {
      onPluginIds(pluginIds.filter((entry) => entry !== id));
      return;
    }
    onPluginIds([...pluginIds, id]);
    // Attach the only matching credential so nobody has to pick it.
    const plugin = selection.catalog.find((entry) => entry.id === id);
    const attached = plugin
      ? autoAttachCredentials(plugin, credentialIds, catalog.credentials)
      : credentialIds;
    if (attached !== credentialIds) onCredentialIds(attached);
  };
  const pluginRows: NativeFormRow[] = [
    ...selection.catalog.flatMap((plugin): NativeFormRow[] => {
      const selected = pluginIds.includes(plugin.id);
      const subtitle = pluginSubtitle(
        plugin,
        selected,
        catalog.credentials,
        selection.selectedCredentials,
      );
      return [
        {
          kind: "toggle",
          id: `plugin-${plugin.id}`,
          label: plugin.name,
          subtitle: subtitle.text,
          subtitleTone: subtitle.tone,
          value: selected,
          onChange: () => togglePlugin(plugin.id),
          disabled,
        },
        ...(selected
          ? requirementRows(
              plugin,
              catalog.credentials,
              credentialIds,
              onCredentialIds,
              disabled,
            )
          : []),
      ];
    }),
    ...selection.unavailablePluginIds.map(
      (id): NativeFormRow => ({
        kind: "toggle",
        id: `plugin-${id}`,
        label: `Unavailable plugin (${id.slice(0, 8)})`,
        subtitle: "Turn off to save",
        subtitleTone: "danger",
        value: true,
        onChange: () => togglePlugin(id),
        disabled,
      }),
    ),
  ];
  const credentialRows: NativeFormRow[] = [
    ...catalog.credentials.map((credential): NativeFormRow => {
      const connected = isCredentialConnected(credential);
      const selected = credentialIds.includes(credential.id);
      const users = selected
        ? pluginsUsingCredential(selection.catalog, pluginIds, credential)
        : [];
      return {
        kind: "toggle",
        id: `credential-${credential.id}`,
        label: credential.name,
        subtitle: [
          credential.service_name,
          credential.scope === "organization" ? "Organization" : "Personal",
          users.length
            ? `Used by ${users.map((plugin) => plugin.name).join(", ")}`
            : null,
          connected ? null : "Reconnect in the web app",
        ]
          .filter(Boolean)
          .join(" · "),
        subtitleTone: connected ? "muted" : "warning",
        value: selected,
        disabled: disabled || (!connected && !selected),
        onChange: () =>
          onCredentialIds(
            toggleWorkspaceCredentialSelection(
              credentialIds,
              credential,
              catalog.credentials,
            ),
          ),
      };
    }),
    ...selection.unavailableCredentialIds.map(
      (id): NativeFormRow => ({
        kind: "toggle",
        id: `credential-${id}`,
        label: `Unavailable credential (${id.slice(0, 8)})`,
        subtitle: "Turn off to save",
        subtitleTone: "danger",
        value: true,
        onChange: () =>
          onCredentialIds(credentialIds.filter((entry) => entry !== id)),
        disabled,
      }),
    ),
  ];
  const sections: NativeFormSection[] = [];
  if (pluginRows.length)
    sections.push({
      id: "plugins",
      title: "Plugins",
      rows: pluginRows,
      footer: selection.missingRequired.length
        ? "Choose the credentials the selected plugins need before saving."
        : "Skills and tools for agents in this workspace.",
      footerTone: selection.missingRequired.length ? "warning" : "muted",
    });
  sections.push({
    id: "credentials",
    title: "Credentials",
    rows: credentialRows.length
      ? credentialRows
      : [
          {
            kind: "note",
            id: "credentials-empty",
            text: "No credentials yet. Add them in Settings → Credentials.",
          },
        ],
    footer: selection.duplicateServiceIds.length
      ? "Choose only one credential per service."
      : "One credential per service. Secrets stay on the server.",
    footerTone: selection.duplicateServiceIds.length ? "danger" : "muted",
  });
  return {
    sections,
    valid: selection.valid,
    summary: workspaceSelectionSummary(selection, pluginIds, credentialIds),
  };
}

/** vCPU, memory and storage steppers within the runner's limits. */
export function resourceRows({
  resources,
  limits,
  onChange,
  disabled,
}: {
  resources: QemuResources;
  limits: QemuResourceLimits;
  onChange: (resources: QemuResources) => void;
  disabled?: boolean;
}): NativeFormRow[] {
  return [
    {
      kind: "stepper",
      id: "vcpus",
      label: "CPU",
      value: resources.vcpus,
      min: limits.minVcpus,
      max: limits.maxVcpus,
      step: 1,
      format: formatVcpus,
      onChange: (vcpus) => onChange({ ...resources, vcpus }),
      disabled,
    },
    {
      kind: "stepper",
      id: "memory",
      label: "Memory",
      value: resources.memoryMb,
      min: limits.minMemoryMb,
      max: limits.maxMemoryMb,
      step: memoryStep(resources.memoryMb),
      format: formatMemory,
      onChange: (memoryMb) => onChange({ ...resources, memoryMb }),
      disabled,
    },
    {
      kind: "stepper",
      id: "disk",
      label: "Storage",
      value: resources.diskSizeGb,
      min: limits.minDiskSizeGb,
      max: limits.maxDiskSizeGb,
      step: diskStep(resources.diskSizeGb),
      format: formatDisk,
      onChange: (diskSizeGb) => onChange({ ...resources, diskSizeGb }),
      disabled,
    },
  ];
}

export function resourcesValid(
  resources: QemuResources,
  limits: QemuResourceLimits,
): boolean {
  return areQemuResourcesValid(resources, limits);
}

/** Common desktop sizes, keeping a custom size set in the web app selectable. */
export function desktopSizeRow({
  width,
  height,
  onChange,
  disabled,
}: {
  width: number;
  height: number;
  onChange: (width: number, height: number) => void;
  disabled?: boolean;
}): NativeFormRow {
  const presets = DESKTOP_PRESETS.some(([w, h]) => w === width && h === height)
    ? DESKTOP_PRESETS
    : [...DESKTOP_PRESETS, [width, height] as [number, number]];
  return {
    kind: "picker",
    id: "desktop",
    label: "Desktop size",
    value: `${width}x${height}`,
    groups: [
      {
        options: presets.map(([w, h]) => ({
          value: `${w}x${h}`,
          label: `${w} × ${h}`,
        })),
      },
    ],
    onChange: (value) => {
      const [w, h] = value.split("x").map(Number);
      if (w && h) onChange(w, h);
    },
    disabled,
  };
}
