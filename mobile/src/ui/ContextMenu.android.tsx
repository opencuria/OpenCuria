import React, { useState } from "react";
import { StyleSheet, View } from "react-native";
import {
  Box,
  DropdownMenu,
  DropdownMenuItem,
  Host,
  Text,
} from "@expo/ui/jetpack-compose";
import { fillMaxSize } from "@expo/ui/jetpack-compose/modifiers";
import type { NativeContextMenuProps } from "./ContextMenu.types";
import { useTheme } from "./theme";

/**
 * Material DropdownMenu anchored to the content, opened by its long press.
 * The anchor only mounts while the menu is open, so rows stay plain views.
 */
export function NativeContextMenu({
  actions,
  onSelect,
  children,
}: NativeContextMenuProps): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  return (
    <View collapsable={false}>
      {children(() => setOpen(true))}
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
            seedColor={theme.primary}
          >
            <DropdownMenu expanded onDismissRequest={() => setOpen(false)}>
              <DropdownMenu.Trigger>
                <Box modifiers={[fillMaxSize()]} />
              </DropdownMenu.Trigger>
              <DropdownMenu.Items>
                {actions.map((action) => (
                  <DropdownMenuItem
                    key={action.id}
                    enabled={!action.disabled}
                    elementColors={
                      action.destructive
                        ? { textColor: theme.danger }
                        : undefined
                    }
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
    </View>
  );
}
