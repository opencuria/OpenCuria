import React, { useEffect, useRef } from "react";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  Menu,
  ProgressView,
  Toolbar,
  ToolbarItem,
  useNativeState,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  buttonStyle,
  disabled,
  font,
  labelStyle,
  navigationBarTitleDisplayMode,
  navigationTitle,
  searchable,
} from "@expo/ui/swift-ui/modifiers";
import type { ModalSearch } from "../ui/ModalFrame.types";
import type { NativeSymbol } from "../ui/controls.types";
import { NativeFormContent } from "../ui/NativeForm.ios";
import { useSettingsNavigation } from "./SettingsContext";
import type { SettingsPageProps } from "./SettingsPage.types";

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

/**
 * A SwiftUI Form in the settings NavigationStack: the system bar owns the
 * title, back button, swipe-back and the liquid-glass toolbar items.
 */
export function SettingsPage({
  title,
  sections,
  root,
  largeTitle,
  primaryAction,
  actions,
  onAction,
  onRefresh,
  empty,
  loading,
  search,
}: SettingsPageProps): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const { text, latest } = useSearchState(search);
  return (
    <Toolbar
      modifiers={[
        navigationTitle(root ? "" : title),
        navigationBarTitleDisplayMode(largeTitle ? "large" : "inline"),
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
      {loading && !sections.length ? (
        <ProgressView />
      ) : (
        <NativeFormContent
          sections={sections}
          onRefresh={onRefresh}
          empty={empty}
        />
      )}
      <Toolbar.Content>
        {root ? (
          <ToolbarItem placement="topBarTrailing">
            <Button role="close" onPress={navigation.close} />
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
  );
}
