import { createContext, useContext } from "react";

/**
 * Height of translucent navigation chrome drawn above the chat. Content keeps
 * scrolling beneath it, so scrollables pad their first item by this inset.
 */
export const ChromeInsetContext = createContext(0);

/** Top inset of the native header covering the current screen; 0 when opaque. */
export function useChromeTopInset(): number {
  return useContext(ChromeInsetContext);
}
