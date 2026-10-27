import React from "react";
import { StyleSheet, View } from "react-native";
import { GlassView, isGlassEffectAPIAvailable } from "expo-glass-effect";
import { useTheme } from "./theme";

const GLASS = isGlassEffectAPIAvailable();

/**
 * Interactive liquid glass, as on system bars: it reacts to touches and
 * refracts the transcript scrolling beneath. It is not clipped, so the glass
 * can flex beyond its bounds; earlier iOS versions use an opaque surface.
 */
export function ComposerSurface({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { theme } = useTheme();
  if (GLASS)
    return (
      <GlassView
        isInteractive
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
        styles.opaque,
        { backgroundColor: theme.surfaceMuted },
      ]}
    >
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { borderRadius: 28, borderCurve: "continuous", padding: 6 },
  opaque: { overflow: "hidden" },
});
