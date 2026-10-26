import type React from "react";

export interface NativeTextFieldHandle {
  focus: () => void;
}

export interface NativeTextFieldProps {
  ref?: React.Ref<NativeTextFieldHandle>;
  label: string;
  accessibilityLabel?: string;
  value: string;
  onChangeText: (value: string) => void;
  placeholder?: string;
  secureTextEntry?: boolean;
  keyboardType?: "email-address" | "url" | "default";
  returnKeyType?: "next" | "go" | "done";
  onSubmitEditing?: () => void;
  onFocus?: () => void;
  autoFocus?: boolean;
  testID?: string;
}
