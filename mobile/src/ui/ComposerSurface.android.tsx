import React from "react";
import { StyleSheet, View } from "react-native";
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
