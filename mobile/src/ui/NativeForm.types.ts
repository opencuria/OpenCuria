import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";
import type { StatusTone } from "./statusColors";

/**
 * Semantic colors a row may use; each platform maps them to its palette.
 * `accent` is the ink (same as `default`), kept for emphasis in data.
 */
export type NativeFormTone =
  | "default"
  | "muted"
  | "accent"
  | "success"
  | "warning"
  | "danger";

/**
 * Status of an entity, drawn as a small colored dot (rows, headers) or as
 * the color of an outcome symbol: running, needs attention, failed, idle.
 * Always paired with a text label; color is never the only signal.
 */
export type NativeFormStatus = StatusTone;

/**
 * A trailing capsule button on an item row, as in the App Store ("Open",
 * "Update"). `prominent` fills it with ink for the action that changes state.
 */
export interface NativeFormAccessory {
  title: string;
  onPress: () => void;
  prominent?: boolean;
  busy?: boolean;
  disabled?: boolean;
  accessibilityLabel?: string;
  testID?: string;
}

/**
 * The centered state shown instead of the sections: empty, no results or a
 * load failure, with an optional action such as "Retry" or "New workspace".
 */
export interface NativeFormEmptyState {
  title: string;
  description?: string;
  symbol: NativeSymbol;
  action?: {
    title: string;
    onPress: () => void;
    symbol?: NativeSymbol;
    busy?: boolean;
    testID?: string;
  };
}

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

/**
 * A tappable list entry: a leading symbol, tile, image or time, title and
 * subtitle (optionally led by a status dot), trailing accessories, a context
 * menu and swipe actions.
 */
export interface NativeFormItemRow extends RowBase {
  kind: "item";
  title: string;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  /** Colored dot before the subtitle, e.g. a workspace that is running. */
  status?: NativeFormStatus;
  /** Leading symbol: plain ink, or on a neutral tile with `tile`. */
  symbol?: NativeSymbol;
  /** Colors a plain leading symbol by status, e.g. a failed run. */
  symbolStatus?: NativeFormStatus;
  /**
   * Places the symbol on a neutral rounded tile, for entities with an
   * identity such as workspaces and apps. Implied by `image`.
   */
  tile?: boolean;
  /**
   * A small local image (`file://`) that fills the leading tile in place of
   * the symbol, e.g. an app icon. Native lists read it while they draw, so
   * keep it to a few KB.
   */
  image?: string;
  /**
   * Short leading text in place of the tile, e.g. the time of an agenda
   * entry, in tabular figures so that a column of them aligns.
   */
  leadingText?: string;
  /** Smaller text after the leading text, e.g. "AM". */
  leadingCaption?: string;
  leadingTone?: NativeFormTone;
  selected?: boolean;
  busy?: boolean;
  /** Short trailing chip, e.g. "Update". */
  badge?: string;
  /** Trailing capsule button; replaces the badge. */
  accessory?: NativeFormAccessory;
  onPress?: () => void;
  /** Trailing detail button (ⓘ). */
  onInfo?: () => void;
  infoLabel?: string;
  actions?: NativeMenuAction[];
  leadingSwipe?: NativeFormSwipeAction[];
  trailingSwipe?: NativeFormSwipeAction[];
  onAction?: (id: string) => void;
}

/**
 * Hero header of a detail screen: a neutral tile, the name, a status line
 * and liquid-glass (iOS) or tonal (Android) quick actions.
 */
export interface NativeFormHeaderRow extends RowBase {
  kind: "header";
  title: string;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  /** Colored dot before the subtitle. */
  status?: NativeFormStatus;
  symbol: NativeSymbol;
  /** A small local image filling the tile in place of the symbol. */
  image?: string;
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
  keyboard?: "default" | "url" | "email" | "number";
  /** Masks the value, e.g. an API key or password. */
  secure?: boolean;
  multiline?: boolean;
  /** Visible lines of a multiline field: [minimum, maximum]. */
  lines?: [number, number];
  /** Natural-language text such as a prompt: sentence case and autocorrection. */
  prose?: boolean;
  /** Code-like text such as a cron expression, set in a monospaced font. */
  monospace?: boolean;
  autoFocus?: boolean;
  onSubmit?: () => void;
}

export interface NativeFormToggleRow extends RowBase {
  kind: "toggle";
  label: string;
  symbol?: NativeSymbol;
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  value: boolean;
  onChange: (value: boolean) => void;
}

