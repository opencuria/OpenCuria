import React, { useState } from "react";
import { Linking } from "react-native";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { BROWSER_HANDOFF_NOTE } from "./CredentialPage";
import { oauthHandoffUrl, serviceUnavailableReason } from "./credentials";
import { serviceTypeLabel } from "./credentialServices";
import { ACTIVE_SERVICES, CREDENTIALS, loadServices } from "./resources";

/**
 * Adds a credential for an active service: a value for environment and
 * file credentials, a generated key pair for SSH, and a browser handoff
 * for MCP OAuth, whose flow the server binds to a browser session.
 */
export function NewCredentialPage({
  serviceId: initialServiceId,
}: {
  /** A service chosen beforehand, e.g. from a plugin's requirements. */
  serviceId?: string;
}): React.JSX.Element {
  const { client, resources, isAdmin, serverUrl } = useSettings();
  const navigation = useSettingsNavigation();
  const services = useResource(resources, ACTIVE_SERVICES, () =>
    loadServices(client),
  );
  const credentials = useResource(resources, CREDENTIALS, () =>
    client.credentials.list(),
  );
  const [serviceId, setServiceId] = useState(initialServiceId ?? "");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [shared, setShared] = useState(false);
  const [busy, setBusy] = useState(false);

  const active = (services.data ?? []).filter((service) => service.is_active);
  const service = (services.data ?? []).find((entry) => entry.id === serviceId);
  const unavailable =
    serviceId && services.data ? serviceUnavailableReason(service) : null;
  const usable = service && !unavailable ? service : undefined;
  const type = usable?.credential_type;
  const needsValue = type === "env" || type === "file";
  const valid = Boolean(usable) && (!needsValue || Boolean(value.trim()));

  const choose = () =>
    navigation.choose({
      title: "Service",
      value: serviceId,
      emptyText: "No active services. Ask an admin to add or activate one.",
      groups: [
        {
          options: active.map((entry) => ({
            value: entry.id,
            label: entry.name,
            subtitle: serviceTypeLabel(entry.credential_type),
          })),
        },
      ],
      onSelect: (id) => {
        setServiceId(id);
        setName("");
        setValue("");
        setShared(false);
      },
    });

  const save = async () => {
    if (!usable || !valid) return;
    if (type === "mcp_oauth") {
      await Linking.openURL(
        oauthHandoffUrl(serverUrl, { serviceId: usable.id }),
      );
      navigation.pop();
      return;
    }
    setBusy(true);
    try {
      await client.credentials.create({
        service_id: usable.id,
        name: name.trim() || undefined,
        value: type === "ssh_key" ? undefined : value,
        organization_credential: isAdmin && shared,
      });
      await credentials.reload();
      showToast({ title: "Credential created", tone: "success" });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "Creation failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const sections: NativeFormSection[] = services.data
    ? [
        {
          id: "service",
          footer:
            unavailable ??
            (active.length
              ? undefined
              : "No active services. Ask an admin to add or activate one."),
          footerTone: unavailable ? "danger" : "muted",
          rows: [
            {
              kind: "value",
              id: "service",
              testID: "credential-service",
              label: "Service",
              value: service?.name ?? "Choose",
              onPress: choose,
            },
          ],
        },
        ...(usable && type === "mcp_oauth"
          ? [{ id: "oauth", footer: BROWSER_HANDOFF_NOTE, rows: [] }]
          : []),
        ...(usable && type !== "mcp_oauth"
          ? [
              {
                id: "credential",
                footer:
                  type === "ssh_key"
                    ? "An Ed25519 SSH key pair is generated automatically."
                    : type === "file"
                      ? `Written to ${usable.target_path}. Encrypted and never shown again.`
                      : "Encrypted and never shown again.",
                rows: [
                  {
                    kind: "text" as const,
                    id: "name",
                    label: "Name (optional)",
                    placeholder: `${usable.name} Credential`,
                    value: name,
                    onChange: setName,
                  },
                  ...(type === "file"
                    ? [
                        {
                          kind: "text" as const,
                          id: "value",
                          label: "Contents",
                          placeholder: `Contents for ${usable.target_path}`,
                          multiline: true,
                          monospace: true,
                          lines: [4, 10] as [number, number],
                          value,
                          onChange: setValue,
                        },
                      ]
                    : type === "env"
                      ? [
                          {
                            kind: "text" as const,
                            id: "value",
                            label: "Value",
                            placeholder: `Value for ${usable.env_var_name}`,
                            secure: true,
                            value,
                            onChange: setValue,
                          },
                        ]
                      : []),
                ],
              },
            ]
          : []),
        ...(usable && isAdmin && type !== "mcp_oauth"
          ? [
              {
                id: "scope",
                rows: [
                  {
                    kind: "toggle" as const,
                    id: "shared",
                    label: "Share with entire organization",
                    value: shared,
                    onChange: setShared,
                  },
                ],
              },
            ]
          : []),
      ]
    : [];

  return (
    <SettingsPage
      title="New credential"
      sections={sections}
      loading={!services.data && !services.error}
      empty={{
        title: "Services unavailable",
        description: services.error ?? undefined,
        symbol: "exclamationmark.triangle.fill",
      }}
      primaryAction={{
        title: type === "mcp_oauth" ? "Continue" : "Save",
        onPress: () => void save(),
        busy,
        disabled: !valid || busy,
        testID: "save-new-credential",
      }}
      testID="settings-new-credential"
    />
  );
}
