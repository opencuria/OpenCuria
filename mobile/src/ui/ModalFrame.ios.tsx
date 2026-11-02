import React, { useEffect, useRef } from "react";
import { View } from "react-native";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  Host,
  Menu,
  NavigationStack,
  ProgressView,
  RNHostView,
  Text,
  Toolbar,
  ToolbarItem,
  VStack,
  useNativeState,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  buttonStyle,
  disabled,
  font,
  foregroundStyle,
  labelStyle,
  lineLimit,
  navigationBarTitleDisplayMode,
  navigationTitle,
  searchable,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeSymbol } from "./controls.types";
import type { ModalFrameProps, ModalSearch } from "./ModalFrame.types";
import { useTheme } from "./theme";

/** iOS 26 confirms with a prominent liquid-glass button, as system sheets do. */
const CONFIRM_STYLE = isLiquidGlassAvailable() ? "glassProminent" : "automatic";

/** Search text owned by SwiftUI; JavaScript only learns about edits. */
function useSearchState(search: ModalSearch | undefined) {
  const text = useNativeState(search?.value ?? "");
  const latest = useRef(search?.value ?? "");
  useEffect(() => {
    if (search && search.value !== latest.current) {
      latest.current = search.value;
      text.set(search.value);
    }
  }, [search, text]);
  return { text, latest };
}

export function ModalFrame({
  title,
  subtitle,
  onClose,
  closePlacement = "trailing",
  actions,
  onAction,
  primaryAction,
  search,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const { text, latest } = useSearchState(search);
  // A screen with its own main action closes from the leading edge.
  const closeLeading = closePlacement === "leading" || Boolean(primaryAction);
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
            ...(search
              ? [
                  searchable(text, {
                    prompt: search.placeholder,
                    onChange: (value) => {
                      latest.current = value;
                      search.onChange(value);
                    },
                  }),
                ]
              : []),
          ]}
        >
          <RNHostView>
            <View style={{ flex: 1 }}>{children}</View>
          </RNHostView>
          <Toolbar.Content>
            {subtitle ? (
              <ToolbarItem placement="principal">
                <VStack spacing={1}>
                  <Text
                    modifiers={[font({ weight: "semibold" }), lineLimit(1)]}
                  >
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
                  closeLeading ? "topBarLeading" : "confirmationAction"
                }
              >
                <Button role="close" onPress={onClose} />
              </ToolbarItem>
            ) : null}
            {primaryAction ? (
              <ToolbarItem
                placement={
                  primaryAction.symbol ? "primaryAction" : "confirmationAction"
                }
              >
                {primaryAction.busy ? (
                  <ProgressView />
                ) : primaryAction.symbol ? (
                  <Button
                    label={primaryAction.title}
                    systemImage={primaryAction.symbol}
                    testID={primaryAction.testID}
                    onPress={primaryAction.onPress}
                    modifiers={[
                      labelStyle("iconOnly"),
                      disabled(Boolean(primaryAction.disabled)),
                      accessibilityLabel(primaryAction.title),
                    ]}
                  />
                ) : (
                  <Button
                    label={primaryAction.title}
                    testID={primaryAction.testID}
                    onPress={primaryAction.onPress}
                    modifiers={[
                      buttonStyle(CONFIRM_STYLE),
                      font({ weight: "semibold" }),
                      disabled(Boolean(primaryAction.disabled)),
                    ]}
                  />
                )}
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
