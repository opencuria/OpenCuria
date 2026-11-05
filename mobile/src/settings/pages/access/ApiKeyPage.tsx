import React, { useEffect, useState } from "react";
import { Alert } from "react-native";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { autosaveSection } from "../../sections";
import { useSettings } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { useAutosave } from "../../useAutosave";
import {
  expiryLabel,
  keyStatus,
  permissionForm,
  permissionsPayload,
  type PermissionForm,
} from "./apiKeys";
import { formatDate, formatRelative } from "../../format";
import { permissionSections } from "./permissionSections";
import { API_KEYS, API_KEY_PERMISSIONS } from "./resources";

/**
 * One API key: its usage, permissions that save automatically while it is
 * active, and permanent revocation.
 */
export function ApiKeyPage({ keyId }: { keyId: string }): React.JSX.Element {
  const { client, resources } = useSettings();
  const keys = useResource(resources, API_KEYS, () => client.apiKeys.list());
  const permissions = useResource(resources, API_KEY_PERMISSIONS, () =>
    client.apiKeys.permissions(),
  );
  const key = keys.data?.find((entry) => entry.id === keyId);
  const [access, setAccess] = useState<PermissionForm | null>(null);
  const [revoking, setRevoking] = useState(false);
  useEffect(() => {
    if (key && permissions.data && !access)
      setAccess(permissionForm(key.permissions, permissions.data));
  }, [key, permissions.data, access]);
  const active = key?.is_active ?? false;
  const payload = access ? permissionsPayload(access) : null;
  const autosave = useAutosave(
    access,
    async () => {
      const saved = await client.apiKeys.updatePermissions(keyId, payload!);
      keys.set((current) =>
        (current ?? []).map((entry) => (entry.id === saved.id ? saved : entry)),
      );
    },
    {
      valid: payload !== null,
      enabled: active,
      failure: "Failed to update permissions",
    },
  );

  if (!key)
    return (
      <SettingsPage
        title="API key"
        sections={[]}
        loading={!keys.data && !keys.error}
        empty={{
          title: "API key unavailable",
          description: keys.error ?? "This key no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-api-key"
      />
    );

  const revoke = () =>
    Alert.alert(
      `Revoke ${key.name}?`,
      `Any integration using ${key.key_prefix}… will immediately lose access. This cannot be undone.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Revoke",
          style: "destructive",
          onPress: async () => {
            setRevoking(true);
            try {
              await client.apiKeys.revoke(key.id);
              keys.set((current) =>
                (current ?? []).map((entry) =>
                  entry.id === key.id ? { ...entry, is_active: false } : entry,
                ),
              );
              showToast({
                title: "API key revoked",
                message: "The key can no longer be used.",
                tone: "success",
              });
            } catch (cause) {
              showToast({
                title: "Revocation failed",
                message: errorMessage(cause, "Please try again."),
                tone: "error",
              });
            } finally {
              setRevoking(false);
            }
          },
        },
      ],
    );

  const sections: NativeFormSection[] = [
    {
      id: "details",
      rows: [
        {
          kind: "value",
          id: "prefix",
          label: "Key",
          value: `${key.key_prefix}…`,
          monospace: true,
        },
        {
          kind: "value",
          id: "status",
          label: "Status",
          value: keyStatus(key),
          tone: keyStatus(key) === "Active" ? "success" : "danger",
        },
        {
          kind: "value",
          id: "expires",
          label: "Expiry",
          value: expiryLabel(key),
        },
        {
          kind: "value",
          id: "created",
          label: "Created",
          value: formatDate(key.created_at),
        },
        {
          kind: "value",
          id: "last-used",
          label: "Last used",
          value: formatRelative(key.last_used_at, "Never"),
        },
      ],
    },
    ...(active && access && permissions.data
      ? [
          ...permissionSections(access, permissions.data, setAccess),
          autosaveSection(autosave),
          {
            id: "revoke",
            rows: [
              {
                kind: "button" as const,
                id: "revoke",
                title: "Revoke key",
                symbol: "trash" as const,
                destructive: true,
                busy: revoking,
                disabled: revoking,
                onPress: revoke,
              },
            ],
          },
        ]
      : active
        ? [
            {
              id: "permissions",
              footer: permissions.error ?? "Loading permissions…",
              footerTone: permissions.error
                ? ("danger" as const)
                : ("muted" as const),
              rows: [],
            },
          ]
        : [
            {
              id: "revoked",
              footer: "Revoked keys cannot be used or changed.",
              rows: [],
            },
          ]),
  ];

  return (
    <SettingsPage
      title={key.name}
      sections={sections}
      testID="settings-api-key"
    />
  );
}
