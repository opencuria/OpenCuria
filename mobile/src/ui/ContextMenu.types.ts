import type React from "react";
import type { NativeMenuAction } from "./NativeMenu";

export interface NativeContextMenuProps {
  /** Menu title on iOS; names the content for assistive technology. */
  label: string;
  actions: NativeMenuAction[];
  onSelect: (id: string) => void;
  /**
   * Renders the tappable content. Android passes `showMenu` for its long
   * press; iOS presents the menu on long press itself.
   */
  children: (showMenu?: () => void) => React.ReactElement;
}
