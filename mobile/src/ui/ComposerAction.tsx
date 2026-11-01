import React from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import type { LucideIcon } from "lucide-react-native";
import { NativeAction } from "./controls";
import { useTheme } from "./theme";

interface ComposerActionContentProps {
  icon: LucideIcon;
  title?: string;
  primary?: boolean;
  disabled?: boolean;
  busy?: boolean;
  iconSize?: number;
  strokeWidth?: number;
  filledIcon?: boolean;
}

/** The composer's 36-point circle (or labelled capsule) in a 44-point target. */
export function ComposerActionContent({
  icon: Icon,
  title,
  primary = false,
  disabled = false,
  busy = false,
  iconSize = primary ? 22 : 21,
  strokeWidth = primary ? 2.25 : 2,
  filledIcon = false,
}: ComposerActionContentProps): React.JSX.Element {
  const { theme } = useTheme();
  const foreground = primary
    ? disabled
      ? theme.subtleText
      : theme.background
    : theme.mutedText;
  return (
    <View style={styles.target}>
      <View
        style={[
          styles.control,
          title ? styles.labelled : styles.circle,
          {
            backgroundColor:
              primary && !disabled ? theme.text : theme.controlFill,
            opacity: !primary && disabled ? 0.38 : 1,
          },
        ]}
      >
        {busy ? (
          <ActivityIndicator size="small" color={foreground} />
        ) : (
          <Icon
            size={iconSize}
            strokeWidth={strokeWidth}
            color={foreground}
            fill={filledIcon ? foreground : "none"}
          />
        )}
        {title ? (
          <Text numberOfLines={1} style={[styles.title, { color: foreground }]}>
            {title}
          </Text>
        ) : null}
      </View>
    </View>
  );
}

/** Shared native action for the chat and Git composers. */
export function ComposerAction({
  label,
  onPress,
  onLongPress,
  accessibilityHint,
  testID,
  ...content
}: ComposerActionContentProps & {
  label: string;
  onPress: () => void;
  onLongPress?: () => void;
  accessibilityHint?: string;
  testID?: string;
}): React.JSX.Element {
  const inactive = Boolean(content.disabled || content.busy);
  return (
    <NativeAction
      fitContent
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={accessibilityHint}
      accessibilityState={{ busy: Boolean(content.busy) }}
      // An unavailable primary action must still allow opening its menu.
      disabled={inactive && !onLongPress}
      onPress={() => {
        if (!inactive) onPress();
      }}
      onLongPress={onLongPress}
      style={styles.action}
      testID={testID}
    >
      <ComposerActionContent {...content} disabled={inactive} />
    </NativeAction>
  );
}

const styles = StyleSheet.create({
  action: { minWidth: 44, height: 44 },
  target: {
    minWidth: 44,
    height: 44,
    padding: 4,
    alignItems: "center",
    justifyContent: "center",
  },
  control: {
    height: 36,
    borderRadius: 18,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
  },
  circle: { width: 36 },
  labelled: { paddingHorizontal: 12 },
  title: { fontSize: 15, fontWeight: "600" },
});
