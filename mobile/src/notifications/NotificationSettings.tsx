import React from "react";
import { Linking, Platform, Text, View } from "react-native";
import { Button, Banner } from "../ui/primitives";
import { SegmentedControl } from "@expo/ui/community/segmented-control";
import { useTheme } from "../ui/theme";
import { useNotifications } from "./NotificationContext";

/** Native settings request permission only after a deliberate opt-in. */
export function NotificationSettings(): React.JSX.Element {
  const { controller, state } = useNotifications();
  const { theme } = useTheme();
  const configured =
    state.capabilities?.[Platform.OS === "ios" ? "ios" : "android"];
  return (
    <View style={{ gap: 10 }} testID="notification-settings">
      <Text style={{ color: theme.text, fontSize: 15, fontWeight: "500" }}>
        Agent notifications
      </Text>
      <SegmentedControl
        values={["Off", "On"]}
        selectedIndex={state.notifications ? 1 : 0}
        enabled={Boolean(
          controller &&
          state.ready &&
          (configured || state.notifications) &&
          !state.busy,
        )}
        appearance={theme.scheme}
        onChange={({ nativeEvent }) => {
          void controller?.setEnabled(
            "notifications",
            nativeEvent.selectedSegmentIndex === 1,
          );
        }}
      />
      <Text style={{ color: theme.mutedText, fontSize: 13 }}>
        Get notified when your agent finishes, fails or needs your input.
      </Text>
      {Platform.OS === "ios" && controller?.live.available ? (
        <>
          <Text
            style={{
              color: theme.text,
              fontSize: 15,
              fontWeight: "500",
              marginTop: 6,
            }}
          >
            Live Activities
          </Text>
          <SegmentedControl
            values={["Off", "On"]}
            selectedIndex={state.liveActivities ? 1 : 0}
            enabled={Boolean(
              state.ready &&
              (state.capabilities?.live_activities || state.liveActivities) &&
              !state.busy,
            )}
            appearance={theme.scheme}
            onChange={({ nativeEvent }) => {
              void controller.setEnabled(
                "liveActivities",
                nativeEvent.selectedSegmentIndex === 1,
              );
            }}
          />
          <Text style={{ color: theme.mutedText, fontSize: 13 }}>
            Follow running agents on your Lock Screen and Dynamic Island.
          </Text>
        </>
      ) : null}
      {state.ready && state.capabilities && !configured ? (
        <Text style={{ color: theme.mutedText, fontSize: 13 }}>
          Your organization has not set up mobile notifications yet.
        </Text>
      ) : null}
      {state.notifications && !state.permissionGranted ? (
        <Button
          title="Open notification settings"
          variant="outline"
          onPress={() => {
            void Linking.openSettings();
          }}
        />
      ) : null}
      {state.error ? (
        <>
          <Banner tone="error" message={state.error} />
          <Button
            title="Retry notifications"
            variant="ghost"
            onPress={() => {
              void controller?.refresh();
            }}
          />
        </>
      ) : null}
    </View>
  );
}
