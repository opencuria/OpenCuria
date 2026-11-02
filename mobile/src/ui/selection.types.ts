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
