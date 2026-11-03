import type { OpenCuriaTheme } from "./theme";

export type TileColor = "green" | "orange" | "red" | "gray" | "indigo";

const TILE_COLORS: Record<
  TileColor,
  Record<OpenCuriaTheme["scheme"], string>
> = {
  green: { light: "#34C759", dark: "#30D158" },
  orange: { light: "#FF9500", dark: "#FF9F0A" },
  red: { light: "#FF3B30", dark: "#FF453A" },
  gray: { light: "#8E8E93", dark: "#636366" },
  indigo: { light: "#5856D6", dark: "#5E5CE6" },
};

/** System colors for status tiles, tuned per appearance as in UIKit. */
export function tileColor(color: TileColor, theme: OpenCuriaTheme): string {
  return TILE_COLORS[color][theme.scheme];
}
