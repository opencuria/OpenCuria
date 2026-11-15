import React from "react";
import { useResource } from "../../resources";
import { useSettings } from "../../SettingsContext";
import { TextPage } from "../TextPage";

/** The latest output of a definition's build on one runner. */
export function BuildLogPage({
  definitionId,
  runnerId,
}: {
  definitionId: string;
  runnerId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const log = useResource(
    resources,
    `build-log:${definitionId}:${runnerId}`,
    () => client.images.runnerBuildLog(definitionId, runnerId),
  );
  return (
    <TextPage
      title="Build log"
      label="Latest output"
      text={log.data?.build_log}
      loading={log.loading}
      error={log.error}
      onRetry={() => void log.reload()}
      emptyText="No build log available."
      testID="settings-build-log"
    />
  );
}
