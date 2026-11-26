import React, { createContext, useContext } from "react";
import { DynamicColorIOS, type ColorValue } from "react-native";
import {
  getTheme,
  useTheme,
  type ColorScheme,
  type OpenCuriaTheme,
} from "./theme";

const INK_KEYS = [
  "text",
  "mutedText",
  "subtleText",
  "background",
  "controlFill",
  "primary",
] as const satisfies ReadonlyArray<keyof OpenCuriaTheme>;
type InkKey = (typeof INK_KEYS)[number];

/** Colors for content on a surface and the scheme its native hosts use. */
export type Ink = Record<InkKey, ColorValue> & {
  /** Forced on SwiftUI hosts; undefined lets them follow the surface. */
  scheme: ColorScheme | undefined;
};

const AdaptiveInk = createContext(false);

/**
 * Liquid glass under 65 points tall (bars, the composer, toasts) switches
 * between its light and dark look with the content behind it, whatever the
 * theme (iOS 26). Glass surfaces wrap their content in this scope: `useInk`
 * then returns dynamic colors that UIKit resolves against the glass's look.
 * Only iOS glass may render it.
 */
export function AdaptiveInkScope({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  return <AdaptiveInk.Provider value>{children}</AdaptiveInk.Provider>;
}

let adaptive: Ink | undefined;

function adaptiveInk(): Ink {
  if (!adaptive) {
    const light = getTheme("light");
    const dark = getTheme("dark");
    adaptive = {
      ...(Object.fromEntries(
        INK_KEYS.map((key) => [
          key,
          DynamicColorIOS({ light: light[key], dark: dark[key] }),
        ]),
      ) as Record<InkKey, ColorValue>),
      scheme: undefined,
    };
  }
  return adaptive;
}

/** The theme's ink, or ink that follows the glass it is drawn on. */
export function useInk(): Ink {
  const { theme } = useTheme();
  return useContext(AdaptiveInk) ? adaptiveInk() : theme;
}
