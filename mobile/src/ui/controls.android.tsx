import React from "react";
import { StyleSheet, View } from "react-native";
import {
  Button as ComposeButton,
  FilledTonalButton,
  OutlinedButton,
  TextButton,
  IconButton as ComposeIconButton,
  FilledIconButton,
  FilledTonalIconButton,
  Host,
  Surface,
  Text,
} from "@expo/ui/jetpack-compose";
import {
  defaultMinSize,
  fillMaxWidth,
  fillMaxSize,
  semantics,
  size,
} from "@expo/ui/jetpack-compose/modifiers";
import type {
  ButtonProps,
  IconButtonProps,
  NativeActionProps,
} from "./controls.types";
import { controlContentStyle, controlLayout } from "./controlLayout";
import { useTheme } from "./theme";

/** Material buttons retain their native sizing, shape, ripple and disabled colors. */
export function Button({
  title,
  variant = "secondary",
  busy,
  disabled,
  onPress,
  style,
  accessibilityLabel,
  testID,
}: ButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  const Component =
    variant === "ghost"
      ? TextButton
      : variant === "outline"
        ? OutlinedButton
        : variant === "secondary"
          ? FilledTonalButton
          : ComposeButton;
  return (
    <View
      testID={testID}
      style={[{ alignSelf: "stretch" }, controlLayout(style)]}
    >
      <Host
        matchContents={{ vertical: true }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Component
          onClick={() => onPress?.()}
          enabled={!disabled && !busy}
          colors={
            variant === "danger"
              ? {
                  containerColor: theme.danger,
                  contentColor: theme.primaryForeground,
                }
              : undefined
          }
          modifiers={[
            fillMaxWidth(),
            defaultMinSize({ minHeight: 48 }),
            semantics({ contentDescription: accessibilityLabel ?? title }),
          ]}
        >
          <Text>{busy ? `${title}…` : title}</Text>
        </Component>
      </Host>
    </View>
  );
}

export function IconButton({
  icon,
  label,
  size: diameter = 48,
  tone = "neutral",
  disabled,
  onPress,
  testID,
}: IconButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  const Component =
    tone === "primary"
      ? FilledIconButton
      : tone === "floating"
        ? FilledTonalIconButton
        : ComposeIconButton;
  return (
    <View testID={testID} style={{ width: diameter, height: diameter }}>
      <Host
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
        style={{ width: diameter, height: diameter }}
      >
        <Component
          enabled={!disabled}
          onClick={() => onPress?.()}
          modifiers={[
            size(diameter, diameter),
            semantics({ contentDescription: label }),
          ]}
        >
          {null}
        </Component>
      </Host>
      <View
        pointerEvents="none"
        style={{
          position: "absolute",
          inset: 0,
          alignItems: "center",
          justifyContent: "center",
          opacity: disabled ? 0.38 : 1,
        }}
      >
        {icon}
      </View>
    </View>
  );
}

/** React Native measures rich labels; the native Material surface owns taps and ripple. */
export function NativeAction({
  children,
  onPress,
  disabled,
  style,
  testID,
  accessibilityLabel,
}: NativeActionProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View collapsable={false} testID={testID} style={controlLayout(style)}>
      <View pointerEvents="none" style={controlContentStyle(style)}>
        {children}
      </View>
      <Host
        style={StyleSheet.absoluteFill}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Surface
          onClick={() => onPress?.()}
          enabled={!disabled}
          color="transparent"
          modifiers={[
            fillMaxSize(),
            ...(accessibilityLabel
              ? [semantics({ contentDescription: accessibilityLabel })]
              : []),
          ]}
        >
          {null}
        </Surface>
      </Host>
    </View>
  );
}
