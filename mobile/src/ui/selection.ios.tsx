import React from "react";
import {
  Button,
  HStack,
  Host,
  Image,
  Picker,
  Spacer,
  Text,
  VStack,
} from "@expo/ui/swift-ui";
import {
  accessibilityAddTraits,
  accessibilityHidden,
  accessibilityLabel,
  buttonStyle,
  contentShape,
  disabled,
  font,
  foregroundStyle,
  frame,
  multilineTextAlignment,
  padding,
  pickerStyle,
  shapes,
  tag,
} from "@expo/ui/swift-ui/modifiers";
import type {
  NativeOptionProps,
  NativePickerProps,
  NativeSegmentedPickerProps,
  OptionRowMetrics,
} from "./selection.types";
import { useTheme } from "./theme";

/** A title3 symbol beside a body label (17 pt). */
export const OPTION_ROW: OptionRowMetrics = {
  inset: 14,
  indicator: 22,
  gap: 12,
  minHeight: 48,
  fontSize: 17,
  lineHeight: 22,
};

/** UISegmentedControl for a few mutually exclusive, always visible choices. */
export function NativeSegmentedPicker(
  props: NativeSegmentedPickerProps,
): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Picker
        label={props.label}
        selection={props.value}
        testID={props.testID}
        onSelectionChange={(value: string) => {
          if (value !== props.value) props.onChange(value);
        }}
        modifiers={[pickerStyle("segmented")]}
      >
        {props.options.map((option) => (
          <Text
            key={option.value}
            modifiers={[
              tag(option.value),
              ...(option.accessibilityLabel
                ? [accessibilityLabel(option.accessibilityLabel)]
                : []),
            ]}
          >
            {option.label}
          </Text>
        ))}
      </Picker>
    </Host>
  );
}

export function NativePicker(props: NativePickerProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Picker
        label={props.label}
        selection={props.value}
        testID={props.testID}
        onSelectionChange={(value: string) => {
          if (!props.options.find((option) => option.value === value)?.disabled)
            props.onChange(value);
        }}
        modifiers={[
          pickerStyle("menu"),
          disabled(Boolean(props.disabled)),
          accessibilityLabel(props.label),
          frame({ minHeight: 44, maxWidth: Infinity }),
        ]}
      >
        {props.options.map((option) => (
          <Text
            key={option.value}
            modifiers={[tag(option.value), disabled(Boolean(option.disabled))]}
          >
            {option.label}
          </Text>
        ))}
      </Picker>
    </Host>
  );
}

/**
 * A choice row as in Reminders: a leading circle (one choice) or square (any
 * number) fills with the ink when picked, beside a leading-aligned label and
 * description. The whole row is the target; Android uses radio/checkboxes.
 */
export function NativeOption(props: NativeOptionProps): React.JSX.Element {
  const { theme } = useTheme();
  const shape = props.multiple ? "square" : "circle";
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <Button
        onPress={props.onPress}
        modifiers={[
          buttonStyle("borderless"),
          disabled(Boolean(props.disabled)),
          accessibilityLabel(
            [props.label, props.description].filter(Boolean).join(", "),
          ),
          accessibilityAddTraits(props.selected ? ["isSelected"] : []),
        ]}
      >
        <HStack
          alignment="firstTextBaseline"
          spacing={OPTION_ROW.gap}
          modifiers={[
            padding({ horizontal: OPTION_ROW.inset, vertical: 12 }),
            frame({
              maxWidth: Infinity,
              minHeight: OPTION_ROW.minHeight,
              alignment: "leading",
            }),
            contentShape(shapes.rectangle()),
          ]}
        >
          <Image
            systemName={props.selected ? `checkmark.${shape}.fill` : shape}
            modifiers={[
              font({ textStyle: "title3" }),
              foregroundStyle(props.selected ? theme.primary : theme.subtleText),
              accessibilityHidden(true),
            ]}
          />
          <VStack alignment="leading" spacing={3}>
            <Text
              modifiers={[
                font({ textStyle: "body" }),
                foregroundStyle(theme.text),
                multilineTextAlignment("leading"),
              ]}
            >
              {props.label}
            </Text>
            {props.description ? (
              <Text
                modifiers={[
                  font({ textStyle: "subheadline" }),
                  foregroundStyle(theme.mutedText),
                  multilineTextAlignment("leading"),
                ]}
              >
                {props.description}
              </Text>
            ) : null}
          </VStack>
          <Spacer />
        </HStack>
      </Button>
    </Host>
  );
}
