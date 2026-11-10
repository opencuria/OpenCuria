import React, { useEffect, useState } from "react";
import type { Organization } from "@opencuria/client";
import { formatMinutesAsDuration } from "@opencuria/chat-core/workspaceLifecycle";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../ui/NativeForm.types";
import { useResource } from "../resources";
import { useSettings } from "../SettingsContext";
import { SettingsPage } from "../SettingsPage";
import { autosaveSection, loadErrorSections } from "../sections";
import { useAutosave } from "../useAutosave";

const MAX_TIMEOUT_MINUTES = 10080;
const DEFAULT_TIMEOUT_MINUTES = 240;
const PRESETS = [30, 60, 120, 240, 480, 720, 1440, 4320, 10080];
const CUSTOM = "custom";

interface PolicyForm {
  stopInactive: boolean;
  /** Text while editing a custom value; validated before saving. */
  minutes: string;
  versions: number;
}

function toForm(organization: Organization): PolicyForm {
  const minutes = organization.workspace_auto_stop_timeout_minutes;
  return {
    stopInactive: minutes != null,
    minutes: String(minutes ?? DEFAULT_TIMEOUT_MINUTES),
    versions: organization.image_versions_to_keep ?? 2,
  };
}

/** A whole number of minutes from 1 to 7 days, or `null` when invalid. */
export function parseTimeoutMinutes(text: string): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const value = Number(text.trim());
  return value >= 1 && value <= MAX_TIMEOUT_MINUTES ? value : null;
}

/**
 * Organization-wide workspace policy ("General" in the webapp): auto-stop
 * after inactivity and how many image versions to keep. Admin-only (members
 * never reach it); changes save automatically.
 */
export function WorkspacePolicyPage(): React.JSX.Element {
  const { client, organizationId, organization, resources } = useSettings();
  const policy = useResource(
    resources,
    `organization:${organizationId ?? ""}`,
    () => client.organizations.get(organizationId!),
    { enabled: Boolean(organizationId) },
  );
  const [form, setForm] = useState<PolicyForm | null>(null);
  const [custom, setCustom] = useState(false);
  useEffect(() => {
    if (policy.data && !form) {
      const initial = toForm(policy.data);
      setForm(initial);
      setCustom(!PRESETS.includes(Number(initial.minutes)));
    }
  }, [policy.data, form]);

  const minutes = form ? parseTimeoutMinutes(form.minutes) : null;
  const valid = Boolean(
    form &&
    (!form.stopInactive || minutes !== null) &&
    form.versions >= 1 &&
    form.versions <= 20,
  );
  const autosave = useAutosave(
    form,
    async (value) => {
      const saved = await client.organizations.updateWorkspacePolicy(
        organizationId!,
        {
          workspace_auto_stop_timeout_minutes: value.stopInactive
            ? parseTimeoutMinutes(value.minutes)
            : null,
          image_versions_to_keep: value.versions,
        },
      );
      policy.set(saved);
    },
    {
      valid,
      enabled: Boolean(organizationId),
      failure: "Failed to update workspace policy",
    },
  );

  const edit = (next: Partial<PolicyForm>) =>
    setForm((current) => (current ? { ...current, ...next } : current));
  const name = policy.data?.name ?? organization?.name;
  const timeoutRows: NativeFormRow[] =
    form?.stopInactive && form
      ? [
          {
            kind: "picker",
            id: "timeout",
            label: "Stop after",
            value: custom ? CUSTOM : form.minutes,
            groups: [
              {
                options: [
                  ...PRESETS.map((value) => ({
                    value: String(value),
                    label: formatMinutesAsDuration(value),
                  })),
                  { value: CUSTOM, label: "Custom" },
                ],
              },
            ],
            onChange: (value) => {
              setCustom(value === CUSTOM);
              if (value !== CUSTOM) edit({ minutes: value });
            },
          },
          ...(custom
            ? [
                {
                  kind: "text" as const,
                  id: "timeout-minutes",
                  label: "Minutes",
                  placeholder: "Minutes (1–10080)",
                  keyboard: "number" as const,
                  value: form.minutes,
                  onChange: (value: string) => edit({ minutes: value }),
                },
              ]
            : []),
        ]
      : [];

  const sections: NativeFormSection[] = form
    ? [
        {
          id: "auto-stop",
          title: "Inactive workspaces",
          footer:
            form.stopInactive && minutes === null
              ? "Enter a whole number of minutes between 1 and 10080."
              : `Applies to all workspaces${name ? ` in ${name}` : ""}. Inactivity means no prompts, terminal input, or file interactions. Active prompt sessions prevent auto-stop until they finish.`,
          footerTone:
            form.stopInactive && minutes === null ? "danger" : "muted",
          rows: [
            {
              kind: "toggle",
              id: "stop-inactive",
              label: "Stop inactive workspaces",
              subtitle: form.stopInactive
                ? minutes !== null
                  ? `Inactive workspaces stop after ${formatMinutesAsDuration(minutes)}.`
                  : undefined
                : "Inactive workspaces keep running until someone stops them.",
              value: form.stopInactive,
              onChange: (value) => edit({ stopInactive: value }),
            },
            ...timeoutRows,
          ],
        },
        {
          id: "images",
          title: "Images",
          footer:
            "Older versions are deleted automatically once no workspace uses them.",
          rows: [
            {
              kind: "stepper",
              id: "versions",
              label: "Versions to keep",
              value: form.versions,
              min: 1,
              max: 20,
              step: 1,
              format: (value) => String(value),
              onChange: (value) => edit({ versions: value }),
            },
          ],
        },
        autosaveSection(autosave),
      ]
    : [];

  return (
    <SettingsPage
      title="Workspace policy"
      sections={
        policy.error && !form
          ? loadErrorSections(policy.error, () => void policy.reload())
          : sections
      }
      loading={!form && !policy.error}
      testID="settings-policy"
    />
  );
}
