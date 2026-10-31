import React from "react";
import { StyleSheet, View } from "react-native";
import { GlassView, isGlassEffectAPIAvailable } from "expo-glass-effect";
import { useTheme } from "./theme";

const GLASS = isGlassEffectAPIAvailable();

/**
 * Liquid glass, like a system notification on iOS 26; earlier versions use a
 * raised surface with a soft shadow.
 */
export function ToastSurface({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { theme } = useTheme();
  if (GLASS)
    return (
      <GlassView
        glassEffectStyle="regular"
        colorScheme={theme.scheme}
        style={styles.surface}
      >
        {children}
      </GlassView>
    );
  return (
    <View
      style={[
        styles.surface,
        styles.raised,
        { backgroundColor: theme.raised, borderColor: theme.border },
      ]}
    >
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { borderRadius: 22, borderCurve: "continuous" },
  raised: {
    borderWidth: StyleSheet.hairlineWidth,
    shadowColor: "#000000",
    shadowOpacity: 0.12,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 6 },
  },
});
