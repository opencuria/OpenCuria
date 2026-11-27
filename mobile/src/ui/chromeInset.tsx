import { createContext, useContext } from "react";
import { makeMutable, type DerivedValue } from "react-native-reanimated";

/**
 * Height of translucent navigation chrome drawn above the chat. Content keeps
 * scrolling beneath it, so scrollables pad their first item by this inset.
 */
export const ChromeInsetContext = createContext(0);

/** Top inset of the native header covering the current screen; 0 when opaque. */
export function useChromeTopInset(): number {
  return useContext(ChromeInsetContext);
}

/**
 * How far the screen's layout reaches below the visible area, e.g. while a
 * sheet laid out at its full height rests lower. It changes every frame the
 * sheet moves; the docked composer rises by it and the transcript ends above
 * it, so nothing is laid out again.
 */
export const HiddenBottomContext = createContext<DerivedValue<number> | null>(
  null,
);

const NOTHING_HIDDEN = makeMutable(0);

/** The layout's part below the visible area (UI thread); 0 outside sheets. */
export function useHiddenBottom(): DerivedValue<number> {
  return useContext(HiddenBottomContext) ?? NOTHING_HIDDEN;
}
