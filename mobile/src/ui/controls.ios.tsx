import React from "react";
import { View } from "react-native";
import { Button as SwiftButton, Host, RNHostView } from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  accessibilityHint,
  accessibilityValue,
  buttonStyle,
  buttonBorderShape,
  controlSize,
  disabled,
  frame,
  labelStyle,
  tint,
} from "@expo/ui/swift-ui/modifiers";
import type {
  ButtonProps,
  IconButtonProps,
  NativeActionProps,
} from "./controls.types";
import { controlContentStyle, controlLayout } from "./controlLayout";
import { useTheme } from "./theme";

/** SwiftUI owns highlighting, hit testing, disabled state and button appearance. */
export function Button({
  title,
  symbol,
  variant = "secondary",
  busy,
  compact,
  disabled: inactive,
  onPress,
  style,
  accessibilityLabel: label,
  testID,
}: ButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
      style={[{ alignSelf: "stretch" }, controlLayout(style)]}
    >
      <SwiftButton
        label={busy ? `${title}…` : title}
        systemImage={symbol}
        testID={testID}
        role={variant === "danger" ? "destructive" : undefined}
        onPress={() => onPress?.()}
        modifiers={[
          buttonStyle(
            variant === "primary" || variant === "danger"
              ? "borderedProminent"
              : variant === "ghost"
                ? "borderless"
                : "bordered",
          ),
          controlSize(compact ? "regular" : "large"),
          frame({ maxWidth: Infinity, minHeight: 44 }),
          disabled(Boolean(inactive || busy)),
          accessibilityLabel(label ?? title),
        ]}
      />
    </Host>
  );
}

/** System symbols and native button styles, including the filled send/stop action. */
export function IconButton({
  icon,
  symbol,
  label,
  size = 44,
  tone = "neutral",
  disabled: inactive,
  onPress,
  accessibilityHint: hint,
  accessibilityValue: value,
  testID,
}: IconButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
      style={{ width: Math.max(44, size), height: Math.max(44, size) }}
    >
      <SwiftButton
        label={symbol ? label : undefined}
        systemImage={symbol}
        testID={testID}
        onPress={() => onPress?.()}
        modifiers={[
          buttonStyle(tone === "primary" ? "borderedProminent" : "borderless"),
          buttonBorderShape("circle"),
          labelStyle("iconOnly"),
          controlSize("large"),
          frame({ minWidth: 44, minHeight: 44 }),
          disabled(Boolean(inactive)),
          tint(tone === "danger" ? theme.danger : theme.primary),
          accessibilityLabel(label),
          ...(hint ? [accessibilityHint(hint)] : []),
          ...(value?.text ? [accessibilityValue(value.text)] : []),
        ]}
      >
        {!symbol ? (
          <RNHostView matchContents>
            <View
              style={{
                width: 24,
                height: 24,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {icon}
            </View>
          </RNHostView>
        ) : undefined}
      </SwiftButton>
    </Host>
  );
}

/** A native button for transcript content such as files and disclosure rows. */
export function NativeAction({
  children,
  onPress,
  disabled: inactive,
  fitContent,
  style,
  testID,
  accessibilityLabel: label,
  accessibilityHint: hint,
}: NativeActionProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={controlLayout(style)}>
      <Host
        style={fitContent ? undefined : { width: "100%" }}
        matchContents={fitContent ? true : { vertical: true }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeArea="all"
      >
        <SwiftButton
          onPress={() => onPress?.()}
          testID={testID}
          modifiers={[
            buttonStyle("borderless"),
            disabled(Boolean(inactive)),
            ...(label ? [accessibilityLabel(label)] : []),
            ...(hint ? [accessibilityHint(hint)] : []),
          ]}
        >
          <RNHostView matchContents={fitContent ? true : { vertical: true }}>
            <View style={controlContentStyle(style)}>{children}</View>
          </RNHostView>
        </SwiftButton>
      </Host>
    </View>
  );
}
