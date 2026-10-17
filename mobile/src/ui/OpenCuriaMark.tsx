import React from "react";
import Svg, { Polygon } from "react-native-svg";
import { useTheme } from "./theme";

/** OpenCuria's six-facet isometric mark, matching the webapp branding. */
export function OpenCuriaMark({
  size = 32,
}: {
  size?: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Svg width={size} height={size} viewBox="13 13 38 38" accessible={false}>
      <Polygon points="18,23 32,15 32,32" fill={theme.text} opacity={0.24} />
      <Polygon points="32,15 46,23 32,32" fill={theme.text} opacity={0.42} />
      <Polygon points="46,23 32,32 46,41" fill={theme.primary} />
      <Polygon points="32,32 32,49 46,41" fill={theme.primary} opacity={0.8} />
      <Polygon points="18,23 32,32 18,41" fill={theme.text} opacity={0.18} />
      <Polygon points="32,32 32,49 18,41" fill={theme.text} opacity={0.1} />
    </Svg>
  );
}
