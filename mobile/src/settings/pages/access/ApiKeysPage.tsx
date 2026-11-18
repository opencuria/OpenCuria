import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadFailure, refreshError } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { keyStatus, keySubtitle } from "./apiKeys";
import { API_KEYS, API_KEY_PERMISSIONS } from "./resources";

/** The signed-in user's API keys for external integrations. */
export function ApiKeysPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const keys = useResource(resources, API_KEYS, () => client.apiKeys.list());
  const permissions = useResource(resources, API_KEY_PERMISSIONS, () =>
    client.apiKeys.permissions(),
  );
  const sections: NativeFormSection[] = keys.data?.length
    ? [
        {
          id: "keys",
          footer:
            "Long-lived keys for external integrations. Use Authorization: Bearer kai_… or X-API-Key: kai_….",
          rows: keys.data.map((key) => {
            const status = keyStatus(key);
            return {
              kind: "item" as const,
              id: `key-${key.id}`,
              testID: `key-${key.id}`,
              title: key.name,
              subtitle:
                status === "Active"
                  ? keySubtitle(key, permissions.data)
                  : `${status} · ${key.key_prefix}…`,
              subtitleTone: status === "Active" ? "muted" : "danger",
              onPress: () => navigation.push(route("api-key", key.id)),
            };
          }),
        },
      ]
    : [];
  const failure = keys.data ? undefined : loadFailure("API keys", keys);
  const create = {
    title: "New API key",
    symbol: "plus" as const,
    onPress: () => navigation.push("api-key-new"),
  };
  return (
    <SettingsPage
      title="API keys"
      sections={failure ? [] : [...refreshError("api-keys", keys), ...sections]}
      loading={!keys.data && !keys.error}
      onRefresh={keys.reload}
      empty={
        failure ?? {
          title: "No API keys yet",
          description:
            "Create your first API key to start integrating with external tools.",
          symbol: "key.horizontal",
          action: { ...create, testID: "empty-new-api-key" },
        }
      }
      primaryAction={{ ...create, testID: "new-api-key" }}
      testID="settings-api-keys"
    />
  );
}
