import React from "react";
import { View } from "react-native";
import { GlassView, isGlassEffectAPIAvailable } from "expo-glass-effect";
import { useTheme } from "./theme";

/** The same native liquid-glass surface used by t3code's docked editor. */
export function ComposerSurface({
  children,
}: React.PropsWithChildren): React.JSX.Element {
  const { theme } = useTheme();
  const Component = isGlassEffectAPIAvailable() ? GlassView : View;
  return (
    <Component
      {...(Component === GlassView
        ? { glassEffectStyle: "regular" as const, colorScheme: theme.scheme }
        : {})}
      style={{
        borderRadius: 26,
        overflow: "hidden",
        padding: 6,
        ...(Component === View ? { backgroundColor: theme.surfaceMuted } : {}),
      }}
    >
      {children}
    </Component>
  );
}
