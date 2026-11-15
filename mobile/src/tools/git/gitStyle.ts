import type { GitFileStatus } from "@opencuria/chat-core/gitTypes";
import type { OpenCuriaTheme } from "../../ui/theme";

/** Lane colours of the webapp graph (`--git-branch-1` … `--git-branch-8`). */
const BRANCH_COLORS = {
  light: [
    "#534ab7",
    "#b45309",
    "#1d4ed8",
    "#be185d",
    "#0f766e",
    "#b91c1c",
    "#4d7c0f",
    "#7e22ce",
  ],
  dark: [
    "#8b85e8",
    "#fbbf24",
    "#60a5fa",
    "#f472b6",
    "#2dd4bf",
    "#f87171",
    "#a3e635",
    "#c084fc",
  ],
} as const;

export function branchColor(theme: OpenCuriaTheme, colour: number): string {
  const palette = BRANCH_COLORS[theme.scheme];
  return palette[colour % palette.length]!;
}

/** Text on a filled (current) branch badge. */
export function branchActiveForeground(theme: OpenCuriaTheme): string {
  return theme.scheme === "dark" ? "#18181b" : "#ffffff";
}

/** Status letter colours of the webapp change rows. */
export function statusColor(
  theme: OpenCuriaTheme,
  status: GitFileStatus,
): string {
  const dark = theme.scheme === "dark";
  switch (status) {
    case "A":
      return dark ? "#4ade80" : "#15803d";
    case "D":
      return dark ? "#f87171" : "#dc2626";
    case "M":
      return dark ? "#fbbf24" : "#b45309";
    case "R":
    case "C":
      return "#a855f7";
    default:
      return theme.mutedText;
  }
}

/** Diff row backgrounds. */
export function diffColors(theme: OpenCuriaTheme) {
  const dark = theme.scheme === "dark";
  return {
    addBackground: dark
      ? "rgba(74, 222, 128, 0.14)"
      : "rgba(21, 128, 61, 0.10)",
    delBackground: dark
      ? "rgba(248, 113, 113, 0.14)"
      : "rgba(220, 38, 38, 0.09)",
    addMarker: dark ? "#4ade80" : "#15803d",
    delMarker: dark ? "#f87171" : "#dc2626",
  };
}

export function fileName(path: string): string {
  return path.split("/").pop() ?? path;
}

export function dirName(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "";
}
