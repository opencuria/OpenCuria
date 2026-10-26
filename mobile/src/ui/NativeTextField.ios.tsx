import React, { useEffect, useImperativeHandle, useRef } from "react";
import {
  Host,
  SecureField,
  TextField,
  useNativeState,
  type TextFieldRef,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  autocorrectionDisabled,
  frame,
  keyboardType,
  onSubmit,
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
    placeholder: props.placeholder ?? props.label,
    onTextChange: (value: string) => {
      latest.current = value;
      props.onChangeText(value);
    },
    onFocusChange: (focused: boolean) => {
      if (focused) props.onFocus?.();
    },
    modifiers: [
      textFieldStyle("roundedBorder"),
      frame({ minHeight: 44, maxWidth: Infinity }),
      accessibilityLabel(props.accessibilityLabel ?? props.label),
      keyboardType(props.keyboardType ?? "default"),
      autocorrectionDisabled(),
      textInputAutocapitalization("never"),
      submitLabel(props.returnKeyType ?? "done"),
      onSubmit(() => props.onSubmitEditing?.()),
      ...(props.label === "Password"
        ? [textContentType("password")]
        : props.label === "Email"
          ? [textContentType("username")]
          : []),
    ],
  };
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      {props.secureTextEntry ? (
        <SecureField {...common} ref={field} />
      ) : (
        <TextField {...common} ref={field} />
      )}
    </Host>
  );
}
