import React, { useState } from "react";
import { Alert } from "react-native";
import type { OperationDisposition } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { DISPOSITION_TITLES } from "./runners";

/** The verified state of a runner operation and the resolutions it allows. */
export function RunnerOperationPage({
  operationId,
}: {
  operationId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const inspection = useResource(
    resources,
    `runner-operation:${operationId}`,
    () => client.runners.inspectOperation(operationId),
  );
  const [busy, setBusy] = useState<OperationDisposition | null>(null);

  const dispose = async (action: OperationDisposition) => {
    setBusy(action);
    try {
      await client.runners.disposeOperation(operationId, action);
      resources.invalidate("runner-storage:");
      showToast({ title: DISPOSITION_TITLES[action], tone: "success" });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "The operation could not be resolved",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };
  const choose = (action: OperationDisposition) => {
    if (action !== "acknowledge_interrupted") return void dispose(action);
    Alert.alert(
      "Close interrupted operation?",
      "This releases the block on further changes and keeps existing resources. The workspace will not be restarted or repaired.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Close operation",
          style: "destructive",
          onPress: () => void dispose(action),
        },
      ],
    );
  };

  const data = inspection.data;
  const sections: NativeFormSection[] = data
    ? [
        {
          id: "status",
          footer:
            data.review_message ||
            "Status could not be verified. Check the runner connection and see Details.",
          rows: [
            {
              kind: "value",
              id: "state",
              label: "Status",
              value: data.status.replaceAll("_", " "),
            },
            {
              kind: "value",
              id: "phase",
              label: "Phase",
              value: data.phase.replaceAll("_", " "),
            },
            ...data.permitted_actions.map((action) => ({
              kind: "button" as const,
              id: action,
              testID: `dispose-${action}`,
              title: DISPOSITION_TITLES[action],
              destructive: action === "acknowledge_interrupted",
              busy: busy === action,
              disabled: Boolean(busy),
              onPress: () => choose(action),
            })),
          ],
        },
        {
          id: "details",
          title: "Details",
          rows: [
            ...(data.diagnostic
              ? [
                  {
                    kind: "value" as const,
                    id: "diagnostic",
                    label: "Diagnostic",
                    value: data.diagnostic,
                    stacked: true,
                  },
                ]
              : []),
            {
              kind: "value",
              id: "evidence",
              label: "Evidence",
              value: JSON.stringify(data.evidence ?? {}, null, 2),
              monospace: true,
            },
          ],
        },
      ]
    : [];

  return (
    <SettingsPage
      title="Operation"
      sections={
        inspection.error && !data
          ? loadErrorSections(inspection.error, () => void inspection.reload())
          : sections
      }
      loading={!data && !inspection.error}
      onRefresh={inspection.reload}
      testID="settings-runner-operation"
    />
  );
}
