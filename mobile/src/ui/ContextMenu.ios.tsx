import React from "react";
import { MenuView } from "@react-native-menu/menu";
import type { NativeContextMenuProps } from "./ContextMenu.types";
import { useTheme } from "./theme";

/** UIContextMenu that lifts the content on long press; taps reach the content. */
export function NativeContextMenu({
  label,
  actions,
  onSelect,
  children,
}: NativeContextMenuProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <MenuView
      title={label}
      themeVariant={theme.scheme}
      shouldOpenOnLongPress
      onPressAction={({ nativeEvent }) => onSelect(nativeEvent.event)}
      actions={actions.map(({ id, title, symbol, destructive, disabled }) => ({
        id,
        title,
        image: symbol,
        attributes: { destructive, disabled },
      }))}
    >
      {children()}
    </MenuView>
  );
}
