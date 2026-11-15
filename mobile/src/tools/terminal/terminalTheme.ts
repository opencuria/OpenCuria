// Palette adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-terminal/LICENSE.
import type { OpenCuriaTheme } from "../../ui/theme";

export interface TerminalTheme {
  readonly background: string;
  readonly foreground: string;
  readonly mutedForeground: string;
  readonly border: string;
  readonly cursorForeground: string;
  readonly cursorBackground: string;
  readonly palette: readonly string[];
}

const LIGHT_PALETTE = [
  "#1F1F21",
  "#e0001b",
  "#0a9a3f",
  "#b58900",
  "#007ad6",
  "#b02ad1",
  "#0598bd",
  "#8e8e95",
  "#5c5c62",
  "#ff2e3f",
  "#0dbe4e",
  "#d9a800",
  "#009fff",
  "#c635e4",
  "#08c0ef",
  "#c6c6c8",
] as const;
const DARK_PALETTE = [
  "#141415",
  "#ff2e3f",
  "#0dbe4e",
  "#ffca00",
  "#009fff",
  "#c635e4",
  "#08c0ef",
  "#c6c6c8",
  "#5c5c62",
  "#ff5c69",
  "#3fd673",
  "#ffd84d",
  "#4db8ff",
  "#d76bed",
  "#4fd3f5",
  "#ffffff",
] as const;

/** Terminal colors on the app's own surfaces (Pierre ANSI palette). */
export function terminalThemeFor(theme: OpenCuriaTheme): TerminalTheme {
  const dark = theme.scheme === "dark";
  const background = dark ? "#0a0a0a" : "#fcfcfc";
  return {
    background,
    foreground: dark ? "#d4d4d8" : "#1F1F21",
    mutedForeground: theme.mutedText,
    border: theme.border,
    cursorForeground: theme.primary,
    cursorBackground: background,
    palette: dark ? DARK_PALETTE : LIGHT_PALETTE,
  };
}

export function buildGhosttyThemeConfig(theme: TerminalTheme): string {
  const lines = [
    `background = ${theme.background}`,
    `foreground = ${theme.foreground}`,
    `cursor-color = ${theme.cursorForeground}`,
    `cursor-text = ${theme.cursorBackground}`,
  ];
  for (const [index, color] of theme.palette.entries()) {
    lines.push(`palette = ${index}=${color}`);
  }
  return `${lines.join("\n")}\n`;
}
