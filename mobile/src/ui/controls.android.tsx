import React from "react";
import { StyleSheet, View } from "react-native";
import {
  Button as ComposeButton,
  FilledTonalButton,
  TextButton,
  IconButton as ComposeIconButton,
  FilledIconButton,
  Host,
  Surface,
  Text,
  type ButtonColors,
} from "@expo/ui/jetpack-compose";
import {
  defaultMinSize,
  combinedClickable,
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
import {
  controlContentStyle,
  controlLayout,
  hugsContent,
} from "./controlLayout";
import { useTheme, type OpenCuriaTheme } from "./theme";

const TRANSPARENT = "#00000000";

/**
 * Material buttons keep their native sizing, shape and ripple; every variant
 * gets explicit monochrome colors instead of the seeded palette.
 */
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
    variant === "ghost" || variant === "outline"
      ? TextButton
      : variant === "secondary"
        ? FilledTonalButton
        : ComposeButton;
  const layout = controlLayout(style);
  const hug = hugsContent(layout);
  // A taller hero action (e.g. sign in) passes its height as `minHeight`.
  const minHeight =
    typeof layout.minHeight === "number" ? Math.max(48, layout.minHeight) : 48;
  return (
    <View
      testID={testID}
      style={[
        { alignSelf: "stretch" },
        layout,
        // Compose draws the outlined button's border from the seeded palette;
        // a text button inside a hairline capsule keeps it neutral.
        variant === "outline" && {
          borderWidth: 1,
          borderColor: theme.separator,
          borderRadius: 999,
        },
      ]}
    >
      <Host
        matchContents={{ vertical: true, horizontal: hug }}
        colorScheme={theme.scheme}
        seedColor={theme.seed}
        ignoreSafeAreaKeyboardInsets
      >
        <Component
          onClick={() => onPress?.()}
          enabled={!disabled && !busy}
          colors={buttonColors(variant, theme)}
          modifiers={[
            ...(hug ? [] : [fillMaxWidth()]),
            defaultMinSize({ minHeight }),
            semantics({ contentDescription: accessibilityLabel ?? title }),
          ]}
        >
          <Text>{busy ? `${title}…` : title}</Text>
        </Component>
      </Host>
    </View>
  );
}

function buttonColors(
  variant: NonNullable<ButtonProps["variant"]>,
  theme: OpenCuriaTheme,
): ButtonColors {
  switch (variant) {
    case "primary":
      return {
        containerColor: theme.primary,
        contentColor: theme.primaryForeground,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      };
    case "danger":
      return {
        containerColor: theme.danger,
        contentColor: theme.primaryForeground,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      };
    case "secondary":
      return {
        containerColor: theme.controlFill,
        contentColor: theme.text,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      };
    case "outline":
    case "ghost":
      return {
        containerColor: TRANSPARENT,
        contentColor: theme.text,
        disabledContainerColor: TRANSPARENT,
        disabledContentColor: theme.subtleText,
      };
  }
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
  const Component = tone === "primary" ? FilledIconButton : ComposeIconButton;
  return (
    <View
      testID={testID}
      style={[
        { width: diameter, height: diameter },
        // Floating controls sit over the transcript: a neutral surface instead
        // of the seed-tinted tonal container.
        tone === "floating" && {
          borderRadius: diameter / 2,
          backgroundColor: theme.surfaceMuted,
        },
      ]}
    >
      <Host
        colorScheme={theme.scheme}
        seedColor={theme.seed}
        ignoreSafeAreaKeyboardInsets
        style={{ width: diameter, height: diameter }}
      >
        <Component
          enabled={!disabled}
          onClick={() => onPress?.()}
          // The icon is drawn by React Native above; the content color tints
          // the ripple.
          colors={
            tone === "primary"
              ? {
                  containerColor: theme.primary,
                  contentColor: theme.primaryForeground,
                  disabledContainerColor: theme.controlFill,
                  disabledContentColor: theme.subtleText,
                }
              : {
                  containerColor: TRANSPARENT,
                  contentColor: tone === "danger" ? theme.danger : theme.text,
                  disabledContainerColor: TRANSPARENT,
                  disabledContentColor: theme.subtleText,
                }
          }
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
  onLongPress,
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
        seedColor={theme.seed}
        ignoreSafeAreaKeyboardInsets
      >
        <Surface
          onClick={onLongPress ? undefined : () => onPress?.()}
          enabled={!disabled}
          color="transparent"
          modifiers={[
            fillMaxSize(),
            ...(onLongPress
              ? [
                  combinedClickable({
                    onClick: disabled ? undefined : onPress,
                    onLongClick: disabled ? undefined : onLongPress,
                  }),
                ]
              : []),
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
