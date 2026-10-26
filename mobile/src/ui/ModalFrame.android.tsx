import React from "react";
import { requireNativeView } from "expo";
import { View, type ViewProps } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { ModalFrameProps } from "./ModalFrame.types";
import { useTheme } from "./theme";

const NativeToolbar = requireNativeView<
  ViewProps & {
    title: string;
    titleColor: string;
    surfaceColor: string;
    tintColor: string;
    showClose: boolean;
    onClose: () => void;
  }
>("OpenCuriaNativeUI");

export function ModalFrame({
  title,
  onClose,
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
        titleColor={theme.text}
        surfaceColor={theme.surface}
        tintColor={theme.primary}
        showClose={Boolean(onClose)}
        onClose={() => onClose?.()}
        style={{ height: 56, width: "100%" }}
      />
      {children}
    </View>
  );
}
