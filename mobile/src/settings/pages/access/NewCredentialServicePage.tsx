import React, { useState } from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import {
  SERVICE_TYPES,
  emptyServiceForm,
  isServiceFormValid,
  serviceCreateInput,
  validateServiceForm,
  type CredentialServiceForm,
  type CredentialServiceType,
} from "./credentialServices";
import { ORGANIZATION_SERVICES } from "./resources";

/** Creates an organization-owned credential service; it starts active. */
export function NewCredentialServicePage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useResource(resources, ORGANIZATION_SERVICES, () =>
    client.credentialServices.list(),
  );
  const [form, setForm] = useState<CredentialServiceForm>(emptyServiceForm);
  const [busy, setBusy] = useState(false);
  const errors = validateServiceForm(form);
  const edit = (next: Partial<CredentialServiceForm>) =>
    setForm((current) => ({ ...current, ...next }));

  const create = async () => {
    if (!isServiceFormValid(form)) return;
    setBusy(true);
    try {
      const created = await client.credentialServices.create(
        serviceCreateInput(form),
      );
      services.set((current) => [...(current ?? []), created]);
      resources.invalidate("credential-services:available");
      showToast({ title: "Service created", tone: "success" });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "Failed to create service",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const target: NativeFormSection =
    form.type === "env"
      ? {
          id: "target",
          footer:
            errors.envVarName ?? "Injected into workspaces under this name.",
          footerTone: errors.envVarName ? "danger" : "muted",
          rows: [
            {
              kind: "text",
              id: "env-var",
              label: "Environment variable",
              placeholder: "GITHUB_TOKEN",
              monospace: true,
              value: form.envVarName,
              onChange: (envVarName) =>
                edit({ envVarName: envVarName.toUpperCase() }),
            },
          ],
        }
      : form.type === "file"
        ? {
            id: "target",
            footer: errors.targetPath ?? "Written to this path in workspaces.",
            footerTone: errors.targetPath ? "danger" : "muted",
            rows: [
              {
                kind: "text",
                id: "target-path",
                label: "Target path",
                placeholder: "~/.config/auth.json",
                monospace: true,
                value: form.targetPath,
                onChange: (targetPath) => edit({ targetPath }),
              },
            ],
          }
        : form.type === "mcp_oauth"
          ? {
              id: "target",
              footer:
                errors.oauthServerUrl ??
                "Fixed HTTPS endpoint without credentials, query, or fragment. OAuth requirements must match this exact endpoint.",
              footerTone: errors.oauthServerUrl ? "danger" : "muted",
              rows: [
                {
                  kind: "text",
                  id: "oauth-url",
                  label: "MCP server URL",
                  placeholder: "https://mcp.example.com/mcp",
                  keyboard: "url",
                  value: form.oauthServerUrl,
                  onChange: (oauthServerUrl) => edit({ oauthServerUrl }),
                },
              ],
            }
          : {
              id: "target",
              footer: "An Ed25519 key pair is generated for each credential.",
              rows: [],
            };

  const sections: NativeFormSection[] = [
    {
      id: "service",
      footer: errors.name,
      footerTone: errors.name ? "danger" : "muted",
      rows: [
        {
          kind: "text",
          id: "name",
          label: "Name",
          placeholder: "GitHub Enterprise",
          value: form.name,
          autoFocus: true,
          onChange: (name) => edit({ name }),
        },
        {
          kind: "picker",
          id: "type",
          label: "Type",
          value: form.type,
          groups: [{ options: SERVICE_TYPES }],
          onChange: (type) => edit({ type: type as CredentialServiceType }),
        },
      ],
    },
    target,
    {
      id: "details",
      footer: "Deactivating a service later only stops new credentials.",
      rows: [
        {
          kind: "text",
          id: "label",
          label: "Label (optional)",
          placeholder: "API token",
          value: form.label,
          onChange: (label) => edit({ label }),
        },
        {
          kind: "text",
          id: "description",
          label: "Description",
          placeholder: "Used for repository access and API integrations.",
          value: form.description,
          multiline: true,
          prose: true,
          lines: [2, 4],
          onChange: (description) => edit({ description }),
        },
      ],
    },
  ];

  return (
    <SettingsPage
      title="New service"
      sections={sections}
      primaryAction={{
        title: "Create",
        onPress: () => void create(),
        busy,
        disabled: !isServiceFormValid(form) || busy,
        testID: "create-credential-service",
      }}
      testID="settings-new-credential-service"
    />
  );
}
