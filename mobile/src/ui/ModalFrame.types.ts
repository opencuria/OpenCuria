import type React from "react";
import type { NativeSymbol } from "./controls.types";
import type { NativeFormProps } from "./NativeForm.types";
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
  /** Close (×) always sits at the leading edge, the main action trailing. */
  onClose?: () => void;
  /**
   * A large, bold title that collapses while scrolling, for top-level
   * collection sheets (Workspaces, Apps, Scheduled tasks). Editors and
   * viewers keep the inline title.
   */
  largeTitle?: boolean;
  /** Overflow ("…") menu at the trailing edge. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  /** Rendered in the iOS toolbar; Android shows it below the content. */
  primaryAction?: ModalPrimaryAction;
  search?: ModalSearch;
  /**
   * iOS: a form rendered natively inside the frame's navigation stack in
   * place of `children`, so the large title collapses and the bar's scroll
   * edge effect follows the list.
   */
  form?: NativeFormProps;
  children: React.ReactNode;
}
