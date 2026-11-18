import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useColorScheme } from "react-native";
import * as SecureStore from "expo-secure-store";

/** User preference for resolving the native appearance. */
export type ThemeMode = "system" | "light" | "dark";
export type ColorScheme = "light" | "dark";

/**
 * Semantic colors of the app's one design language: ink on a white or black
 * canvas, as in the chat. Color only marks status (see `statusColors.ts`) and
 * destructive actions; the accent (`primary`) is the ink itself.
 */
export interface OpenCuriaTheme {
  scheme: ColorScheme;
  /** Canvas of the chat and of every sheet and page. */
  background: string;
  surface: string;
  raised: string;
  surfaceMuted: string;
  /** Grouped rows and cards on the canvas. */
  card: string;
  /** Hairline between rows of a card. */
  separator: string;
  /** Navigation revealed beneath the chat when the sidebar slides open. */
  sidebar: string;
  userBubble: string;
  composer: string;
  /** Translucent fill for secondary controls placed on the composer surface. */
  controlFill: string;
  disabledAction: string;
  text: string;
  mutedText: string;
  subtleText: string;
  border: string;
  /** Ink for the one prominent action, selection and control tint. */
  primary: string;
  /** Label on a `primary` fill. */
  primaryForeground: string;
  /**
   * Seed of the Material palette that Compose hosts generate on Android.
   * Components there set explicit ink colors; the seed only keeps the
   * generated palette stable for system details such as ripples.
   */
  seed: string;
  brand: string;
  danger: string;
  dangerSurface: string;
  success: string;
  warning: string;
  overlay: string;
  codeBackground: string;
}

const THEMES: Record<ColorScheme, OpenCuriaTheme> = {
  light: {
    scheme: "light",
    background: "#FFFFFF",
    surface: "#FFFFFF",
    raised: "#FFFFFF",
    surfaceMuted: "#F2F2F7",
    card: "#F2F2F7",
    separator: "#D8D8DC",
    sidebar: "#F4F4F5",
    userBubble: "#E9E9EB",
    composer: "#FFFFFF",
    controlFill: "rgba(0, 0, 0, 0.06)",
    disabledAction: "#E7E7E7",
    text: "#0D0D0D",
    mutedText: "#606060",
    subtleText: "#909090",
    border: "#E4E4E4",
    primary: "#0D0D0D",
    primaryForeground: "#FFFFFF",
    seed: "#007AFF",
    brand: "#534AB7",
    danger: "#B42318",
    dangerSurface: "#FEF3F2",
    success: "#18794E",
    warning: "#8A5A00",
    overlay: "rgba(15, 15, 20, 0.42)",
    codeBackground: "#F3F3F3",
  },
  dark: {
    scheme: "dark",
    background: "#000000",
    surface: "#1C1C1E",
    raised: "#303030",
    surfaceMuted: "#262626",
    card: "#1C1C1E",
    separator: "#38383A",
    sidebar: "#151517",
    userBubble: "#2C2C2E",
    composer: "#303030",
    controlFill: "rgba(255, 255, 255, 0.09)",
    disabledAction: "#444444",
    text: "#F5F5F5",
    mutedText: "#B4B4B4",
    subtleText: "#969696",
    border: "#414141",
    primary: "#F5F5F5",
    primaryForeground: "#000000",
    seed: "#0A84FF",
    brand: "#A49BFF",
    danger: "#FF8A80",
    dangerSurface: "#351D20",
    success: "#79D5A6",
    warning: "#F2C66D",
    overlay: "rgba(0, 0, 0, 0.68)",
    codeBackground: "#202020",
  },
};

interface ThemeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  theme: OpenCuriaTheme;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);
const APPEARANCE_KEY = "opencuria.appearance.v1";

function isThemeMode(value: unknown): value is ThemeMode {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * Provides the chat's light, dark, and system theme. Place once near the mobile
 * app root; the selected mode is controlled by this provider, which (with
 * `persist`) restores the user's Appearance choice on the next launch.
 */
export function ThemeProvider({
  children,
  initialMode = "system",
  persist = false,
}: React.PropsWithChildren<{
  initialMode?: ThemeMode;
  /** Remember the Appearance setting on this device. */
  persist?: boolean;
}>): React.JSX.Element {
  const systemScheme = useColorScheme();
  const [mode, setModeState] = useState<ThemeMode>(initialMode);
  useEffect(() => {
    if (!persist) return;
    let active = true;
    SecureStore.getItemAsync(APPEARANCE_KEY)
      .then((saved) => {
        if (active && isThemeMode(saved)) setModeState(saved);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [persist]);
  const setMode = useCallback(
    (next: ThemeMode) => {
      setModeState(next);
      if (persist)
        void SecureStore.setItemAsync(APPEARANCE_KEY, next).catch(
          () => undefined,
        );
    },
    [persist],
  );
  const scheme: ColorScheme =
    mode === "system" ? (systemScheme === "dark" ? "dark" : "light") : mode;
  const value = useMemo<ThemeContextValue>(
    () => ({ mode, setMode, theme: THEMES[scheme] }),
    [mode, scheme, setMode],
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
