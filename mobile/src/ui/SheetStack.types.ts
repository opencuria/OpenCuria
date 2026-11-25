import type React from "react";
import type { LucideIcon } from "lucide-react-native";
import type { NativeSymbol } from "./controls.types";
import type { NativeMenuAction } from "./NativeMenu";

/** A trailing button of a page's navigation bar. */
export interface SheetPageAction {
  id: string;
  label: string;
  accessibilityHint?: string;
  /** SF Symbol on iOS. */
  symbol: NativeSymbol;
  /** Icon drawn on Android. */
  icon: LucideIcon;
  /** A status color for the symbol, e.g. the recording red. */
  tint?: string;
  disabled?: boolean;
  /** Its own glass capsule instead of the group of the other items (iOS 26). */
  standalone?: boolean;
  onPress: () => void;
  testID?: string;
}

/** Actions of the overflow menu, divided from the next group by a separator. */
export interface SheetMenuSection {
  id: string;
  /** Heading above the group, e.g. for a set of toggles. */
  title?: string;
  actions: NativeMenuAction[];
}

/** The overflow ("…") menu at the trailing edge of a page's bar. */
export interface SheetPageMenu {
  label: string;
  sections: SheetMenuSection[];
  onSelect: (id: string) => void;
  testID?: string;
}

export interface SheetPage {
  title: string;
  /** Quieter second line, e.g. the workspace. */
  subtitle?: string;
  actions?: SheetPageAction[];
  menu?: SheetPageMenu;
  /** Scrolls beneath the translucent bar; it pads by `useChromeTopInset`. */
  children: React.ReactNode;
  testID?: string;
}

export interface SheetStackProps {
  /** The first page, e.g. a chat list. */
  root: SheetPage;
  /** Pushed onto the root, e.g. a chat; its bar leads back to the root. */
  detail?: SheetPage;
  detailShown: boolean;
  /** The user went back (button, edge swipe) or the stack showed `detail`. */
  onDetailShownChange: (shown: boolean) => void;
  /** Height of the navigation bar, e.g. for the sheet's drag area. */
  onBarHeight?: (height: number) => void;
}
