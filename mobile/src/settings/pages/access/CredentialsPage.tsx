import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
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
  return (
    <SettingsPage
      title="Credentials"
      sections={
        credentials.error && !credentials.data
          ? loadErrorSections(
              credentials.error,
              () => void credentials.reload(),
            )
          : sections
      }
      loading={!credentials.data && !credentials.error}
      onRefresh={credentials.reload}
      empty={{
        title: "No credentials",
        description: "Add a credential for an active service.",
        symbol: "key",
      }}
      primaryAction={{
        title: "Add credential",
        symbol: "plus",
        onPress: () => navigation.push("credential-new"),
        testID: "add-credential",
      }}
      testID="settings-credentials"
    />
  );
}
