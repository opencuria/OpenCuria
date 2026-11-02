import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";

/** Semantic colors a row may use; each platform maps them to its palette. */
export type NativeFormTone =
  | "default"
  | "muted"
  | "accent"
  | "success"
  | "warning"
  | "danger";

/** A swipe (iOS) or overflow (Android) action of an item row. */
export interface NativeFormSwipeAction {
  id: string;
  title: string;
  symbol: NativeSymbol;
  tone?: NativeFormTone;
  destructive?: boolean;
}

/** A labelled icon button in a header row (liquid glass on iOS 26). */
export interface NativeFormQuickAction {
  id: string;
  title: string;
  symbol: NativeSymbol;
  disabled?: boolean;
  destructive?: boolean;
}

export interface NativeFormOptionGroup {
  title?: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
}

interface RowBase {
  id: string;
  testID?: string;
  disabled?: boolean;
}

/** A tappable list entry with a status tile, subtitle, menu and swipe actions. */
export interface NativeFormItemRow extends RowBase {
  kind: "item";
  title: string;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  symbol?: NativeSymbol;
  /** Color of the leading tile. */
  tint?: string;
  selected?: boolean;
  busy?: boolean;
  /** Short trailing hint, e.g. "Update". */
  badge?: string;
  onPress?: () => void;
  /** Trailing detail button (ⓘ). */
  onInfo?: () => void;
  infoLabel?: string;
  actions?: NativeMenuAction[];
  leadingSwipe?: NativeFormSwipeAction[];
  trailingSwipe?: NativeFormSwipeAction[];
  onAction?: (id: string) => void;
}

/** Hero header of a detail screen with its quick actions. */
export interface NativeFormHeaderRow extends RowBase {
  kind: "header";
  title: string;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  symbol: NativeSymbol;
  tint: string;
  busy?: boolean;
  actions: NativeFormQuickAction[];
  onAction: (id: string) => void;
}

export interface NativeFormTextRow extends RowBase {
  kind: "text";
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  keyboard?: "default" | "url";
  multiline?: boolean;
  autoFocus?: boolean;
  onSubmit?: () => void;
}

export interface NativeFormToggleRow extends RowBase {
  kind: "toggle";
  label: string;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  value: boolean;
  onChange: (value: boolean) => void;
}

export interface NativeFormPickerRow extends RowBase {
  kind: "picker";
  label: string;
  value: string;
  groups: NativeFormOptionGroup[];
  onChange: (value: string) => void;
}

export interface NativeFormStepperRow extends RowBase {
  kind: "stepper";
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (value: number) => string;
  onChange: (value: number) => void;
}

export interface NativeFormValueRow extends RowBase {
  kind: "value";
  label: string;
  value: string;
  tone?: NativeFormTone;
}

/** Linear capacity meter, e.g. storage in use. */
export interface NativeFormGaugeRow extends RowBase {
  kind: "gauge";
  label: string;
  /** Share in use, 0–1. */
  value: number;
  valueLabel: string;
  tone?: NativeFormTone;
}

export interface NativeFormButtonRow extends RowBase {
  kind: "button";
  title: string;
  symbol?: NativeSymbol;
  destructive?: boolean;
  busy?: boolean;
  onPress: () => void;
}

export interface NativeFormNoteRow extends RowBase {
  kind: "note";
  text: string;
  tone?: NativeFormTone;
}

export type NativeFormRow =
  | NativeFormItemRow
  | NativeFormHeaderRow
  | NativeFormTextRow
  | NativeFormToggleRow
  | NativeFormPickerRow
  | NativeFormStepperRow
  | NativeFormValueRow
  | NativeFormGaugeRow
  | NativeFormButtonRow
  | NativeFormNoteRow;

export interface NativeFormSection {
  id: string;
  title?: string;
  footer?: string;
  footerTone?: NativeFormTone;
  rows: NativeFormRow[];
}

export interface NativeFormProps {
  sections: NativeFormSection[];
  /** Pull to refresh. */
  onRefresh?: () => Promise<void>;
  /** Shown instead of the sections when there are none. */
  empty?: {
    title: string;
    description?: string;
    symbol: NativeSymbol;
  };
  /** Extra space below the last section for a floating action button. */
  contentInsetBottom?: number;
  testID?: string;
}
