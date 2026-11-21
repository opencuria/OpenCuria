import React, { useCallback, useEffect, useRef, useState } from "react";
import { Animated, StyleSheet, View } from "react-native";
import {
  Box,
  DropdownMenu,
  DropdownMenuItem,
  Host,
  Text,
} from "@expo/ui/jetpack-compose";
import { fillMaxSize } from "@expo/ui/jetpack-compose/modifiers";
import type { NativeContextMenuProps } from "./ContextMenu.types";
import { gestureThresholdHaptic } from "./haptics";
import { useTheme } from "./theme";

/** How far open content grows on each side, as iOS lifts it, at most 5 %. */
const LIFT_POINTS = 6;
const LIFT_MAX_SCALE = 1.05;

/**
 * Material DropdownMenu anchored to the content, opened by its long press.
 * Like iOS, the press ticks and the content lifts while the menu is open.
 * The anchor only mounts while the menu is open, so rows stay plain views.
 */
export function NativeContextMenu({
  actions,
  onSelect,
  children,
}: NativeContextMenuProps): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const width = useRef(0);
  const scale = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    const lifted = width.current
      ? Math.min(LIFT_MAX_SCALE, 1 + (2 * LIFT_POINTS) / width.current)
      : 1;
    Animated.spring(scale, {
      toValue: open ? lifted : 1,
      speed: 28,
      bounciness: open ? 8 : 0,
      useNativeDriver: true,
    }).start();
  }, [open, scale]);
  const showMenu = useCallback(() => {
    gestureThresholdHaptic();
    setOpen(true);
  }, []);
  return (
    <Animated.View
      collapsable={false}
      onLayout={(event) => {
        width.current = event.nativeEvent.layout.width;
      }}
      style={{ transform: [{ scale }] }}
    >
      {children(showMenu)}
      {open ? (
        <View
          pointerEvents="none"
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          style={StyleSheet.absoluteFill}
        >
          <Host
            style={StyleSheet.absoluteFill}
            colorScheme={theme.scheme}
            seedColor={theme.seed}
          >
            <DropdownMenu
              expanded
              color={theme.raised}
              onDismissRequest={() => setOpen(false)}
            >
              <DropdownMenu.Trigger>
                <Box modifiers={[fillMaxSize()]} />
              </DropdownMenu.Trigger>
              <DropdownMenu.Items>
                {actions.map((action) => (
                  <DropdownMenuItem
                    key={action.id}
                    enabled={!action.disabled}
                    elementColors={{
                      textColor: action.destructive ? theme.danger : theme.text,
                      disabledTextColor: theme.subtleText,
                    }}
                    onClick={() => {
                      setOpen(false);
                      onSelect(action.id);
                    }}
                  >
                    <DropdownMenuItem.Text>
                      <Text>{action.title}</Text>
                    </DropdownMenuItem.Text>
                  </DropdownMenuItem>
                ))}
              </DropdownMenu.Items>
            </DropdownMenu>
          </Host>
        </View>
      ) : null}
    </Animated.View>
  );
}
