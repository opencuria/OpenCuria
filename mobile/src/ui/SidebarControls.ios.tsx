import React, { useEffect, useRef } from "react";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  HStack,
  Host,
  Image,
  Text,
  TextField,
  useNativeState,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  autocorrectionDisabled,
  background,
  buttonBorderShape,
  buttonStyle,
  controlSize,
  disabled,
  font,
  foregroundStyle,
  frame,
  glassEffect,
  labelStyle,
  padding,
  shapes,
  submitLabel,
  textInputAutocapitalization,
  tint,
} from "@expo/ui/swift-ui/modifiers";
import type {
  AccountButtonProps,
  NewChatButtonProps,
  SidebarIconButtonProps,
  SidebarSearchFieldProps,
} from "./SidebarControls.types";
import { useTheme } from "./theme";

/** iOS 26 floats sidebar controls as liquid glass; earlier systems use bordered buttons. */
const GLASS = isLiquidGlassAvailable();

/** A round liquid-glass icon button, as in the system's floating bars. */
export function SidebarIconButton({
  symbol,
  label,
  onPress,
  testID,
}: SidebarIconButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Button
        label={label}
        systemImage={symbol}
        testID={testID}
        onPress={onPress}
        modifiers={[
          buttonStyle(GLASS ? "glass" : "bordered"),
          buttonBorderShape("circle"),
          labelStyle("iconOnly"),
          controlSize("large"),
          tint(theme.text),
          frame({ minWidth: 44, minHeight: 44 }),
          accessibilityLabel(label),
        ]}
      />
    </Host>
  );
}

/** The user's initials on a liquid-glass circle; opens Settings. */
export function AccountButton({
  initials,
  label,
  onPress,
  testID,
}: AccountButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Button
        onPress={onPress}
        testID={testID}
        modifiers={[
          buttonStyle(GLASS ? "glass" : "bordered"),
          buttonBorderShape("circle"),
          controlSize("large"),
          accessibilityLabel(label),
        ]}
      >
        <Text
          modifiers={[
            font({ size: 16, weight: "semibold" }),
            foregroundStyle(theme.text),
            frame({ width: 34, height: 34 }),
          ]}
        >
          {initials}
        </Text>
      </Button>
    </Host>
  );
}

/** A prominent capsule in the text color, tinted liquid glass on iOS 26. */
export function NewChatButton({
  onPress,
  disabled: inactive,
  testID,
}: NewChatButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  // Disabled buttons keep the system's dimmed label instead of a forced color.
  const label = inactive ? [] : [foregroundStyle(theme.background)];
  return (
    <Host
      matchContents
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Button
        onPress={onPress}
        testID={testID}
        modifiers={[
          buttonStyle(GLASS ? "glassProminent" : "borderedProminent"),
          buttonBorderShape("capsule"),
          controlSize("large"),
          tint(theme.text),
          disabled(Boolean(inactive)),
          accessibilityLabel("New chat"),
        ]}
      >
        <HStack spacing={7} modifiers={[padding({ horizontal: 4 })]}>
          <Image
            systemName="square.and.pencil"
            modifiers={[font({ size: 16, weight: "semibold" }), ...label]}
          />
          <Text modifiers={[font({ size: 17, weight: "semibold" }), ...label]}>
            New chat
          </Text>
        </HStack>
      </Button>
    </Host>
  );
}

/**
 * The system search field look: a capsule with the magnifier, a clear button
 * while text is entered and a close control; liquid glass on iOS 26.
 */
export function SidebarSearchField({
  value,
  onChange,
  onCancel,
  placeholder,
  accessibilityLabel: label,
  testID,
}: SidebarSearchFieldProps): React.JSX.Element {
  const { theme } = useTheme();
  const text = useNativeState(value);
  const latest = useRef(value);
  useEffect(() => {
    if (value !== latest.current) {
      latest.current = value;
      text.set(value);
    }
  }, [value, text]);
  const change = (next: string) => {
    latest.current = next;
    onChange(next);
  };
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
      style={{ flex: 1 }}
    >
      <HStack spacing={10}>
        <HStack
          spacing={8}
          modifiers={[
            padding({ leading: 14, trailing: 10 }),
            frame({ maxWidth: Infinity, minHeight: 44 }),
            GLASS
              ? glassEffect({
                  glass: { variant: "regular", interactive: true },
                  shape: "capsule",
                })
              : background(theme.controlFill, shapes.capsule()),
          ]}
        >
          <Image
            systemName="magnifyingglass"
            modifiers={[
              font({ size: 16, weight: "medium" }),
              foregroundStyle(theme.mutedText),
            ]}
          />
          <TextField
            text={text}
            autoFocus
            placeholder={placeholder}
            testID={testID}
            onTextChange={change}
            modifiers={[
              accessibilityLabel(label),
              autocorrectionDisabled(),
              textInputAutocapitalization("never"),
              submitLabel("search"),
              font({ size: 17 }),
            ]}
          />
          {value ? (
            <Button
              onPress={() => {
                text.set("");
                change("");
              }}
              modifiers={[
                buttonStyle("plain"),
                accessibilityLabel("Clear search"),
              ]}
            >
              <Image
                systemName="xmark.circle.fill"
                modifiers={[
                  font({ size: 16 }),
                  foregroundStyle(theme.subtleText),
                ]}
              />
            </Button>
          ) : null}
        </HStack>
        {GLASS ? (
          <Button
            label="Cancel"
            systemImage="xmark"
            onPress={onCancel}
            modifiers={[
              buttonStyle("glass"),
              buttonBorderShape("circle"),
              labelStyle("iconOnly"),
              controlSize("large"),
              tint(theme.text),
              accessibilityLabel("Cancel"),
            ]}
          />
        ) : (
          <Button
            label="Cancel"
            onPress={onCancel}
            modifiers={[buttonStyle("borderless"), tint(theme.text)]}
          />
        )}
      </HStack>
    </Host>
  );
}
