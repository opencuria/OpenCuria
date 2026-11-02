import React from "react";
import { requireNativeView } from "expo";
import { View, type ViewProps } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { DockedSearchBar, Host, Icon, Text } from "@expo/ui/jetpack-compose";
import { fillMaxWidth, padding } from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import type { ModalFrameProps, ModalSearch } from "./ModalFrame.types";
import { useTheme } from "./theme";

/** Material 3 docked search bar below the toolbar. */
function SearchBar({ search }: { search: ModalSearch }): React.JSX.Element {
  const { theme } = useTheme();
  return (
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
  );
}

interface NativeToolbarMenuItem {
  id: string;
  title: string;
  enabled: boolean;
  checked: boolean;
}

const NativeToolbar = requireNativeView<
  ViewProps & {
    title: string;
    subtitle: string;
    titleColor: string;
    subtitleColor: string;
    surfaceColor: string;
    tintColor: string;
    showClose: boolean;
    menuItems: NativeToolbarMenuItem[];
    onClose: () => void;
    onMenuAction: (event: { nativeEvent: { id: string } }) => void;
  }
>("OpenCuriaNativeUI");

export function ModalFrame({
  title,
  subtitle,
  onClose,
  actions,
  onAction,
  search,
  children,
}: ModalFrameProps): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <View
      style={{
        flex: 1,
        paddingTop: insets.top,
        backgroundColor: theme.surface,
      }}
    >
      <NativeToolbar
        title={title}
        subtitle={subtitle ?? ""}
        titleColor={theme.text}
        subtitleColor={theme.mutedText}
        surfaceColor={theme.surface}
        tintColor={theme.text}
        showClose={Boolean(onClose)}
        menuItems={(actions ?? []).map((action) => ({
          id: action.id,
          title: action.title,
          enabled: !action.disabled,
          checked: Boolean(action.selected),
        }))}
        onClose={() => onClose?.()}
        onMenuAction={({ nativeEvent }) => onAction?.(nativeEvent.id)}
        style={{ height: 56, width: "100%" }}
      />
      {search ? <SearchBar search={search} /> : null}
      {children}
    </View>
  );
}
