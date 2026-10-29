import React from "react";
import {
  Button,
  ContextMenu,
  Host,
  RNHostView,
  Section,
} from "@expo/ui/swift-ui";
import { disabled } from "@expo/ui/swift-ui/modifiers";
import type { NativeContextMenuProps } from "./ContextMenu.types";
import type { NativeSymbol } from "./controls.types";
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
    <Host
      matchContents={{ vertical: true }}
      style={{ width: "100%" }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeArea="all"
    >
      <ContextMenu>
        <ContextMenu.Trigger>
          {/* RNHostView preserves the row's touch target and intrinsic height. */}
          <RNHostView matchContents={{ vertical: true }}>
            {children()}
          </RNHostView>
        </ContextMenu.Trigger>
        <ContextMenu.Items>
          <Section title={label}>
            {actions.map((action) => (
              <Button
                key={action.id}
                label={action.title}
                systemImage={action.symbol as NativeSymbol | undefined}
                role={action.destructive ? "destructive" : undefined}
                modifiers={[disabled(Boolean(action.disabled))]}
                onPress={() => onSelect(action.id)}
              />
            ))}
          </Section>
        </ContextMenu.Items>
      </ContextMenu>
    </Host>
  );
}