export interface NativeFormPickerRow extends RowBase {
  kind: "picker";
  label: string;
  symbol?: NativeSymbol;
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
  symbol?: NativeSymbol;
  /** Shows the value wrapped below the label, e.g. an email address. */
  stacked?: boolean;
  /** Code-like values such as tokens: stacked, monospaced and selectable. */
  monospace?: boolean;
  /** Turns the row into a navigation row (chevron) that opens a choice. */
  onPress?: () => void;
}

/** A few mutually exclusive, always visible choices. */
export interface NativeFormSegmentedRow extends RowBase {
  kind: "segmented";
  /** Spoken name of the control, e.g. "Repeat". */
  label: string;
  value: string;
  options: Array<{
    value: string;
    label: string;
    /** Spoken label when the visible one is abbreviated. */
    accessibilityLabel?: string;
  }>;
  onChange: (value: string) => void;
}

/** Days of the week as round toggles, Monday first. */
export interface NativeFormWeekdaysRow extends RowBase {
  kind: "weekdays";
  label: string;
  /** Short summary of the selection, e.g. "Weekdays". */
  summary?: string;
  /** Selected days, 0 = Monday … 6 = Sunday. */
  value: number[];
  onChange: (value: number[]) => void;
}

/** A native date or time picker for a wall-clock value. */
export interface NativeFormDateRow extends RowBase {
  kind: "date";
  label: string;
  mode: "date" | "time";
  /** YYYY-MM-DD for `date`, HH:MM for `time`. */
  value: string;
  onChange: (value: string) => void;
  /** Earliest selectable YYYY-MM-DD of a `date` picker. */
  minimumDate?: string;
  /** Removes this entry from a list, e.g. one of several run times. */
  onRemove?: () => void;
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

/**
 * A row that pushes another settings page, as in the Settings app: plain
 * leading symbol, title, optional second line, trailing value and chevron.
 * Only valid inside the settings navigator.
 */
export interface NativeFormLinkRow extends RowBase {
  kind: "link";
  title: string;
  /** Second line below the title, e.g. an email address. */
  subtitle?: string;
  subtitleTone?: NativeFormTone;
  /** Short trailing value, e.g. "Admin" or "3". */
  value?: string;
  valueTone?: NativeFormTone;
  symbol?: NativeSymbol;
  busy?: boolean;
  /** Route of the pushed page. */
  route: string;
}

/**
 * Large centered avatar with the user's initials and name, as in ChatGPT:
 * ink initials on a neutral circle, like the sidebar's account button.
 */
export interface NativeFormProfileRow extends RowBase {
  kind: "profile";
  initials: string;
  title: string;
  subtitle?: string;
}

/** A highlighted card with a capsule action or menu, e.g. the organization. */
export interface NativeFormCardRow extends RowBase {
  kind: "card";
  title: string;
  subtitle?: string;
  actionTitle: string;
  /** A plain action; without it the capsule opens `actions` as a menu. */
  onAction?: () => void;
  actions?: NativeMenuAction[];
  onSelect?: (id: string) => void;
}

/**
 * Explanatory text inside a section, e.g. an offline notice or an inline
 * error. `busy` leads it with a small spinner for section-level loading
 * ("Loading runs…"); `symbol` leads it with a glyph in the tone's color.
 */
export interface NativeFormNoteRow extends RowBase {
  kind: "note";
  text: string;
  tone?: NativeFormTone;
  symbol?: NativeSymbol;
  busy?: boolean;
}

export type NativeFormRow =
  | NativeFormItemRow
  | NativeFormHeaderRow
  | NativeFormLinkRow
  | NativeFormProfileRow
  | NativeFormCardRow
  | NativeFormTextRow
  | NativeFormToggleRow
  | NativeFormPickerRow
  | NativeFormStepperRow
  | NativeFormValueRow
  | NativeFormSegmentedRow
  | NativeFormWeekdaysRow
  | NativeFormDateRow
  | NativeFormGaugeRow
  | NativeFormButtonRow
  | NativeFormNoteRow;

/**
 * A group of rows on one card. Every title renders as a prominent,
 * sentence-case header (ink, bold), the same on every sheet and page.
 */
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
  empty?: NativeFormEmptyState;
  /**
   * The first load is still running: a centered progress indicator replaces
   * the empty state while there are no sections.
   */
  loading?: boolean;
  /** Extra space below the last section for a floating action button. */
  contentInsetBottom?: number;
  testID?: string;
}
