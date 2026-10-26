import React, { useEffect, useImperativeHandle, useRef } from "react";
import {
  Host,
  OutlinedTextField,
  Text,
  useNativeState,
  type TextFieldRef,
} from "@expo/ui/jetpack-compose";
import { fillMaxWidth, semantics } from "@expo/ui/jetpack-compose/modifiers";
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
  const submit = () => props.onSubmitEditing?.();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeAreaKeyboardInsets
    >
      <OutlinedTextField
        ref={field}
        autoFocus={props.autoFocus}
        value={text}
        singleLine
        visualTransformation={props.secureTextEntry ? "password" : "none"}
        onValueChange={(value) => {
          latest.current = value;
          props.onChangeText(value);
        }}
        onFocusChanged={(focused) => {
          if (focused) props.onFocus?.();
        }}
        keyboardOptions={{
          capitalization: "none",
          autoCorrectEnabled: false,
          keyboardType: props.secureTextEntry
            ? "password"
            : props.keyboardType === "email-address"
              ? "email"
              : props.keyboardType === "url"
                ? "uri"
                : "text",
          imeAction: props.returnKeyType ?? "done",
        }}
        keyboardActions={
          props.onSubmitEditing
            ? { onDone: submit, onNext: submit, onGo: submit }
            : undefined
        }
        modifiers={[
          fillMaxWidth(),
          semantics({
            contentDescription: props.accessibilityLabel ?? props.label,
            ...(props.label === "Email"
              ? { contentType: "email" }
              : props.label === "Password"
                ? { contentType: "password" }
                : {}),
          }),
        ]}
      >
        <OutlinedTextField.Label>
          <Text>{props.label}</Text>
        </OutlinedTextField.Label>
        {props.placeholder ? (
          <OutlinedTextField.Placeholder>
            <Text>{props.placeholder}</Text>
          </OutlinedTextField.Placeholder>
        ) : null}
      </OutlinedTextField>
    </Host>
  );
}
