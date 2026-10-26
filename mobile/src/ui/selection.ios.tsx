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
  accessibilityLabel,
  buttonStyle,
  disabled,
  font,
  foregroundStyle,
  frame,
  pickerStyle,
  tag,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeOptionProps, NativePickerProps } from "./selection.types";
import { useTheme } from "./theme";

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

/** iOS uses its standard checkmark selection rows; Android uses radio/checkboxes. */
export function NativeOption(props: NativeOptionProps): React.JSX.Element {
  const { theme } = useTheme();
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
          frame({ minHeight: 48, maxWidth: Infinity }),
          accessibilityLabel(
            [props.label, props.description, props.selected ? "Selected" : ""]
              .filter(Boolean)
              .join(", "),
          ),
        ]}
      >
        <HStack spacing={12}>
          <VStack alignment="leading" spacing={4}>
            <Text
              modifiers={[
                font({ textStyle: "body" }),
                foregroundStyle(theme.text),
              ]}
            >
              {props.label}
            </Text>
            {props.description ? (
              <Text
                modifiers={[
                  font({ textStyle: "subheadline" }),
                  foregroundStyle(theme.mutedText),
                ]}
              >
                {props.description}
              </Text>
            ) : null}
          </VStack>
          <Spacer />
          {props.selected ? (
            <Image systemName="checkmark" color={theme.primary} size={18} />
          ) : null}
        </HStack>
      </Button>
    </Host>
  );
}
