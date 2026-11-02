import type React from "react";
import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";

/** The screen's main action, e.g. "Create" or "New workspace". */
export interface ModalPrimaryAction {
  title: string;
  onPress: () => void;
  /**
   * An icon action (e.g. "+") instead of a confirmation: an icon toolbar
   * button on iOS, an extended floating action button on Android.
   */
  symbol?: NativeSymbol;
  disabled?: boolean;
  busy?: boolean;
  testID?: string;
}

/** Native search: navigation-bar search on iOS, a docked search bar on Android. */
export interface ModalSearch {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}

export interface ModalFrameProps {
  title: string;
  /** Second header line, e.g. the folder of an opened file. */
  subtitle?: string;
  onClose?: () => void;
  /** Close sits trailing (confirmation) by default, leading for viewers. */
  closePlacement?: "leading" | "trailing";
  /** Overflow ("…") menu at the trailing edge. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  /** Rendered in the iOS toolbar; Android shows it below the content. */
  primaryAction?: ModalPrimaryAction;
  search?: ModalSearch;
  children: React.ReactNode;
}
