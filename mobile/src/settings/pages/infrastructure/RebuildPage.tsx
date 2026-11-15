import React, { useState } from "react";
import { showToast } from "../../../ui/toast";
import { errorMessage } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";

const MESSAGE_LIMIT = 500;

/** Builds a new version of a definition on one runner, with an optional note. */
export function RebuildPage({
  definitionId,
  runnerId,
}: {
  definitionId: string;
  runnerId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const rebuild = async () => {
    setBusy(true);
    try {
      const note = message.trim();
      await client.images.updateRunnerBuild(definitionId, runnerId, {
        action: "rebuild",
        ...(note ? { message: note } : {}),
      });
      resources.invalidate(`runner-builds:${definitionId}`);
      resources.invalidate("image-definitions");
      showToast({ title: "Rebuild started", tone: "success" });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "Rebuild failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <SettingsPage
      title="Rebuild image"
      sections={[
        {
          id: "message",
          footer:
            "Builds a new version on this runner. New workspaces use it once the build succeeds. Existing workspaces keep their version until they are updated.",
          rows: [
            {
              kind: "text",
              id: "rebuild-message",
              label: "What changed?",
              placeholder: "Optional, e.g. Added Playwright",
              multiline: true,
              prose: true,
              lines: [2, 5],
              value: message,
              onChange: (value) => setMessage(value.slice(0, MESSAGE_LIMIT)),
            },
          ],
        },
      ]}
      primaryAction={{
        title: "Rebuild",
        onPress: () => void rebuild(),
        busy,
        disabled: busy,
        testID: "rebuild",
      }}
      testID="settings-rebuild"
    />
  );
}
