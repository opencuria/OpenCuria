import type React from "react";
import type { NativeSymbol } from "./controls.types";

export interface NativeTextFieldHandle {
  focus: () => void;
}

/** A native button at the trailing edge inside the field, e.g. reveal password. */
export interface NativeTextFieldAction {
  symbol: NativeSymbol;
  label: string;
  onPress: () => void;
  testID?: string;
}

export interface NativeTextFieldProps {
  ref?: React.Ref<NativeTextFieldHandle>;
  label: string;
  accessibilityLabel?: string;
  value: string;
  onChangeText: (value: string) => void;
  placeholder?: string;
  secureTextEntry?: boolean;
  /** Free text such as answers: autocorrection and sentence capitalization. */
  prose?: boolean;
  keyboardType?: "email-address" | "url" | "default";
  returnKeyType?: "next" | "go" | "done";
  onSubmitEditing?: () => void;
  onFocus?: () => void;
  autoFocus?: boolean;
  /**
   * A row of a `FieldGroup`. iOS draws no border and shows the label as the
   * placeholder; Android keeps its outlined field.
   */
  grouped?: boolean;
  trailingAction?: NativeTextFieldAction;
  testID?: string;
}
