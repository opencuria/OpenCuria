import React from "react";
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type ModalProps,
  type PressableProps,
  type StyleProp,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import { X } from "lucide-react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTheme } from "./theme";

export interface IconButtonProps extends Omit<PressableProps, "children"> {
  icon: React.ReactNode;
  label: string;
  size?: number;
  tone?: "neutral" | "primary" | "danger";
  testID?: string;
}

/** A high-contrast accessible icon tap target, minimum 44 points. */
export function IconButton({
  icon,
  label,
  size = 44,
  tone = "neutral",
  style,
  testID,
  ...props
}: IconButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  const color =
    tone === "primary"
      ? theme.primaryForeground
      : tone === "danger"
        ? theme.danger
        : theme.mutedText;
  const backgroundColor = tone === "primary" ? theme.primary : "transparent";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      testID={testID}
      hitSlop={4}
      style={(state) => [
        styles.iconButton,
        {
          width: Math.max(44, size),
          height: Math.max(44, size),
          backgroundColor,
          opacity: state.pressed ? 0.72 : 1,
        },
        typeof style === "function" ? style(state) : style,
      ]}
      {...props}
    >
      {icon}
    </Pressable>
  );
}

export interface ButtonProps extends Omit<PressableProps, "children"> {
  title: string;
  icon?: React.ReactNode;
  variant?: "primary" | "secondary" | "outline" | "danger" | "ghost";
  busy?: boolean;
  compact?: boolean;
  textStyle?: StyleProp<TextStyle>;
  testID?: string;
}

/** Consistent native action button with scalable labels and a 44pt default height. */
export function Button({
  title,
  icon,
  variant = "secondary",
  busy = false,
  compact = false,
  disabled,
  style,
  textStyle,
  testID,
  ...props
}: ButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  const foreground =
    variant === "primary"
      ? theme.primaryForeground
      : variant === "danger"
        ? theme.primaryForeground
        : variant === "outline" || variant === "ghost"
          ? theme.text
          : theme.text;
  const backgroundColor =
    variant === "primary"
      ? theme.primary
      : variant === "danger"
        ? theme.danger
        : variant === "secondary"
          ? theme.surfaceMuted
          : "transparent";
  const borderColor = variant === "outline" ? theme.border : "transparent";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: Boolean(disabled || busy), busy }}
      disabled={disabled || busy}
      testID={testID}
      style={(state) => [
        styles.button,
        {
          minHeight: compact ? 40 : 48,
          paddingHorizontal: compact ? 14 : 18,
          backgroundColor,
          borderColor,
          borderWidth: variant === "outline" ? StyleSheet.hairlineWidth : 0,
          opacity: disabled ? 0.48 : state.pressed ? 0.78 : 1,
        },
        typeof style === "function" ? style(state) : style,
      ]}
      {...props}
    >
      {busy ? <ActivityIndicator size="small" color={foreground} /> : icon}
      <Text style={[styles.buttonText, { color: foreground }, textStyle]}>
        {busy ? `${title}…` : title}
      </Text>
    </Pressable>
  );
}

export interface SheetProps extends Pick<
  ModalProps,
  "onRequestClose" | "statusBarTranslucent"
> {
  visible: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  testID?: string;
  dismissible?: boolean;
  maxHeight?: number | `${number}%`;
}

/** Bottom-anchored native Modal sheet with safe-area handling and a scrollable body. */
export function Sheet({
  visible,
  onClose,
  title,
  children,
  footer,
  testID,
  dismissible = true,
  maxHeight = "88%",
  onRequestClose,
  statusBarTranslucent = true,
}: SheetProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onRequestClose ?? onClose}
      statusBarTranslucent={statusBarTranslucent}
    >
      <View style={styles.modalRoot}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Close sheet"
          onPress={dismissible ? onClose : undefined}
          disabled={!dismissible}
          style={[StyleSheet.absoluteFill, { backgroundColor: theme.overlay }]}
        />
        <View
          testID={testID}
          accessibilityViewIsModal
          style={[
            styles.sheet,
            {
              backgroundColor: theme.raised,
              borderColor: theme.border,
              maxHeight,
              paddingBottom: Math.max(insets.bottom, 16),
            },
          ]}
        >
          <View style={styles.sheetHandle} />
          {title || dismissible ? (
            <View style={styles.sheetHeader}>
              {title ? (
                <Text
                  accessibilityRole="header"
                  style={[styles.sheetTitle, { color: theme.text }]}
                >
                  {title}
                </Text>
              ) : (
                <View style={{ flex: 1 }} />
              )}
              {dismissible ? (
                <IconButton
                  icon={<X size={19} color={theme.mutedText} />}
                  label="Close"
                  onPress={onClose}
                  testID={testID ? `${testID}-close` : undefined}
                />
              ) : null}
            </View>
          ) : null}
          <ScrollView
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={styles.sheetBody}
          >
            {children}
          </ScrollView>
          {footer ? (
            <View
              style={[styles.sheetFooter, { borderTopColor: theme.border }]}
            >
              {footer}
            </View>
          ) : null}
        </View>
      </View>
    </Modal>
  );
}

export type BannerTone = "info" | "success" | "warning" | "error";
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
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={actionLabel}
            onPress={onAction}
            style={styles.bannerAction}
          >
            <Text style={[styles.bannerActionText, { color }]}>
              {actionLabel}
            </Text>
          </Pressable>
        ) : null}
      </View>
      {onDismiss ? (
        <IconButton
          icon={<X size={17} color={theme.mutedText} />}
          label="Dismiss message"
          onPress={onDismiss}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  iconButton: {
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 24,
  },
  button: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderRadius: 14,
  },
  buttonText: { fontSize: 15, lineHeight: 21, fontWeight: "600" },
  modalRoot: { flex: 1, justifyContent: "flex-end" },
  sheet: {
    width: "100%",
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    borderWidth: StyleSheet.hairlineWidth,
    paddingTop: 10,
    overflow: "hidden",
  },
  sheetHandle: {
    alignSelf: "center",
    width: 38,
    height: 4,
    borderRadius: 2,
    backgroundColor: "#8B8B97",
    opacity: 0.45,
    marginBottom: 8,
  },
  sheetHeader: {
    minHeight: 48,
    paddingLeft: 20,
    paddingRight: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  sheetTitle: { flex: 1, fontSize: 18, lineHeight: 24, fontWeight: "600" },
  sheetBody: {
    paddingHorizontal: 20,
    paddingTop: 8,
    paddingBottom: 20,
    gap: 12,
  },
  sheetFooter: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 20,
    paddingTop: 12,
    gap: 8,
  },
  banner: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 16,
    padding: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  bannerTitle: { fontSize: 13, fontWeight: "600", lineHeight: 18 },
  bannerMessage: { fontSize: 14, lineHeight: 20 },
  bannerAction: {
    alignSelf: "flex-start",
    minHeight: 40,
    justifyContent: "center",
  },
  bannerActionText: { fontSize: 14, fontWeight: "600" },
});
