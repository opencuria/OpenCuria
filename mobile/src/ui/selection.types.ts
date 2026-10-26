export interface NativePickerProps {
  label: string;
  value: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  onChange: (value: string) => void;
  disabled?: boolean;
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
