import React, { useEffect, useState } from "react";
import { Alert, Linking } from "react-native";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { useForegroundReload } from "../../useForegroundReload";
import {
  acceptsValue,
  canDisconnect,
  canManageCredential,
  canReconnect,
  credentialScopeLabel,
  credentialTypeLabel,
  credentialUpdate,
  oauthHandoffUrl,
  oauthStatus,
} from "./credentials";
import { formatDate } from "../../format";
import { CREDENTIALS } from "./resources";

export const BROWSER_HANDOFF_NOTE =
  "Finish connecting in your browser. You need to be signed in to OpenCuria there; this list updates when you return.";

/**
 * One credential: its service and target, renaming or replacing its value
 * for those who manage it, OAuth reconnection in the browser, and deletion.
 */
export function CredentialPage({
  credentialId,
}: {
  credentialId: string;
}): React.JSX.Element {
  const { client, resources, user, isAdmin, serverUrl } = useSettings();
  const navigation = useSettingsNavigation();
  const credentials = useResource(resources, CREDENTIALS, () =>
    client.credentials.list(),
  );
  useForegroundReload(credentials.reload);
  const credential = credentials.data?.find(
    (entry) => entry.id === credentialId,
  );
  const [name, setName] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (credential && name === null) setName(credential.name);
  }, [credential, name]);

  if (!credential)
    return (
      <SettingsPage
        title="Credential"
        sections={[]}
        loading={!credentials.data && !credentials.error}
        empty={{
          title: "Credential unavailable",
          description: credentials.error ?? "This credential no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-credential"
      />
    );

  const manageable = canManageCredential(credential, user.id, isAdmin);
  const oauth = credential.credential_type === "mcp_oauth";
  const update = credentialUpdate(credential, name ?? credential.name, value);

  const run = async (
    id: string,
    action: () => Promise<void>,
    success: string,
    failure: string,
    leave = false,
  ) => {
    setBusy(id);
    try {
      await action();
      await credentials.reload();
      showToast({ title: success, tone: "success" });
      if (leave) navigation.pop();
    } catch (cause) {
      showToast({
        title: failure,
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const save = () => {
    if (!update) return;
    void run(
      "save",
      async () => {
        await client.credentials.update(credential.id, update);
        setValue("");
        setName(null);
      },
      "Credential updated",
      "Update failed",
    );
  };

  const confirm = (
    title: string,
    message: string,
    button: string,
    action: () => void,
  ) =>
    Alert.alert(title, message, [
      { text: "Cancel", style: "cancel" },
      { text: button, style: "destructive", onPress: action },
    ]);

  const details: NativeFormRow[] = [
    {
      kind: "value",
      id: "service",
      label: "Service",
      value: credential.service_name,
    },
    {
      kind: "value",
      id: "type",
      label: "Type",
      value: `${credentialTypeLabel(credential.credential_type)} · ${credentialScopeLabel(credential)}`,
    },
    ...(credential.env_var_name
      ? [
          {
            kind: "value" as const,
            id: "env",
            label: "Environment variable",
            value: credential.env_var_name,
            monospace: true,
          },
        ]
      : []),
    ...(credential.target_path
      ? [
          {
            kind: "value" as const,
            id: "path",
            label: "Target path",
            value: credential.target_path,
            monospace: true,
          },
        ]
      : []),
    ...(oauth
      ? [
          {
            kind: "value" as const,
            id: "oauth",
            label: "Connection",
            value: oauthStatus(credential).label,
            tone: oauthStatus(credential).tone,
          },
        ]
      : []),
    {
      kind: "value",
      id: "created",
      label: "Created",
      value: formatDate(credential.created_at),
    },
  ];

  const valueRow: NativeFormRow | null =
    manageable && acceptsValue(credential.credential_type)
      ? credential.credential_type === "file"
        ? {
            kind: "text",
            id: "value",
            label: "New value",
            placeholder: "Leave empty to keep current file contents",
            multiline: true,
            monospace: true,
            lines: [4, 10],
            value,
            onChange: setValue,
          }
        : {
            kind: "text",
            id: "value",
            label: "New value",
            placeholder: "Leave empty to keep current value",
            secure: true,
            value,
            onChange: setValue,
          }
      : null;

  const sections: NativeFormSection[] = [
    { id: "details", rows: details },
    ...(manageable
      ? [
          {
            id: "edit",
            title: "Edit",
            footer: valueRow
              ? "Only fill in a new value to replace the stored one."
              : undefined,
            rows: [
              {
                kind: "text" as const,
                id: "name",
                label: "Name",
                placeholder: "Credential name",
                value: name ?? credential.name,
                onChange: setName,
              },
              ...(valueRow ? [valueRow] : []),
            ],
          },
        ]
      : []),
    {
      id: "actions",
      footer:
        manageable && oauth && canReconnect(credential)
          ? BROWSER_HANDOFF_NOTE
          : undefined,
      rows: [
        ...(credential.has_public_key
          ? [
              {
                kind: "link" as const,
                id: "public-key",
                title: "Public key",
                symbol: "key" as const,
                route: route("credential-public-key", credential.id),
              },
            ]
          : []),
        ...(manageable && canReconnect(credential)
          ? [
              {
                kind: "button" as const,
                id: "reconnect",
                title: "Reconnect in browser",
                symbol: "arrow.up.right.square" as const,
                onPress: () =>
                  void Linking.openURL(
                    oauthHandoffUrl(serverUrl, { credentialId: credential.id }),
                  ),
              },
            ]
          : []),
        ...(manageable && canDisconnect(credential)
          ? [
              {
                kind: "button" as const,
                id: "disconnect",
                title: "Disconnect",
                symbol: "powerplug" as const,
                destructive: true,
                busy: busy === "disconnect",
                disabled: Boolean(busy),
                onPress: () =>
                  confirm(
                    `Disconnect ${credential.name}?`,
                    "This removes the provider connection. Workspace associations are preserved.",
                    "Disconnect",
                    () =>
                      void run(
                        "disconnect",
                        () => client.credentials.disconnectOAuth(credential.id),
                        "OAuth disconnected",
                        "Disconnect failed",
                      ),
                  ),
              },
            ]
          : []),
        ...(manageable
          ? [
              {
                kind: "button" as const,
                id: "delete",
                title: "Delete credential",
                symbol: "trash" as const,
                destructive: true,
                busy: busy === "delete",
                disabled: Boolean(busy),
                onPress: () =>
                  confirm(
                    `Delete ${credential.name}?`,
                    "Deletion may be blocked while a workspace or required plugin depends on this credential.",
                    "Delete",
                    () =>
                      void run(
                        "delete",
                        () => client.credentials.delete(credential.id),
                        "Credential deleted",
                        "Deletion failed",
                        true,
                      ),
                  ),
              },
            ]
          : []),
      ],
    },
  ];

  return (
    <SettingsPage
      title={credential.name}
      sections={sections}
      primaryAction={
        manageable
          ? {
              title: "Save",
              onPress: save,
              busy: busy === "save",
              disabled: !update || Boolean(busy),
              testID: "save-credential",
            }
          : undefined
      }
      testID="settings-credential"
    />
  );
}
