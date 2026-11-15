import React from "react";
import {
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { KeyboardStickyView } from "react-native-keyboard-controller";
import { ChevronDown } from "lucide-react-native";
import { IconButton } from "../ui/controls";
import { useTheme } from "../ui/theme";

export const KEY_ACCESSORY_HEIGHT = 52;

export interface AccessoryKey {
  id: string;
  label: string;
  /** A latched modifier. */
  selected?: boolean;
  /** Word labels (ctrl, tab) render in capitals; symbols stay as they are. */
  uppercase?: boolean;
}

export interface KeyAccessoryBarProps {
  keys: AccessoryKey[];
  onPress: (id: string) => void;
  onDismiss: () => void;
  background: string;
  foreground: string;
  /** Prefix of the bar's test ids, e.g. `terminal` → `terminal-key-esc`. */
  testIDPrefix: string;
}

/**
 * The row of keys a phone keyboard lacks, docked on top of the keyboard,
 * with a trailing dismiss button. Shared by the terminal and the desktop.
 */
export function KeyAccessoryBar({
  keys,
  onPress,
  onDismiss,
  background,
  foreground,
  testIDPrefix,
}: KeyAccessoryBarProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <KeyboardStickyView style={styles.dock} offset={{ closed: 0, opened: 0 }}>
      <View
        style={[
          styles.bar,
          { backgroundColor: background, borderTopColor: theme.border },
        ]}
        testID={`${testIDPrefix}-accessory`}
      >
        <ScrollView
          horizontal
          keyboardShouldPersistTaps="always"
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.keys}
        >
          {keys.map((key) => (
            <Pressable
              key={key.id}
              accessibilityRole="button"
              accessibilityLabel={key.label}
              accessibilityState={{ selected: Boolean(key.selected) }}
              onPress={() => onPress(key.id)}
              testID={`${testIDPrefix}-key-${key.id}`}
              android_ripple={{ color: theme.controlFill, borderless: false }}
              style={({ pressed }) => [
                styles.key,
                {
                  minWidth: key.label.length > 1 ? 52 : 42,
                  backgroundColor: key.selected
                    ? theme.text
                    : pressed
                      ? theme.border
                      : theme.controlFill,
                },
              ]}
            >
              <Text
                style={[
                  styles.label,
                  {
                    color: key.selected ? theme.background : foreground,
                    textTransform: key.uppercase ? "uppercase" : "none",
                  },
                ]}
              >
                {key.label}
              </Text>
            </Pressable>
          ))}
        </ScrollView>
        <IconButton
          label="Dismiss keyboard"
          symbol="keyboard.chevron.compact.down"
          icon={<ChevronDown size={20} color={theme.text} />}
          tone="quiet"
          onPress={onDismiss}
          testID={`${testIDPrefix}-dismiss-keyboard`}
        />
      </View>
    </KeyboardStickyView>
  );
}

const styles = StyleSheet.create({
  dock: { position: "absolute", left: 0, right: 0, bottom: 0 },
  bar: {
    minHeight: KEY_ACCESSORY_HEIGHT,
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  keys: { gap: 6, alignItems: "center", paddingRight: 4 },
  key: {
    height: 36,
    borderRadius: 10,
    paddingHorizontal: 10,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  label: {
    fontSize: 14,
    fontWeight: "600",
    fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
  },
});
