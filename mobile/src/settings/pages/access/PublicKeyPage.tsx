import React from "react";
import { useResource } from "../../resources";
import { useSettings } from "../../SettingsContext";
import { TextPage } from "../TextPage";

/** The public half of an SSH key credential, to add to a Git host or server. */
export function PublicKeyPage({
  credentialId,
}: {
  credentialId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const key = useResource(
    resources,
    `credential-public-key:${credentialId}`,
    () => client.credentials.publicKey(credentialId),
  );
  return (
    <TextPage
      title="Public key"
      label="Public key"
      text={key.data?.public_key}
      loading={key.loading}
      error={key.error}
      onRetry={() => void key.reload()}
      emptyText="No public key available."
      copyTitle="Copy public key"
      footer="Add this key to your Git provider (e.g. GitHub → Settings → SSH Keys) to enable SSH-based repository access in workspaces."
      testID="settings-public-key"
    />
  );
}
