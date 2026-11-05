import React from "react";
import { View } from "react-native";
import {
  Button,
  DockedSearchBar,
  FilledTonalIconButton,
  Host,
  Icon,
  IconButton,
  Row,
  Spacer,
  Text,
} from "@expo/ui/jetpack-compose";
import {
  semantics,
  size,
  weight,
  width,
} from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import type {
  AccountButtonProps,
  NewChatButtonProps,
  SidebarIconButtonProps,
  SidebarSearchFieldProps,
} from "./SidebarControls.types";
import { useTheme } from "./theme";

/** A tonal Material icon button, e.g. Search. */
export function SidebarIconButton({
  symbol,
  label,
  onPress,
  testID,
}: SidebarIconButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View testID={testID} style={{ width: 48, height: 48 }}>
      <Host
        style={{ width: 48, height: 48 }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <FilledTonalIconButton
          onClick={onPress}
          colors={{
            containerColor: theme.surfaceMuted,
            contentColor: theme.text,
          }}
          modifiers={[size(48, 48), semantics({ contentDescription: label })]}
        >
          <Icon source={androidIcon(symbol)!} size={24} />
        </FilledTonalIconButton>
      </Host>
    </View>
  );
}

/** The user's initials on a tonal circle; opens Settings. */
export function AccountButton({
  initials,
  label,
  onPress,
  testID,
}: AccountButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View testID={testID} style={{ width: 48, height: 48 }}>
      <Host
        style={{ width: 48, height: 48 }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <FilledTonalIconButton
          onClick={onPress}
          colors={{
            containerColor: theme.surfaceMuted,
            contentColor: theme.text,
          }}
          modifiers={[size(48, 48), semantics({ contentDescription: label })]}
        >
          <Text style={{ typography: "titleMedium", fontWeight: "600" }}>
            {initials}
          </Text>
        </FilledTonalIconButton>
      </Host>
    </View>
  );
}

/** A filled Material button with the compose icon, in the text color. */
export function NewChatButton({
  onPress,
  disabled,
  testID,
}: NewChatButtonProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View testID={testID}>
      <Host
        matchContents
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Button
          onClick={onPress}
          enabled={!disabled}
          colors={{
            containerColor: theme.text,
            contentColor: theme.background,
          }}
          contentPadding={{ start: 18, top: 12, end: 22, bottom: 12 }}
          modifiers={[semantics({ contentDescription: "New chat" })]}
        >
          <Icon source={androidIcon("pencil")!} size={20} />
          <Spacer modifiers={[width(8)]} />
          <Text style={{ typography: "titleMedium", fontWeight: "600" }}>
            New chat
          </Text>
        </Button>
      </Host>
    </View>
  );
}

/** Material 3 docked search bar with a close button beside it. */
export function SidebarSearchField({
  onChange,
  onCancel,
  placeholder,
  accessibilityLabel,
  testID,
}: SidebarSearchFieldProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View testID={testID} style={{ flex: 1 }}>
      <Host
        matchContents={{ vertical: true }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        <Row verticalAlignment="center">
          <DockedSearchBar
            onQueryChange={onChange}
            modifiers={[
              weight(1),
              semantics({ contentDescription: accessibilityLabel }),
            ]}
          >
            <DockedSearchBar.LeadingIcon>
              <Icon source={androidIcon("magnifyingglass")!} size={24} />
            </DockedSearchBar.LeadingIcon>
            <DockedSearchBar.Placeholder>
              <Text>{placeholder}</Text>
            </DockedSearchBar.Placeholder>
          </DockedSearchBar>
          <IconButton
            onClick={onCancel}
            modifiers={[semantics({ contentDescription: "Cancel" })]}
          >
            <Icon source={androidIcon("xmark")!} size={24} />
          </IconButton>
        </Row>
      </Host>
    </View>
  );
}
