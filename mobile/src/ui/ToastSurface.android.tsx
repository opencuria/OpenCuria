import React from "react";
import { StyleSheet, View } from "react-native";
import { useTheme } from "./theme";

/** A raised Material surface, like a heads-up notification. */
export function ToastSurface({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={[styles.surface, { backgroundColor: theme.raised }]}>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { borderRadius: 22, elevation: 4 },
});
