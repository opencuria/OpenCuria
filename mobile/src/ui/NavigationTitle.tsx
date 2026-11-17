import React from "react";
import { Platform, StyleSheet, Text, View } from "react-native";
import { useTheme } from "./theme";

/**
 * Two-line navigation bar title: the page and a quieter subtitle. Centered as
 * a UIKit title view on iOS, start-aligned beside the navigation icon as a
 * Material top app bar title on Android.
 */
export function NavigationTitle({
  title,
  subtitle,
  maxWidth,
  testID,
}: {
  title: string;
  subtitle?: string;
  /** Keeps a centered title clear of the bar items on both sides. */
  maxWidth?: number;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View
      accessible
      accessibilityRole="header"
      accessibilityLabel={subtitle ? `${title}, ${subtitle}` : title}
      testID={testID}
      style={[styles.root, maxWidth ? { maxWidth } : null]}
    >
      <Text numberOfLines={1} style={[styles.title, { color: theme.text }]}>
        {title}
      </Text>
      {subtitle ? (
        <Text
          numberOfLines={1}
          style={[styles.subtitle, { color: theme.mutedText }]}
        >
          {subtitle}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: Platform.select({
    ios: { alignItems: "center" },
    default: { flexShrink: 1, justifyContent: "center", paddingStart: 4 },
  }),
  title: Platform.select({
    ios: { fontSize: 16, lineHeight: 20, fontWeight: "600" },
    default: { fontSize: 16, lineHeight: 20, fontWeight: "500" },
  }),
  subtitle: Platform.select({
    ios: { fontSize: 12, lineHeight: 16 },
    default: { fontSize: 12, lineHeight: 16 },
  }),
});
