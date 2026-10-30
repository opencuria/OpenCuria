import type React from "react";
import type { PressableProps } from "react-native";
import type { ButtonProps as SwiftButtonProps } from "@expo/ui/swift-ui";

export type NativeSymbol = NonNullable<SwiftButtonProps["systemImage"]>;

export interface NativeActionProps extends Omit<
  PressableProps,
  "children" | "onPress" | "onLongPress"
> {
  children?: React.ReactNode;
  onPress?: () => void;
  /** Size the native hit surface to its rich label instead of the parent width. */
  fitContent?: boolean;
}

export interface ButtonProps extends Omit<
  PressableProps,
  "children" | "onPress"
> {
  title: string;
  onPress?: () => void;
  symbol?: NativeSymbol;
  variant?: "primary" | "secondary" | "outline" | "danger" | "ghost";
  busy?: boolean;
  compact?: boolean;
}

export interface IconButtonProps extends Omit<
  PressableProps,
  "children" | "onPress"
> {
  icon?: React.ReactNode;
  symbol?: NativeSymbol;
  label: string;
  onPress?: () => void;
  size?: number;
  /**
   * `floating` sits above scrolling content: liquid glass on iOS 26, a neutral surface on Android.
   * `quiet` is a small, uncolored inline action such as transcript copy.
   */
  tone?: "neutral" | "primary" | "danger" | "floating" | "quiet";
}
