import React, { useState } from "react";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { TextPage } from "../TextPage";
import { RUNNERS } from "./ImageDefinitionsPage";

const runnerToken = (id: string) => `runner-token:${id}`;

/** Registers a runner; its API token is shown once on the next page. */
export function NewRunnerPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const runners = useResource(resources, RUNNERS, () => client.runners.list());
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const register = async () => {
    setBusy(true);
    try {
      const created = await client.runners.create({ name: name.trim() });
      resources.set(runnerToken(created.id), created.api_token);
      void runners.reload();
      showToast({
        title: "Runner registered",
        message: `Runner "${created.name || created.id}" created successfully.`,
        tone: "success",
      });
      navigation.replace(route("runner-token", created.id));
    } catch (cause) {
      showToast({
        title: "Registration failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <SettingsPage
      title="Register runner"
      sections={[
        {
          id: "runner",
          footer:
            "You receive an API token for the runner. It is shown only once.",
          rows: [
            {
              kind: "text",
              id: "runner-name",
              label: "Name (optional)",
              placeholder: "e.g. dev-runner-01",
              value: name,
              autoFocus: true,
              onChange: setName,
              onSubmit: () => void register(),
            },
          ],
        },
      ]}
      primaryAction={{
        title: "Register",
        onPress: () => void register(),
        busy,
        disabled: busy,
        testID: "create-runner",
      }}
      testID="settings-new-runner"
    />
  );
}

/** A new runner's API token, shown once. */
export function RunnerTokenPage({
  runnerId,
}: {
  runnerId: string;
}): React.JSX.Element {
  const { resources } = useSettings();
  const navigation = useSettingsNavigation();
  const token = useResource<string>(
    resources,
    runnerToken(runnerId),
    async () => "",
    {
      enabled: false,
    },
  );
  return (
    <TextPage
      title="Runner token"
      label="API token"
      text={token.data ?? ""}
      emptyText="The token is no longer shown. Register the runner again if you did not save it."
      notice="Save this API token now. It will not be shown again."
      copyTitle="Copy token"
      primaryAction={{
        title: "Done",
        onPress: () => {
          resources.set(runnerToken(runnerId), "");
          navigation.pop();
        },
        testID: "runner-token-done",
      }}
      testID="settings-runner-token"
    />
  );
}
