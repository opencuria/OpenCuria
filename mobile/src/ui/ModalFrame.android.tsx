import React from "react";
import { View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { ModalFrameProps } from "./ModalFrame.types";
import { NativeToolbar } from "./NativeToolbar.android";
import { SearchField } from "./SearchField.android";
import { useTheme } from "./theme";

/**
 * Sheets share the chat's canvas: the toolbar and content sit on
 * `theme.background`, close leading, overflow trailing. `form`,
 * `primaryAction` and `largeTitle` are iOS toolbar concerns: on Android
 * `NativeModal` renders the form and the bottom action as `children`.
 */
export function ModalFrame({
  title,
  subtitle,
  onClose,
  actions,
  onAction,
  search,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <View
      style={{
        flex: 1,
        paddingTop: insets.top,
        backgroundColor: theme.background,
      }}
    >
      {/* The native toolbar parses colors with Color.parseColor: hex only. */}
      <NativeToolbar
        title={title}
        subtitle={subtitle ?? ""}
        titleColor={theme.text}
        subtitleColor={theme.mutedText}
        surfaceColor={theme.background}
        tintColor={theme.text}
        showClose={Boolean(onClose)}
        menuItems={(actions ?? []).map((action) => ({
          id: action.id,
          title: action.title,
          enabled: !action.disabled,
          checked: Boolean(action.selected),
        }))}
        onClose={() => onClose?.()}
        onMenuAction={({ nativeEvent }) => onAction?.(nativeEvent.id)}
        style={{ height: 56, width: "100%" }}
      />
      {search ? <SearchField search={search} /> : null}
      {children}
    </View>
  );
}
