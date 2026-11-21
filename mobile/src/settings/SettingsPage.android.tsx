import React from "react";
import { View } from "react-native";
import { KeyboardAvoidingView as ImeAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { visiblePrimaryAction } from "../ui/formStates";
import { ModalPrimaryAction } from "../ui/ModalPrimaryAction.android";
import { NativeForm } from "../ui/NativeForm.android";
import { NativeToolbar } from "../ui/NativeToolbar.android";
import { SearchField } from "../ui/SearchField.android";
import { useTheme } from "../ui/theme";
import { useSettingsNavigation } from "./SettingsContext";
import type { SettingsPageProps } from "./SettingsPage.types";

/** Room for an extended floating action button above the last row. */
const FAB_INSET = 88;

/**
 * A Material settings screen on the chat's canvas: the AppCompat toolbar with
 * back (× on the first page) and overflow menu, a grouped Compose list and
 * the main action at the bottom.
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
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const navigation = useSettingsNavigation();
  // The empty state's own action replaces the main action.
  const primary = visiblePrimaryAction(primaryAction, {
    sections,
    empty,
    loading,
  });
  return (
    <View
      testID={testID}
      style={{
        flex: 1,
        paddingTop: insets.top,
        backgroundColor: theme.background,
      }}
    >
      {/* The native toolbar parses colors with Color.parseColor: hex only. */}
      <NativeToolbar
        title={root ? "Settings" : title}
        subtitle=""
        titleColor={theme.text}
        subtitleColor={theme.mutedText}
        surfaceColor={theme.background}
        tintColor={theme.text}
        showClose
        navigationIcon={root ? "close" : "back"}
        menuItems={(actions ?? []).map((action) => ({
          id: action.id,
          title: action.title,
          enabled: !action.disabled,
          checked: Boolean(action.selected),
        }))}
        onClose={root ? navigation.close : navigation.pop}
        onMenuAction={({ nativeEvent }) => onAction?.(nativeEvent.id)}
        style={{ height: 56, width: "100%" }}
      />
      {search ? <SearchField search={search} /> : null}
      {/* Edge-to-edge screens do not resize for the keyboard by themselves. */}
      <ImeAvoidingView behavior="padding" automaticOffset style={{ flex: 1 }}>
        <View style={{ flex: 1, paddingBottom: insets.bottom }}>
          <NativeForm
            sections={sections}
            onRefresh={onRefresh}
            empty={empty}
            loading={loading}
            contentInsetBottom={primary?.symbol ? FAB_INSET : 0}
          />
          {primary ? <ModalPrimaryAction action={primary} /> : null}
        </View>
      </ImeAvoidingView>
    </View>
  );
}
