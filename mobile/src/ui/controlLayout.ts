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
