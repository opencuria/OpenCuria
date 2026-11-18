import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadFailure, refreshError } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { useForegroundReload } from "../../useForegroundReload";
import { credentialSubtitle, oauthStatus } from "./credentials";
import { CREDENTIALS } from "./resources";

/** Personal and organization credentials; OAuth ones show their connection. */
export function CredentialsPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const credentials = useResource(resources, CREDENTIALS, () =>
    client.credentials.list(),
  );
  // OAuth connections finish in the browser.
  useForegroundReload(credentials.reload);
  const sections: NativeFormSection[] = credentials.data?.length
    ? [
        {
          id: "credentials",
          footer:
            "Credentials are encrypted and injected into the workspaces you attach them to.",
          rows: credentials.data.map((credential) => {
            const oauth =
              credential.credential_type === "mcp_oauth"
                ? oauthStatus(credential)
                : null;
            return {
              kind: "item" as const,
              id: `credential-${credential.id}`,
              testID: `credential-${credential.id}`,
              title: credential.name,
              subtitle: [credentialSubtitle(credential), oauth?.label]
                .filter(Boolean)
                .join(" · "),
              subtitleTone:
                oauth && oauth.tone !== "success" ? oauth.tone : "muted",
              onPress: () =>
                navigation.push(route("credential", credential.id)),
            };
          }),
        },
      ]
    : [];
  const failure = credentials.data
    ? undefined
    : loadFailure("credentials", credentials);
  const add = {
    title: "Add credential",
    symbol: "plus" as const,
    onPress: () => navigation.push("credential-new"),
  };
  return (
    <SettingsPage
      title="Credentials"
      sections={
        failure
          ? []
          : [...refreshError("credentials", credentials), ...sections]
      }
      loading={!credentials.data && !credentials.error}
      onRefresh={credentials.reload}
      empty={
        failure ?? {
          title: "No credentials yet",
          description: "Add a credential for an active service.",
          symbol: "key",
          action: { ...add, testID: "empty-add-credential" },
        }
      }
      primaryAction={{ ...add, testID: "add-credential" }}
      testID="settings-credentials"
    />
  );
}
