import React, { useState } from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import {
  EXPIRY_CHOICES,
  expiresAt,
  isValidExpiryDate,
  localDay,
  permissionsPayload,
  type ExpiryChoice,
  type PermissionForm,
} from "./apiKeys";
import { permissionSections } from "./permissionSections";
import { API_KEYS, API_KEY_PERMISSIONS } from "./resources";

/** Resource key of a new key's one-time secret, kept in memory only. */
export const apiKeySecret = (id: string) => `api-key-secret:${id}`;

/** Creates an API key; its secret is shown once on the next page. */
export function NewApiKeyPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const keys = useResource(resources, API_KEYS, () => client.apiKeys.list());
  const permissions = useResource(resources, API_KEY_PERMISSIONS, () =>
    client.apiKeys.permissions(),
  );
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState<ExpiryChoice>("never");
  const [customDate, setCustomDate] = useState(() => localDay(30));
  const [access, setAccess] = useState<PermissionForm>({
    full: true,
    selected: [],
  });
  const [busy, setBusy] = useState(false);
  const payload = permissionsPayload(access);
  const dateValid = expiry !== "custom" || isValidExpiryDate(customDate);
  const valid = Boolean(name.trim()) && payload !== null && dateValid;

  const create = async () => {
    if (!valid || payload === null) return;
    setBusy(true);
    try {
      const created = await client.apiKeys.create({
        name: name.trim(),
        expires_at: expiresAt(expiry, customDate),
        permissions: payload,
      });
      const { key, ...stored } = created;
      resources.set(apiKeySecret(created.id), key);
      keys.set((current) => [stored, ...(current ?? [])]);
      navigation.replace(route("api-key-created", created.id));
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

  const sections: NativeFormSection[] = [
    {
      id: "key",
      footer: dateValid
        ? "Expiry cannot be changed later."
        : "Choose a date from today on.",
      footerTone: dateValid ? "muted" : "danger",
      rows: [
        {
          kind: "text",
          id: "key-name",
          label: "Name",
          placeholder: "e.g. n8n prod",
          value: name,
          autoFocus: true,
          onChange: setName,
        },
        {
          kind: "picker",
          id: "expiry",
          label: "Expires",
          value: expiry,
          groups: [{ options: [...EXPIRY_CHOICES] }],
          onChange: (value) => setExpiry(value as ExpiryChoice),
        },
        ...(expiry === "custom"
          ? [
              {
                kind: "date" as const,
                id: "expiry-date",
                label: "Expiry date",
                mode: "date" as const,
                value: customDate,
                minimumDate: localDay(0),
                onChange: setCustomDate,
              },
            ]
          : []),
      ],
    },
    ...(permissions.data
      ? permissionSections(access, permissions.data, setAccess, {
          footer:
            payload === null ? "Select at least one permission." : undefined,
        })
      : [
          {
            id: "permissions",
            footer: permissions.error ?? "Loading permissions…",
            footerTone: permissions.error
              ? ("danger" as const)
              : ("muted" as const),
            rows: [],
          },
        ]),
  ];

  return (
    <SettingsPage
      title="New API key"
      sections={sections}
      primaryAction={{
        title: "Create",
        onPress: () => void create(),
        busy,
        disabled: !valid || busy || (!access.full && !permissions.data),
        testID: "create-api-key",
      }}
      testID="settings-new-api-key"
    />
  );
}
