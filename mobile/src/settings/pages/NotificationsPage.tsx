import React from "react";
import { Linking, Platform } from "react-native";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../ui/NativeForm.types";
import { useNotifications } from "../../notifications/NotificationContext";
import { SettingsPage } from "../SettingsPage";

/** Push notifications and, on iOS, Live Activities; permission is only asked on opt-in. */
export function NotificationsPage(): React.JSX.Element {
  const { controller, state } = useNotifications();
  const configured =
    state.capabilities?.[Platform.OS === "ios" ? "ios" : "android"];
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
              (state.capabilities?.live_activities || state.liveActivities) &&
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
      footer:
        state.ready && state.capabilities && !configured
          ? "This server has not set up mobile notifications yet."
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
            footer: state.error,
            footerTone: "danger" as const,
            rows: [
              {
                kind: "button" as const,
                id: "retry",
                title: "Retry notifications",
                symbol: "arrow.clockwise" as const,
                onPress: () => void controller?.refresh(),
              },
            ],
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
