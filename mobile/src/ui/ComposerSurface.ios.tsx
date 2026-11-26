import React from "react";
import { StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import { GlassView, isGlassEffectAPIAvailable } from "expo-glass-effect";
import { AdaptiveInkScope } from "./ink";
import { useTheme } from "./theme";

const GLASS = isGlassEffectAPIAvailable();

/**
 * Glass cannot sample glass beneath it, so with liquid glass the sheet stack
 * floats above the composer as its own card. Opaque sheets run under the
 * composer's top edge instead.
 */
export const composerSheetsFloat = GLASS;

/**
 * Interactive liquid glass, as on system bars: it reacts to touches and
 * refracts the transcript scrolling beneath. Its content draws in adaptive
 * ink, as the glass turns light or dark with what lies beneath. It is not
 * clipped, so the glass can flex beyond its bounds; earlier iOS versions use
 * an opaque surface.
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
        <AdaptiveInkScope>{children}</AdaptiveInkScope>
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
 * Card of the composer sheet stack: interactive liquid glass like the
 * composer, rounded all around. Without glass it is an opaque card whose
 * square lower edge runs into the composer.
 */
export function ComposerSheetSurface({
  children,
  style,
}: React.PropsWithChildren<{
  style?: StyleProp<ViewStyle>;
}>): React.JSX.Element {
  const { theme } = useTheme();
  if (GLASS)
    return (
      <GlassView
        isInteractive
        glassEffectStyle="regular"
        colorScheme={theme.scheme}
        style={[styles.card, style]}
      >
        {children}
      </GlassView>
    );
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

/** Top edge of a sheet waiting beneath the top one; the stack positions it. */
export function ComposerSheetPeek({
  style,
}: {
  style?: StyleProp<ViewStyle>;
}): React.JSX.Element {
  const { theme } = useTheme();
  if (GLASS)
    return (
      <GlassView
        glassEffectStyle="regular"
        colorScheme={theme.scheme}
        style={[styles.peek, style]}
      />
    );
  return (
    <View
      style={[
        styles.peek,
        styles.peekOpaque,
        { backgroundColor: theme.surfaceMuted, borderColor: theme.border },
        style,
      ]}
    />
  );
}

const styles = StyleSheet.create({
  surface: { borderRadius: 28, borderCurve: "continuous", padding: 6 },
  opaque: { overflow: "hidden" },
  // The composer's corners, so the floating card and the composer match.
  card: { borderRadius: 28, borderCurve: "continuous" },
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
  peek: {
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    borderCurve: "continuous",
  },
  peekOpaque: {
    borderWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: 0,
  },
});
