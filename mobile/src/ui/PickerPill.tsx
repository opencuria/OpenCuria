import React from "react";
import {
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { ChevronDown } from "lucide-react-native";
import { NativeAction } from "./controls";
import { NativeMenu, type NativeMenuAction } from "./NativeMenu";
import { useTheme } from "./theme";

export interface PickerPillProps {
  /** The chosen value, e.g. the model or the repository. */
  value: string;
  /** Quieter detail after the value, e.g. the effort or the branch. */
  detail?: string;
  /** Quieter text before the value, e.g. "Plan · ". */
  prefix?: string;
  /** Bounds the label so neighbouring controls keep their room. */
  maxWidth?: number;
  accessibilityLabel: string;
  accessibilityHint?: string;
  /** Opens a sheet or screen. */
  onPress?: () => void;
  /** Opens a native menu anchored to the pill instead of `onPress`. */
  menu?: {
    title: string;
    actions: NativeMenuAction[];
    onSelect: (id: string) => void;
  };
  style?: StyleProp<ViewStyle>;
  testID?: string;
}

/**
 * The composer's model picker: value, quieter detail and a chevron, without
 * a container of its own. Used wherever a compact choice opens a picker.
 */
export function PickerPill({
  value,
  detail,
  prefix,
  maxWidth,
  accessibilityLabel,
  accessibilityHint,
  onPress,
  menu,
  style,
  testID,
}: PickerPillProps): React.JSX.Element {
  const { theme } = useTheme();
  const label = (
    <>
      <Text
        numberOfLines={1}
        style={[styles.text, maxWidth != null && { maxWidth }]}
      >
        {prefix ? (
          <Text style={{ color: theme.mutedText }}>{prefix}</Text>
        ) : null}
        <Text style={{ color: theme.text }}>{value}</Text>
        {detail ? (
          <Text style={{ color: theme.mutedText }}> {detail}</Text>
        ) : null}
      </Text>
      <ChevronDown size={14} color={theme.mutedText} />
    </>
  );
  if (menu)
    return (
      <NativeMenu
        label={menu.title}
        accessibilityLabel={accessibilityLabel}
        actions={menu.actions}
        onSelect={menu.onSelect}
        style={[styles.menu, style]}
        testID={testID}
      >
        <View style={styles.control}>{label}</View>
      </NativeMenu>
    );
  return (
    <NativeAction
      fitContent
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityHint={accessibilityHint}
      onPress={onPress}
      testID={testID}
      style={[styles.control, style]}
    >
      {label}
    </NativeAction>
  );
}

const styles = StyleSheet.create({
  control: {
    maxWidth: "100%",
    minHeight: 44,
    paddingHorizontal: 4,
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  menu: { flexShrink: 1, minWidth: 0 },
  text: { flexShrink: 1, fontSize: 15, fontWeight: "500" },
});
