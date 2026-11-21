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
  tint,
  type ModifierConfig,
} from "@expo/ui/swift-ui/modifiers";
import { ProminentLabel } from "./controls.ios";
import type { NativeSymbol } from "./controls.types";
import type {
  ModalFrameProps,
  ModalPrimaryAction,
  ModalSearch,
} from "./ModalFrame.types";
import { NativeFormContent } from "./NativeForm.ios";
import type { NativeMenuAction } from "./NativeMenu";
import { useTheme } from "./theme";

/** A confirmation is a prominent liquid-glass capsule on iOS 26, filled before it. */
const CONFIRM_STYLE = isLiquidGlassAvailable()
  ? "glassProminent"
  : "borderedProminent";

/**
 * Navigation-bar search whose text SwiftUI owns; JavaScript only learns
 * about edits. Returns the `searchable` modifier, or none without search.
 */
export function useSearchModifiers(
  search: ModalSearch | undefined,
): ModifierConfig[] {
  const text = useNativeState(search?.value ?? "");
  const latest = useRef(search?.value ?? "");
  useEffect(() => {
    if (search && search.value !== latest.current) {
      latest.current = search.value;
      text.set(search.value);
    }
  }, [search, text]);
  if (!search) return [];
  return [
    searchable(text, {
      prompt: search.placeholder,
      onChange: (value) => {
        latest.current = value;
        search.onChange(value);
      },
    }),
  ];
}

/**
 * The toolbar every sheet and settings page shares: close (×) leading, the
 * main action and the overflow menu ("…") trailing.
 */
export function ModalToolbarItems({
  onClose,
  primaryAction,
  actions,
  onAction,
}: {
  onClose?: () => void;
  primaryAction?: ModalPrimaryAction;
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const inactive = Boolean(primaryAction?.disabled);
  return (
    <>
      {onClose ? (
        <ToolbarItem placement="topBarLeading">
          {/* Below iOS 26 the close role is dropped and a bare close button
              draws nothing, so it carries its own xmark. */}
          <Button
            role="close"
            label="Close"
            systemImage="xmark"
            onPress={onClose}
            modifiers={[labelStyle("iconOnly"), accessibilityLabel("Close")]}
          />
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
                tint(theme.primary),
                disabled(inactive),
                accessibilityLabel(primaryAction.title),
              ]}
            />
          ) : (
            // SwiftUI draws white on the tint, which vanishes on the
            // near-white ink of dark mode; the label sets its own color.
            <Button
              testID={primaryAction.testID}
              onPress={primaryAction.onPress}
              modifiers={[
                buttonStyle(CONFIRM_STYLE),
                tint(theme.primary),
                disabled(inactive),
                accessibilityLabel(primaryAction.title),
              ]}
            >
              <ProminentLabel title={primaryAction.title} enabled={!inactive} />
            </Button>
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
    </>
  );
}

/**
 * A SwiftUI navigation bar above a native form or React Native content: the
 * system owns the title (large for collections, inline otherwise), search
 * and glass items.
 */
export function ModalFrame({
  title,
  subtitle,
  onClose,
  largeTitle,
  actions,
  onAction,
  primaryAction,
  search,
  form,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const searchModifiers = useSearchModifiers(search);
  return (
    <Host
      // The canvas behind the SwiftUI views, as in the settings stack.
      style={{ flex: 1, backgroundColor: theme.background }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
    >
      <NavigationStack>
        <Toolbar
          modifiers={[
            navigationTitle(subtitle ? "" : title),
            // A two-line title sits inline in the bar's center.
            navigationBarTitleDisplayMode(
              largeTitle && !subtitle ? "large" : "inline",
            ),
            ...searchModifiers,
          ]}
        >
          {form ? (
            // Inside the stack, as on a settings page: the large title
            // collapses and the bar's scroll edge follows the list.
            <NativeFormContent
              sections={form.sections}
              onRefresh={form.onRefresh}
              empty={form.empty}
              loading={form.loading}
              testID={form.testID}
            />
          ) : (
            <RNHostView>
              <View style={{ flex: 1 }}>{children}</View>
            </RNHostView>
          )}
          <Toolbar.Content>
            {subtitle ? (
              <ToolbarItem placement="principal">
                <VStack spacing={1}>
                  <Text
                    modifiers={[
                      font({ weight: "semibold" }),
                      foregroundStyle(theme.text),
                      lineLimit(1),
                    ]}
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
            <ModalToolbarItems
              onClose={onClose}
              primaryAction={primaryAction}
              actions={actions}
              onAction={onAction}
            />
          </Toolbar.Content>
        </Toolbar>
      </NavigationStack>
    </Host>
  );
}
