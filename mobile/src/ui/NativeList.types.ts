import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";

export interface NativeListRow {
  id: string;
  title: string;
  subtitle?: string;
  symbol?: NativeSymbol;
  selected?: boolean;
  busy?: boolean;
  unread?: boolean;
  disabled?: boolean;
  onPress?: () => void;
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  testID?: string;
}

export interface NativeListSection {
  id: string;
  title?: string;
  onPress?: () => void;
  selected?: boolean;
  busy?: boolean;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  rows: NativeListRow[];
}

export interface NativeListProps {
  sections: NativeListSection[];
  sidebar?: boolean;
}
