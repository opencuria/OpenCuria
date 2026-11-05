import React, { useState } from "react";
import { showToast } from "../../ui/toast";
import { errorMessage } from "../resources";
import { useSettings, useSettingsNavigation } from "../SettingsContext";
import { SettingsPage } from "../SettingsPage";

/** Creates an organization and switches to it. */
export function NewOrganizationPage(): React.JSX.Element {
  const settings = useSettings();
  const navigation = useSettingsNavigation();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await settings.createOrganization(name.trim());
      navigation.close();
    } catch (cause) {
      showToast({
        title: "Could not create organization",
        message: errorMessage(cause, "Please try a different name."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <SettingsPage
      title="New organization"
      primaryAction={{
        title: "Create",
        onPress: () => void create(),
        disabled: !name.trim(),
        busy,
        testID: "create-organization",
      }}
      sections={[
        {
          id: "organization",
          footer:
            "Organizations group runners, workspaces and settings. You become its admin and switch to it.",
          rows: [
            {
              kind: "text",
              id: "organization-name",
              label: "Organization name",
              placeholder: "My Company",
              value: name,
              autoFocus: true,
              onChange: setName,
              onSubmit: () => void create(),
            },
          ],
        },
      ]}
      testID="settings-new-organization"
    />
  );
}
