import React from "react";
import { StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
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

/**
 * Opaque card of the composer sheet stack. Questions and approvals stay legible
 * above any transcript; its square lower edge runs into the composer.
 */
export function ComposerSheetSurface({
  children,
  style,
}: React.PropsWithChildren<{
  style?: StyleProp<ViewStyle>;
}>): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View
      style={[
        styles.sheet,
        { backgroundColor: theme.raised, borderColor: theme.border },
        style,
      ]}
    >
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { borderRadius: 28, borderCurve: "continuous", padding: 6 },
  opaque: { overflow: "hidden" },
  sheet: {
    borderTopLeftRadius: 22,
    borderTopRightRadius: 22,
    borderCurve: "continuous",
    borderWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: 0,
    shadowColor: "#000000",
    shadowOpacity: 0.1,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: -2 },
  },
});
