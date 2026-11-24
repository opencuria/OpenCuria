import React from "react";
import { Linking, Platform } from "react-native";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../ui/NativeForm.types";
import { useNotifications } from "../../notifications/NotificationContext";
import { availabilityMessage } from "../../notifications/pushProxy";
import { errorRows } from "../../ui/formStates";
import { SettingsPage } from "../SettingsPage";

/** Push notifications and, on iOS, Live Activities; permission is only asked on opt-in. */
export function NotificationsPage(): React.JSX.Element {
  const { controller, state } = useNotifications();
  const configured = state.availability === "available";
  const live = Platform.OS === "ios" && Boolean(controller?.live.available);
  const rows: NativeFormRow[] = [
    {
      kind: "toggle",
      id: "notifications",
      testID: "settings-notifications-toggle",
      label: "Agent notifications",
      subtitle: "When your agent finishes, fails or needs your input.",
      value: state.notifications,
      disabled: !(
        controller &&
        state.ready &&
        (configured || state.notifications) &&
        !state.busy
      ),
      onChange: (value) => void controller?.setEnabled("notifications", value),
    },
    ...(live
      ? [
          {
            kind: "toggle" as const,
            id: "live-activities",
            label: "Live Activities",
            subtitle:
              "Follow running agents on your Lock Screen and Dynamic Island.",
            value: state.liveActivities,
            disabled: !(
              state.ready &&
              (configured || state.liveActivities) &&
              !state.busy
            ),
            onChange: (value: boolean) =>
              void controller?.setEnabled("liveActivities", value),
          },
        ]
      : []),
  ];
  const sections: NativeFormSection[] = [
    {
      id: "notifications",
      footer: state.ready
        ? (availabilityMessage(
            state.availability,
            state.capabilities,
            controller?.proxy.url ?? "",
          ) ?? undefined)
        : undefined,
      rows,
    },
    ...(state.notifications && !state.permissionGranted
      ? [
          {
            id: "permission",
            footer:
              "Notifications are turned off for OpenCuria on this device.",
            rows: [
              {
                kind: "button" as const,
                id: "open-settings",
                title: "Open notification settings",
                symbol: "arrow.up.right.square" as const,
                onPress: () => void Linking.openSettings(),
              },
            ],
          },
        ]
      : []),
    ...(state.error
      ? [
          {
            id: "error",
            rows: errorRows(
              "notifications",
              state.error,
              () => void controller?.refresh(),
              state.busy,
            ),
          },
        ]
      : []),
  ];
  return (
    <SettingsPage
      title="Notifications"
      sections={sections}
      testID="settings-notifications"
    />
  );
}
