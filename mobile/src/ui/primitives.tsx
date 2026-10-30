import React from "react";
import {
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { X } from "lucide-react-native";
import { useTheme } from "./theme";
import { IconButton, NativeAction } from "./controls";
export { Button, IconButton, NativeAction } from "./controls";
export type { ButtonProps, IconButtonProps } from "./controls.types";
export type { NativeModalProps } from "./NativeModal";
export { NativeModal } from "./NativeModal";

/** `neutral` carries no status color, e.g. for notices inside the transcript. */
export type BannerTone = "info" | "success" | "warning" | "error" | "neutral";
export interface BannerProps {
  tone?: BannerTone;
  title?: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
  onDismiss?: () => void;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}

/** Inline status feedback for errors or notices that belong beside their content. */
export function Banner({
  tone = "info",
  title,
  message,
  actionLabel,
  onAction,
  onDismiss,
  style,
  testID,
}: BannerProps): React.JSX.Element {
  const { theme } = useTheme();
  const color =
    tone === "error"
      ? theme.danger
      : tone === "warning"
        ? theme.warning
        : tone === "success"
          ? theme.success
          : tone === "neutral"
            ? theme.text
            : theme.info;
  return (
    <View
      accessibilityRole="alert"
      testID={testID}
      style={[
        styles.banner,
        {
          backgroundColor:
            tone === "error" ? theme.dangerSurface : theme.surfaceMuted,
          borderColor: theme.border,
        },
        style,
      ]}
    >
      <View style={{ flex: 1, gap: 3 }}>
        {title ? (
          <Text style={[styles.bannerTitle, { color }]}>{title}</Text>
        ) : null}
        <Text style={[styles.bannerMessage, { color: theme.text }]}>
          {message}
        </Text>
        {actionLabel && onAction ? (
          <NativeAction
            accessibilityRole="button"
            accessibilityLabel={actionLabel}
            onPress={onAction}
            style={styles.bannerAction}
          >
            <Text style={[styles.bannerActionText, { color }]}>
              {actionLabel}
            </Text>
          </NativeAction>
        ) : null}
      </View>
      {onDismiss ? (
        <IconButton
          symbol="xmark"
          icon={<X size={17} color={theme.mutedText} />}
          label="Dismiss message"
          tone="quiet"
          onPress={onDismiss}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    borderRadius: 12,
    padding: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  bannerTitle: { fontSize: 13, fontWeight: "600", lineHeight: 18 },
  bannerMessage: { fontSize: 14, lineHeight: 20 },
  bannerAction: {
    alignSelf: "flex-start",
    minHeight: 44,
    justifyContent: "center",
  },
  bannerActionText: { fontSize: 14, fontWeight: "600" },
});
