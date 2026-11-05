import { StyleSheet, type PressableProps, type ViewStyle } from "react-native";

/** Keep caller layout while the OS owns the control's paint and shape. */
export function controlLayout(style: PressableProps["style"]): ViewStyle {
  const resolved =
    StyleSheet.flatten(
      typeof style === "function"
        ? style({ pressed: false, hovered: false })
        : style,
    ) ?? {};
  const keys = [
    "flex",
    "flexGrow",
    "flexShrink",
    "flexBasis",
    "alignSelf",
    "width",
    "minWidth",
    "maxWidth",
    "height",
    "minHeight",
    "maxHeight",
    "margin",
    "marginTop",
    "marginBottom",
    "marginHorizontal",
    "marginVertical",
    "marginLeft",
    "marginRight",
    "position",
    "top",
    "right",
    "bottom",
    "left",
    "zIndex",
  ] as const;
  return Object.fromEntries(
    keys
      .filter((key) => resolved[key] !== undefined)
      .map((key) => [key, resolved[key]]),
  ) as ViewStyle;
}

/**
 * Whether a control sized by the caller's layout gets no width from it (e.g.
 * `alignSelf: "center"`). Native hosts then size it to its own label instead.
 */
export function hugsContent(layout: ViewStyle): boolean {
  return (
    layout.alignSelf !== undefined &&
    layout.alignSelf !== "stretch" &&
    layout.width === undefined &&
    layout.flex === undefined &&
    layout.flexGrow === undefined
  );
}

/** Rich labels lay out normally, independently of their native hit surface. */
export function controlContentStyle(style: PressableProps["style"]): ViewStyle {
  const resolved =
    StyleSheet.flatten(
      typeof style === "function"
        ? style({ pressed: false, hovered: false })
        : style,
    ) ?? {};
  const layout = controlLayout(style);
  const sizes = new Set([
    "width",
    "minWidth",
    "maxWidth",
    "height",
    "minHeight",
    "maxHeight",
  ]);
  return Object.fromEntries(
    Object.entries(resolved).filter(
      ([key]) => !(key in layout) || sizes.has(key),
    ),
  ) as ViewStyle;
}
