import React from "react";
import { useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { TextPage } from "../TextPage";
import { accessSummary } from "./apiKeys";
import { apiKeySecret } from "./NewApiKeyPage";
import { API_KEYS, API_KEY_PERMISSIONS } from "./resources";

/** A new key's secret, shown once; OpenCuria only stores a hash. */
export function ApiKeyCreatedPage({
  keyId,
}: {
  keyId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const secret = useResource<string>(
    resources,
    apiKeySecret(keyId),
    async () => "",
    {
      enabled: false,
    },
  );
  const keys = useResource(resources, API_KEYS, () => client.apiKeys.list());
  const permissions = useResource(resources, API_KEY_PERMISSIONS, () =>
    client.apiKeys.permissions(),
  );
  const key = keys.data?.find((entry) => entry.id === keyId);
  return (
    <TextPage
      title={key?.name ?? "API key"}
      label="API key"
      text={secret.data ?? ""}
      emptyText="The key is no longer shown. Create a new key if you did not save it."
      notice="Copy your key now. It will not be shown again; OpenCuria only stores a hash."
      footer={
        key
          ? `${accessSummary(key.permissions, permissions.data)}. Use Authorization: Bearer ${key.key_prefix}… or X-API-Key: ${key.key_prefix}….`
          : undefined
      }
      copyTitle="Copy key"
      primaryAction={{
        title: "Done",
        onPress: () => {
          // Forget the secret once the user leaves this page.
          resources.set(apiKeySecret(keyId), "");
          navigation.pop();
        },
        testID: "api-key-done",
      }}
      testID="settings-api-key-created"
    />
  );
}
