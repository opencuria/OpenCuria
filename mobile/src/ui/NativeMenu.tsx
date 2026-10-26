import React, { useRef } from "react";
import {
  Platform,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { MenuView, type MenuComponentRef } from "@react-native-menu/menu";
import { useTheme } from "./theme";

export interface NativeMenuAction {
  id: string;
  title: string;
  symbol?: string;
  destructive?: boolean;
  disabled?: boolean;
  selected?: boolean;
}

/** UIMenu / Android PopupMenu anchored to its actual trigger. */
export function NativeMenu({
  children,
  label,
  actions,
  onSelect,
  longPress = false,
  style,
  testID,
}: React.PropsWithChildren<{
  label: string;
  actions: NativeMenuAction[];
  onSelect: (id: string) => void;
  longPress?: boolean;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}>): React.JSX.Element {
  const { theme } = useTheme();
  const menuRef = useRef<MenuComponentRef>(null);
  const menu = (
    <MenuView
      ref={menuRef}
      style={Platform.OS === "android" ? StyleSheet.absoluteFill : style}
      testID={testID}
      title={label}
      themeVariant={theme.scheme}
      shouldOpenOnLongPress={longPress}
      onPressAction={({ nativeEvent }) => onSelect(nativeEvent.event)}
      actions={actions.map(
        ({ id, title, symbol, destructive, disabled, selected }) => ({
          id,
          title,
          image: Platform.OS === "ios" ? symbol : undefined,
          attributes: { destructive, disabled },
          state: selected === undefined ? undefined : selected ? "on" : "off",
        }),
      )}
    >
      <View
        accessible
        accessibilityRole="button"
        accessibilityLabel={label}
        style={Platform.OS === "android" ? { flex: 1 } : undefined}
        accessibilityActions={[{ name: "activate" }]}
        onAccessibilityAction={() => menuRef.current?.show?.()}
      >
        {Platform.OS === "android" ? null : children}
      </View>
    </MenuView>
  );
  return Platform.OS === "android" ? (
    <View collapsable={false} style={style}>
      <View
        pointerEvents="none"
        accessibilityElementsHidden
        importantForAccessibility="no-hide-descendants"
      >
        {children}
      </View>
      {menu}
    </View>
  ) : (
    menu
  );
}
