import React, { useEffect, useImperativeHandle, useRef } from "react";
import {
  Button,
  HStack,
  Host,
  Image,
  SecureField,
  TextField,
  useNativeState,
  type TextFieldRef,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  autocorrectionDisabled,
  buttonStyle,
  contentShape,
  font,
  foregroundStyle,
  frame,
  keyboardType,
  onSubmit,
  shapes,
  submitLabel,
  textContentType,
  textFieldStyle,
  textInputAutocapitalization,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeTextFieldProps } from "./NativeTextField.types";
import { useTheme } from "./theme";

export function NativeTextField(
  props: NativeTextFieldProps,
): React.JSX.Element {
  const { theme } = useTheme();
  const text = useNativeState(props.value);
  const latest = useRef(props.value);
  const field = useRef<TextFieldRef>(null);
  useEffect(() => {
    if (props.value !== latest.current) {
      latest.current = props.value;
      text.set(props.value);
    }
  }, [props.value, text]);
  useImperativeHandle(props.ref, () => ({
    focus: () => {
      void field.current?.focus();
    },
  }));
  const common = {
    text,
    autoFocus: props.autoFocus,
    testID: props.testID,
    // A grouped row has no label of its own; its placeholder names it.
    placeholder: props.grouped
      ? props.label
      : (props.placeholder ?? props.label),
    onTextChange: (value: string) => {
      latest.current = value;
      props.onChangeText(value);
    },
    onFocusChange: (focused: boolean) => {
      if (focused) props.onFocus?.();
    },
    modifiers: [
      textFieldStyle(props.grouped ? "plain" : "roundedBorder"),
      frame({ minHeight: props.grouped ? 52 : 44, maxWidth: Infinity }),
      accessibilityLabel(props.accessibilityLabel ?? props.label),
      keyboardType(props.keyboardType ?? "default"),
      ...(props.prose ? [] : [autocorrectionDisabled()]),
      textInputAutocapitalization(props.prose ? "sentences" : "never"),
      submitLabel(props.returnKeyType ?? "done"),
      onSubmit(() => props.onSubmitEditing?.()),
      ...(props.label === "Password"
        ? [textContentType("password")]
        : props.label === "Email"
          ? [textContentType("username")]
          : []),
    ],
  };
  const input = props.secureTextEntry ? (
    <SecureField {...common} ref={field} />
  ) : (
    <TextField {...common} ref={field} />
  );
  const action = props.trailingAction;
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      {action ? (
        <HStack spacing={4}>
          {input}
          <Button
            onPress={action.onPress}
            testID={action.testID}
            modifiers={[
              buttonStyle("borderless"),
              accessibilityLabel(action.label),
            ]}
          >
            <Image
              systemName={action.symbol}
              modifiers={[
                font({ textStyle: "body" }),
                foregroundStyle(theme.subtleText),
                // The glyph sits at the row's trailing inset; the 44 pt frame
                // around it is the target.
                frame({ minWidth: 44, minHeight: 44, alignment: "trailing" }),
                contentShape(shapes.rectangle()),
              ]}
            />
          </Button>
        </HStack>
      ) : (
        input
      )}
    </Host>
  );
}
