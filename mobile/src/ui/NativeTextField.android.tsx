import React, { useEffect, useImperativeHandle, useRef } from "react";
import {
  Host,
  OutlinedTextField,
  Shape,
  Text,
  useNativeState,
  type ShapeJSXElement,
  type TextFieldColors,
  type TextFieldRef,
} from "@expo/ui/jetpack-compose";
import { fillMaxWidth, semantics } from "@expo/ui/jetpack-compose/modifiers";
import type { NativeTextFieldProps } from "./NativeTextField.types";
import { useTheme, type OpenCuriaTheme } from "./theme";

const TRANSPARENT = "#00000000";
const RADIUS = 12;

/** Ink outlined field: border and label ink when focused, neutral otherwise. */
export function outlinedFieldColors(theme: OpenCuriaTheme): TextFieldColors {
  return {
    focusedTextColor: theme.text,
    unfocusedTextColor: theme.text,
    disabledTextColor: theme.subtleText,
    focusedContainerColor: TRANSPARENT,
    unfocusedContainerColor: TRANSPARENT,
    disabledContainerColor: TRANSPARENT,
    cursorColor: theme.text,
    errorCursorColor: theme.danger,
    focusedIndicatorColor: theme.text,
    unfocusedIndicatorColor: theme.separator,
    disabledIndicatorColor: theme.separator,
    errorIndicatorColor: theme.danger,
    focusedLabelColor: theme.text,
    unfocusedLabelColor: theme.mutedText,
    disabledLabelColor: theme.subtleText,
    errorLabelColor: theme.danger,
    focusedPlaceholderColor: theme.subtleText,
    unfocusedPlaceholderColor: theme.subtleText,
    focusedLeadingIconColor: theme.mutedText,
    unfocusedLeadingIconColor: theme.mutedText,
    focusedTrailingIconColor: theme.mutedText,
    unfocusedTrailingIconColor: theme.mutedText,
    disabledTrailingIconColor: theme.subtleText,
  };
}

/** Rounded 12dp outline shared by text and picker fields. */
export function outlinedFieldShape(): ShapeJSXElement {
  return Shape.RoundedCorner({
    cornerRadii: {
      topStart: RADIUS,
      topEnd: RADIUS,
      bottomStart: RADIUS,
      bottomEnd: RADIUS,
    },
  });
}

/** Ink caret handles over a neutral highlight instead of the seed color. */
export function fieldSelectionColors(theme: OpenCuriaTheme): {
  handleColor: string;
  backgroundColor: string;
} {
  return { handleColor: theme.text, backgroundColor: theme.separator };
}

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
      seedColor={theme.seed}
      ignoreSafeAreaKeyboardInsets
    >
      <OutlinedTextField
        ref={field}
        autoFocus={props.autoFocus}
        value={text}
        singleLine
        shape={outlinedFieldShape()}
        colors={outlinedFieldColors(theme)}
        textSelectionColors={fieldSelectionColors(theme)}
        visualTransformation={props.secureTextEntry ? "password" : "none"}
        onValueChange={(value) => {
          latest.current = value;
          props.onChangeText(value);
        }}
        onFocusChanged={(focused) => {
          if (focused) props.onFocus?.();
        }}
        keyboardOptions={{
          capitalization: props.prose ? "sentences" : "none",
          autoCorrectEnabled: Boolean(props.prose),
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
