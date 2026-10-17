import React, { createContext, useContext, useMemo, useState } from "react";
import { useColorScheme } from "react-native";

/** User preference for resolving the native appearance. */
export type ThemeMode = "system" | "light" | "dark";
export type ColorScheme = "light" | "dark";

/** Semantic colors used by the mobile chat UI. Keep surfaces neutral and actions violet. */
export interface OpenCuriaTheme {
  scheme: ColorScheme;
  background: string;
  surface: string;
  raised: string;
  surfaceMuted: string;
  text: string;
  mutedText: string;
  subtleText: string;
  border: string;
  primary: string;
  primaryForeground: string;
  brand: string;
  danger: string;
  dangerSurface: string;
  success: string;
  warning: string;
  info: string;
  overlay: string;
  codeBackground: string;
}

const THEMES: Record<ColorScheme, OpenCuriaTheme> = {
  light: {
    scheme: "light",
    background: "#FFFFFF",
    surface: "#FFFFFF",
    raised: "#FFFFFF",
    surfaceMuted: "#F4F4F6",
    text: "#17171B",
    mutedText: "#64646F",
    subtleText: "#858590",
    border: "#E7E7EB",
    primary: "#534AB7",
    primaryForeground: "#FFFFFF",
    brand: "#534AB7",
    danger: "#B42318",
    dangerSurface: "#FEF3F2",
    success: "#18794E",
    warning: "#8A5A00",
    info: "#3159A5",
    overlay: "rgba(15, 15, 20, 0.42)",
    codeBackground: "#F2F2F5",
  },
  dark: {
    scheme: "dark",
    background: "#101014",
    surface: "#17171C",
    raised: "#202027",
    surfaceMuted: "#25252D",
    text: "#F4F4F6",
    mutedText: "#B1B1BC",
    subtleText: "#8B8B97",
    border: "#34343E",
    primary: "#A49BFF",
    primaryForeground: "#171522",
    brand: "#A49BFF",
    danger: "#FF8A80",
    dangerSurface: "#351D20",
    success: "#79D5A6",
    warning: "#F2C66D",
    info: "#9BB9FF",
    overlay: "rgba(0, 0, 0, 0.68)",
    codeBackground: "#222229",
  },
};

interface ThemeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  theme: OpenCuriaTheme;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * Provides the chat's light, dark, and system theme. Place once near the mobile
 * app root; the selected mode is intentionally controlled by this provider,
 * not persisted by a presentation component.
 */
export function ThemeProvider({
  children,
  initialMode = "system",
}: React.PropsWithChildren<{ initialMode?: ThemeMode }>): React.JSX.Element {
  const systemScheme = useColorScheme();
  const [mode, setMode] = useState<ThemeMode>(initialMode);
  const scheme: ColorScheme =
    mode === "system" ? (systemScheme === "dark" ? "dark" : "light") : mode;
  const value = useMemo<ThemeContextValue>(
    () => ({ mode, setMode, theme: THEMES[scheme] }),
    [mode, scheme],
  );
  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

/** Read the active semantic palette and theme preference setter. */
export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used inside an OpenCuria ThemeProvider.");
  }
  return context;
}

/** Allows lightweight theme-aware components outside the provider in stories/tests. */
export function getTheme(scheme: ColorScheme): OpenCuriaTheme {
  return THEMES[scheme];
}
