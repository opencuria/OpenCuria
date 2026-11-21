export interface NativePickerProps {
  label: string;
  value: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  onChange: (value: string) => void;
  disabled?: boolean;
  testID?: string;
}

export interface NativeSegmentedPickerProps {
  /** Spoken name of the whole control, e.g. "Mode". */
  label: string;
  value: string;
  options: Array<{
    value: string;
    label: string;
    /** Spoken label when the visible one is abbreviated. */
    accessibilityLabel?: string;
  }>;
  onChange: (value: string) => void;
  testID?: string;
}

export interface NativeOptionProps {
  label: string;
  description?: string;
  selected: boolean;
  multiple?: boolean;
  disabled?: boolean;
  onPress: () => void;
}

/** Geometry of a `NativeOption` row, for rows that line up with it. */
export interface OptionRowMetrics {
  /** From the row's leading edge to its indicator. */
  inset: number;
  /** Width of the radio or checkbox indicator. */
  indicator: number;
  /** Between the indicator and the label. */
  gap: number;
  /** Height of a row with a one-line label. */
  minHeight: number;
  /** The label's text size and line height. */
  fontSize: number;
  lineHeight: number;
}
