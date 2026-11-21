import React from "react";
import { Toolbar } from "@expo/ui/swift-ui";
import {
  navigationBarTitleDisplayMode,
  navigationTitle,
} from "@expo/ui/swift-ui/modifiers";
import { visiblePrimaryAction } from "../ui/formStates";
import { ModalToolbarItems, useSearchModifiers } from "../ui/ModalFrame.ios";
import { NativeFormContent } from "../ui/NativeForm.ios";
import { useSettingsNavigation } from "./SettingsContext";
import type { SettingsPageProps } from "./SettingsPage.types";

/**
 * A SwiftUI Form in the settings NavigationStack: the system bar owns the
 * inline title, back button, swipe-back and the liquid-glass toolbar items,
 * laid out as on every sheet (close leading, main action and "…" trailing).
 */
export function SettingsPage({
  title,
  sections,
  root,
  primaryAction,
  actions,
  onAction,
  onRefresh,
  empty,
  loading,
  search,
  testID,
}: SettingsPageProps): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const searchModifiers = useSearchModifiers(search);
  return (
    <Toolbar
      modifiers={[
        navigationTitle(title),
        navigationBarTitleDisplayMode("inline"),
        ...searchModifiers,
      ]}
    >
      <NativeFormContent
        sections={sections}
        onRefresh={onRefresh}
        empty={empty}
        loading={loading}
        testID={testID}
      />
      <Toolbar.Content>
        <ModalToolbarItems
          // Pushed pages go back with the system's back button instead.
          onClose={root ? navigation.close : undefined}
          // The empty state's action replaces the toolbar's "+".
          primaryAction={visiblePrimaryAction(primaryAction, {
            sections,
            empty,
            loading,
          })}
          actions={actions}
          onAction={onAction}
        />
      </Toolbar.Content>
    </Toolbar>
  );
}
