import React from "react";
import { requireNativeView } from "expo";
import { View, type ViewProps } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { ModalFrameProps } from "./ModalFrame.types";
import { useTheme } from "./theme";

interface NativeToolbarMenuItem {
  id: string;
  title: string;
  enabled: boolean;
  checked: boolean;
}

const NativeToolbar = requireNativeView<
  ViewProps & {
    title: string;
    subtitle: string;
    titleColor: string;
    subtitleColor: string;
    surfaceColor: string;
    tintColor: string;
    showClose: boolean;
    menuItems: NativeToolbarMenuItem[];
    onClose: () => void;
    onMenuAction: (event: { nativeEvent: { id: string } }) => void;
  }
>("OpenCuriaNativeUI");

export function ModalFrame({
  title,
  subtitle,
  onClose,
  actions,
  onAction,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <View
      style={{
        flex: 1,
        paddingTop: insets.top,
        backgroundColor: theme.surface,
      }}
    >
      <NativeToolbar
        title={title}
        subtitle={subtitle ?? ""}
        titleColor={theme.text}
        subtitleColor={theme.mutedText}
        surfaceColor={theme.surface}
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
      {children}
    </View>
  );
}
