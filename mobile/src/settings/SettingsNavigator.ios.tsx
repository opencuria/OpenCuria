import React from "react";
import {
  Host,
  NavigationDestination,
  NavigationStack,
} from "@expo/ui/swift-ui";
import { useTheme } from "../ui/theme";
import type { SettingsNavigatorProps } from "./SettingsNavigator.types";

/**
 * A SwiftUI NavigationStack: NavigationLink rows push pages with the system
 * transition, swipe-back pops them, and every page keeps its own bar.
 */
export function SettingsNavigator({
  path,
  onPathChange,
  root,
  renderRoute,
}: SettingsNavigatorProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
    >
      <NavigationStack path={path} onPathChange={onPathChange}>
        {root}
        {path.map((route) => (
          <NavigationDestination key={route} value={route}>
            {renderRoute(route)}
          </NavigationDestination>
        ))}
      </NavigationStack>
    </Host>
  );
}
