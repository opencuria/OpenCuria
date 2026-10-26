import React from "react";
import { View } from "react-native";
import {
  Button,
  Host,
  NavigationStack,
  RNHostView,
  Toolbar,
  ToolbarItem,
} from "@expo/ui/swift-ui";
import {
  navigationTitle,
  navigationBarTitleDisplayMode,
} from "@expo/ui/swift-ui/modifiers";
import type { ModalFrameProps } from "./ModalFrame.types";
import { useTheme } from "./theme";

export function ModalFrame({
  title,
  onClose,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
    >
      <NavigationStack>
        <Toolbar
          modifiers={[
            navigationTitle(title),
            navigationBarTitleDisplayMode("inline"),
          ]}
        >
          <RNHostView>
            <View style={{ flex: 1 }}>{children}</View>
          </RNHostView>
          <Toolbar.Content>
            {onClose ? (
              <ToolbarItem placement="confirmationAction">
                <Button role="close" onPress={onClose} />
              </ToolbarItem>
            ) : null}
          </Toolbar.Content>
        </Toolbar>
      </NavigationStack>
    </Host>
  );
}
