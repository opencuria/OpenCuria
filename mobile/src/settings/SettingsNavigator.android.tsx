import React, { useMemo } from "react";
import { StyleSheet, View } from "react-native";
import Animated, { SlideInRight } from "react-native-reanimated";
import { FormNavigationContext } from "../ui/formNavigation";
import type { SettingsNavigatorProps } from "./SettingsNavigator.types";

/**
 * Pages stay mounted beneath the one on top, like a native back stack, so
 * going back keeps their state; Back pops (see the settings modal).
 */
export function SettingsNavigator({
  path,
  onPathChange,
  root,
  renderRoute,
}: SettingsNavigatorProps): React.JSX.Element {
  const navigation = useMemo(
    () => ({ push: (route: string) => onPathChange([...path, route]) }),
    [path, onPathChange],
  );
  const top = path.length;
  return (
    <FormNavigationContext.Provider value={navigation}>
      <View style={styles.page} pointerEvents={top ? "none" : "auto"}>
        {root}
      </View>
      {path.map((route, index) => (
        <Animated.View
          key={route}
          entering={SlideInRight.duration(220)}
          pointerEvents={index === top - 1 ? "auto" : "none"}
          style={styles.page}
        >
          {renderRoute(route)}
        </Animated.View>
      ))}
    </FormNavigationContext.Provider>
  );
}

const styles = StyleSheet.create({
  page: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0 },
});
