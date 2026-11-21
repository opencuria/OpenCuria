import type { OpenCuriaTheme } from "./theme";

/**
 * The only meanings color carries in lists and headers: running or
 * succeeded, needs attention, failed, and idle. Each tone has one color
 * everywhere (status dots, outcome symbols, swipe actions).
 */
export type StatusTone = "positive" | "attention" | "critical" | "neutral";

const STATUS_COLORS: Record<
  StatusTone,
  Record<OpenCuriaTheme["scheme"], string>
> = {
  positive: { light: "#34C759", dark: "#30D158" },
  attention: { light: "#FF9500", dark: "#FF9F0A" },
  critical: { light: "#FF3B30", dark: "#FF453A" },
  neutral: { light: "#8E8E93", dark: "#8E8E93" },
};

/** Vivid system color of a status dot or outcome symbol, per appearance. */
export function statusColor(tone: StatusTone, theme: OpenCuriaTheme): string {
  return STATUS_COLORS[tone][theme.scheme];
}

/** Semantic swipe or quick-action tones (NativeFormTone without `default`). */
export type ActionTone = "accent" | "muted" | "success" | "warning" | "danger";

/**
 * Background of a swipe action. Its label is always white, so the ink accent
 * stays dark in both appearances; success and warning reuse the status hues.
 */
export function actionFill(tone: ActionTone, theme: OpenCuriaTheme): string {
  switch (tone) {
    case "accent":
      return theme.scheme === "dark" ? "#48484A" : "#1C1C1E";
    case "success":
      return statusColor("positive", theme);
    case "warning":
      return statusColor("attention", theme);
    case "danger":
      return statusColor("critical", theme);
    default:
      return theme.scheme === "dark" ? "#636366" : "#8E8E93";
  }
}

/** System green of an iOS switch; ink would put a white knob on white. */
export function switchOnColor(theme: OpenCuriaTheme): string {
  return statusColor("positive", theme);
}
