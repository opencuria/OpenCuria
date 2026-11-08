import React from "react";
import { StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import { Host, Shape, Surface } from "@expo/ui/jetpack-compose";
import { fillMaxSize } from "@expo/ui/jetpack-compose/modifiers";
import { useTheme } from "./theme";

/** Material owns the editor container's shape, color and elevation. */
export function ComposerSurface({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View collapsable={false}>
      <Host
        pointerEvents="none"
        style={StyleSheet.absoluteFill}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Surface
          color={theme.surfaceMuted}
          tonalElevation={3}
          shape={Shape.RoundedCorner({
            cornerRadii: {
              topStart: 28,
              topEnd: 28,
              bottomStart: 28,
              bottomEnd: 28,
            },
          })}
          modifiers={[fillMaxSize()]}
        >
          {null}
        </Surface>
      </Host>
      <View style={{ padding: 6 }}>{children}</View>
    </View>
  );
}

const SHEET_RADIUS = 22;

/** Material surface of the composer sheet stack; its square lower edge runs into the composer. */
export function ComposerSheetSurface({
  children,
  style,
}: React.PropsWithChildren<{
  style?: StyleProp<ViewStyle>;
}>): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View collapsable={false} style={style}>
      <Host
        pointerEvents="none"
        style={StyleSheet.absoluteFill}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Surface
          color={theme.raised}
          tonalElevation={1}
          shadowElevation={3}
          border={{ width: StyleSheet.hairlineWidth, color: theme.border }}
          shape={Shape.RoundedCorner({
            cornerRadii: {
              topStart: SHEET_RADIUS,
              topEnd: SHEET_RADIUS,
              bottomStart: 0,
              bottomEnd: 0,
            },
          })}
          modifiers={[fillMaxSize()]}
        >
          {null}
        </Surface>
      </Host>
      {children}
    </View>
  );
}
