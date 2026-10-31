import React from "react";
import { View } from "react-native";
import {
  Button,
  Host,
  Menu,
  NavigationStack,
  RNHostView,
  Text,
  Toolbar,
  ToolbarItem,
  VStack,
} from "@expo/ui/swift-ui";
import {
  disabled,
  font,
  foregroundStyle,
  labelStyle,
  lineLimit,
  navigationBarTitleDisplayMode,
  navigationTitle,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeSymbol } from "./controls.types";
import type { ModalFrameProps } from "./ModalFrame.types";
import { useTheme } from "./theme";

export function ModalFrame({
  title,
  subtitle,
  onClose,
  closePlacement = "trailing",
  actions,
  onAction,
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
            navigationTitle(subtitle ? "" : title),
            navigationBarTitleDisplayMode("inline"),
          ]}
        >
          <RNHostView>
            <View style={{ flex: 1 }}>{children}</View>
          </RNHostView>
          <Toolbar.Content>
            {subtitle ? (
              <ToolbarItem placement="principal">
                <VStack spacing={1}>
                  <Text modifiers={[font({ weight: "semibold" }), lineLimit(1)]}>
                    {title}
                  </Text>
                  <Text
                    modifiers={[
                      font({ size: 12 }),
                      foregroundStyle(theme.mutedText),
                      lineLimit(1),
                    ]}
                  >
                    {subtitle}
                  </Text>
                </VStack>
              </ToolbarItem>
            ) : null}
            {onClose ? (
              <ToolbarItem
                placement={
                  closePlacement === "leading"
                    ? "topBarLeading"
                    : "confirmationAction"
                }
              >
                <Button role="close" onPress={onClose} />
              </ToolbarItem>
            ) : null}
            {actions?.length ? (
              <ToolbarItem placement="topBarTrailing">
                <Menu
                  label="More"
                  systemImage="ellipsis"
                  modifiers={[labelStyle("iconOnly")]}
                >
                  {actions.map((action) => (
                    <Button
                      key={action.id}
                      label={action.title}
                      systemImage={
                        action.selected
                          ? "checkmark"
                          : (action.symbol as NativeSymbol | undefined)
                      }
                      role={action.destructive ? "destructive" : undefined}
                      modifiers={[disabled(Boolean(action.disabled))]}
                      onPress={() => onAction?.(action.id)}
                    />
                  ))}
                </Menu>
              </ToolbarItem>
            ) : null}
          </Toolbar.Content>
        </Toolbar>
      </NavigationStack>
    </Host>
  );
}
