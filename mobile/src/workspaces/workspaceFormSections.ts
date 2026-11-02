/**
 * Form sections shared by "New workspace" and the workspace detail screen:
 * plugins with their credential requirements, credentials, and resources.
 */
import {
  evaluateWorkspacePluginSelection,
  isCredentialConnected,
  toggleWorkspaceCredentialSelection,
} from "@opencuria/chat-core/workspaceConfiguration";
import {
  areQemuResourcesValid,
  type QemuResourceLimits,
  type QemuResources,
} from "@opencuria/chat-core/workspaceCreation";
import type { Credential, Plugin } from "@opencuria/client";
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
  const required = plugin.credential_requirements.filter(
    (requirement) => requirement.required,
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
        ? `Select a ${names} credential below`
        : `Needs a ${names} credential – add it in the web app`,
    tone: "warning",
  };
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
}): { sections: NativeFormSection[]; valid: boolean } {
  if (catalog.error)
    return {
      valid: false,
      sections: [
        {
          id: "catalog",
          title: "Plugins & credentials",
          rows: [
            {
              kind: "note",
              id: "catalog-error",
              text: `Could not load plugins or credentials: ${catalog.error}`,
              tone: "danger",
            },
            {
              kind: "button",
              id: "catalog-retry",
              title: "Retry",
              symbol: "arrow.counterclockwise",
              busy: catalog.loading,
              onPress: () => {
                void catalog.reload();
              },
            },
          ],
        },
      ],
    };
  if (!catalog.ready)
    return {
      valid: false,
      sections: [
        {
          id: "catalog",
          title: "Plugins & credentials",
          rows: [
            {
              kind: "note",
              id: "catalog-loading",
              text: "Loading plugins and credentials…",
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
  const togglePlugin = (id: string) =>
    onPluginIds(
      pluginIds.includes(id)
        ? pluginIds.filter((entry) => entry !== id)
        : [...pluginIds, id],
    );
  const pluginRows: NativeFormRow[] = [
    ...selection.catalog.map((plugin): NativeFormRow => {
      const subtitle = pluginSubtitle(
        plugin,
        pluginIds.includes(plugin.id),
        catalog.credentials,
        selection.selectedCredentials,
      );
      return {
        kind: "toggle",
        id: `plugin-${plugin.id}`,
        label: plugin.name,
        subtitle: subtitle.text,
        subtitleTone: subtitle.tone,
        value: pluginIds.includes(plugin.id),
        onChange: () => togglePlugin(plugin.id),
        disabled,
      };
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
      return {
        kind: "toggle",
        id: `credential-${credential.id}`,
        label: credential.name,
        subtitle: [
          credential.service_name,
          credential.scope === "organization" ? "Organization" : "Personal",
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
        ? "Select all required credentials before saving."
        : undefined,
      footerTone: "warning",
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
            text: "No credentials yet. Add them in the web app.",
          },
        ],
    footer: selection.duplicateServiceIds.length
      ? "Choose only one credential per service."
      : "One credential per service. Secrets stay on the server.",
    footerTone: selection.duplicateServiceIds.length ? "danger" : "muted",
  });
  return { sections, valid: selection.valid };
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
