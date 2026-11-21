import React from "react";
import { View } from "react-native";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button as SwiftButton,
  HStack,
  Host,
  Image,
  RNHostView,
  Text,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  accessibilityHint,
  accessibilityValue,
  buttonStyle,
  buttonBorderShape,
  controlSize,
  disabled,
  font,
  foregroundStyle,
  frame,
  labelStyle,
  lineLimit,
  onLongPressGesture,
  tint,
} from "@expo/ui/swift-ui/modifiers";
import type {
  ButtonProps,
  IconButtonProps,
  NativeActionProps,
  NativeSymbol,
} from "./controls.types";
import {
  controlContentStyle,
  controlLayout,
  hugsContent,
} from "./controlLayout";
import { statusColor } from "./statusColors";
import { useTheme } from "./theme";

/** Floating controls are liquid glass on iOS 26 and bordered before it. */
const FLOATING_STYLE = isLiquidGlassAvailable() ? "glass" : "bordered";

/**
 * Label of a prominent button tinted with the ink. SwiftUI draws white on the
 * tint, which vanishes on the near-white ink of dark mode, so an enabled label
 * takes `primaryForeground`; a disabled one keeps the system's dimmed color.
 */
export function ProminentLabel({
  title,
  symbol,
  enabled = true,
}: {
  title: string;
  symbol?: NativeSymbol;
  enabled?: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  const color = enabled ? [foregroundStyle(theme.primaryForeground)] : [];
  return (
    <HStack spacing={6}>
      {symbol ? (
        <Image
          systemName={symbol}
          modifiers={[
            font({ textStyle: "body", weight: "semibold" }),
            ...color,
          ]}
        />
      ) : null}
      <Text
        modifiers={[
          font({ textStyle: "body", weight: "semibold" }),
          lineLimit(1),
          ...color,
        ]}
      >
        {title}
      </Text>
    </HStack>
  );
}

/** SwiftUI owns highlighting, hit testing, disabled state and button appearance. */
export function Button({
  title,
  symbol,
  variant = "secondary",
  busy,
  compact,
  shape,
  disabled: inactive,
  onPress,
  style,
  accessibilityLabel: accessibleLabel,
  testID,
}: ButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  const layout = controlLayout(style);
  const hug = hugsContent(layout);
  const prominent = variant === "primary";
  const label = busy ? `${title}…` : title;
  return (
    <Host
      matchContents={{ vertical: true, horizontal: hug }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
      style={[{ alignSelf: "stretch" }, layout]}
    >
      <SwiftButton
        label={prominent ? undefined : label}
        systemImage={prominent ? undefined : symbol}
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
          ...(shape ? [buttonBorderShape(shape)] : []),
          frame(
            hug ? { minHeight: 44 } : { maxWidth: Infinity, minHeight: 44 },
          ),
          // Danger fills with the system red (white label in both modes);
          // the others take the ink (a primary label sets its own color).
          tint(
            variant === "danger"
              ? statusColor("critical", theme)
              : theme.primary,
          ),
          disabled(Boolean(inactive || busy)),
          accessibilityLabel(accessibleLabel ?? title),
        ]}
      >
        {prominent ? (
          <ProminentLabel
            title={label}
            symbol={symbol}
            enabled={!inactive && !busy}
          />
        ) : undefined}
      </SwiftButton>
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
  // A filled ink circle draws its symbol itself, in the label-on-ink color.
  const filled = tone === "primary" ? symbol : undefined;
  return (
    <Host
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
      style={{ width: Math.max(44, size), height: Math.max(44, size) }}
    >
      <SwiftButton
        label={symbol && !filled ? label : undefined}
        systemImage={filled ? undefined : symbol}
        testID={testID}
        onPress={() => onPress?.()}
        modifiers={[
          buttonStyle(
            tone === "primary"
              ? "borderedProminent"
              : tone === "floating"
                ? FLOATING_STYLE
                : "borderless",
          ),
          buttonBorderShape("circle"),
          labelStyle("iconOnly"),
          controlSize("large"),
          // Quiet symbols follow the subheadline size instead of the large control.
          ...(tone === "quiet" ? [font({ textStyle: "subheadline" })] : []),
          frame({ minWidth: 44, minHeight: 44 }),
          disabled(Boolean(inactive)),
          tint(
            tone === "danger"
              ? theme.danger
              : tone === "floating"
                ? theme.text
                : tone === "quiet"
                  ? theme.mutedText
                  : theme.primary,
          ),
          accessibilityLabel(label),
          ...(hint ? [accessibilityHint(hint)] : []),
          ...(value?.text ? [accessibilityValue(value.text)] : []),
        ]}
      >
        {filled ? (
          <Image
            systemName={filled}
            modifiers={[
              font({ textStyle: "body", weight: "semibold" }),
              ...(inactive ? [] : [foregroundStyle(theme.primaryForeground)]),
            ]}
          />
        ) : !symbol ? (
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
  onLongPress,
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
            ...(onLongPress ? [onLongPressGesture(onLongPress)] : []),
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
