import React from "react";
import { ActivityIndicator, View } from "react-native";
import { KeyboardAvoidingView as ImeAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { DockedSearchBar, Host, Icon, Text } from "@expo/ui/jetpack-compose";
import { fillMaxWidth, padding } from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "../ui/androidIcons";
import { ModalPrimaryAction } from "../ui/ModalPrimaryAction.android";
import { NativeForm } from "../ui/NativeForm.android";
import { NativeToolbar } from "../ui/NativeToolbar.android";
import { useTheme } from "../ui/theme";
import { useSettingsNavigation } from "./SettingsContext";
import type { SettingsPageProps } from "./SettingsPage.types";

/** Room for an extended floating action button above the last row. */
const FAB_INSET = 88;

/**
 * A Material settings screen: the AppCompat toolbar with back (×on the first
 * page) and overflow menu, a grouped Compose list and the main action at the
 * bottom.
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
  return (
    <View
      testID={testID}
      style={{
        flex: 1,
        paddingTop: insets.top,
        backgroundColor: theme.surface,
      }}
    >
      <NativeToolbar
        title={root ? "Settings" : title}
        subtitle=""
        titleColor={theme.text}
        subtitleColor={theme.mutedText}
        surfaceColor={theme.surface}
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
      {search ? (
        <Host
          matchContents={{ vertical: true }}
          colorScheme={theme.scheme}
          seedColor={theme.primary}
          ignoreSafeAreaKeyboardInsets
        >
          <DockedSearchBar
            onQueryChange={search.onChange}
            modifiers={[fillMaxWidth(), padding(16, 4, 16, 8)]}
          >
            <DockedSearchBar.LeadingIcon>
              <Icon source={androidIcon("magnifyingglass")!} size={24} />
            </DockedSearchBar.LeadingIcon>
            <DockedSearchBar.Placeholder>
              <Text>{search.placeholder}</Text>
            </DockedSearchBar.Placeholder>
          </DockedSearchBar>
        </Host>
      ) : null}
      {/* Edge-to-edge screens do not resize for the keyboard by themselves. */}
      <ImeAvoidingView behavior="padding" automaticOffset style={{ flex: 1 }}>
        <View style={{ flex: 1, paddingBottom: insets.bottom }}>
          {loading && !sections.length ? (
            <View
              style={{
                flex: 1,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <ActivityIndicator color={theme.primary} />
            </View>
          ) : (
            <NativeForm
              sections={sections}
              onRefresh={onRefresh}
              empty={empty}
              contentInsetBottom={primaryAction?.symbol ? FAB_INSET : 0}
            />
          )}
          {primaryAction ? <ModalPrimaryAction action={primaryAction} /> : null}
        </View>
      </ImeAvoidingView>
    </View>
  );
}
