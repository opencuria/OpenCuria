import React from "react";
import {
  Button,
  ContextMenu,
  HStack,
  Host,
  Image,
  List,
  Menu,
  ProgressView,
  Section,
  Spacer,
  Text,
  VStack,
} from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  buttonStyle,
  disabled,
  font,
  foregroundStyle,
  frame,
  listRowBackground,
  listStyle,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeListProps, NativeListRow } from "./NativeList.types";
import { useTheme } from "./theme";

function ListRow({ row }: { row: NativeListRow }): React.JSX.Element {
  const { theme } = useTheme();
  const actions = row.actions?.map((action) => (
    <Button
      key={action.id}
      label={action.title}
      role={action.destructive ? "destructive" : undefined}
      modifiers={[disabled(Boolean(action.disabled))]}
      onPress={() => row.onAction?.(action.id)}
    />
  ));
  const body = (
    <HStack
      modifiers={row.selected ? [listRowBackground(theme.surfaceMuted)] : []}
    >
      <Button
        onPress={row.onPress}
        testID={row.testID ?? row.id}
        modifiers={[
          buttonStyle("borderless"),
          disabled(Boolean(row.disabled || !row.onPress)),
          accessibilityLabel(
            [row.title, row.subtitle].filter(Boolean).join(", "),
          ),
          frame({ maxWidth: Infinity, alignment: "leading", minHeight: 44 }),
        ]}
      >
        <HStack spacing={10}>
          {row.symbol ? (
            <Image systemName={row.symbol} size={18} color={theme.primary} />
          ) : null}
          <VStack alignment="leading" spacing={3}>
            <Text
              modifiers={[
                font({
                  textStyle: "body",
                  weight: row.unread ? "semibold" : "regular",
                }),
                foregroundStyle(theme.text),
              ]}
            >
              {row.title}
            </Text>
            {row.subtitle ? (
              <Text
                modifiers={[
                  font({ textStyle: "caption" }),
                  foregroundStyle(theme.mutedText),
                ]}
              >
                {row.subtitle}
              </Text>
            ) : null}
          </VStack>
          <Spacer />
          {row.busy ? (
            <ProgressView />
          ) : row.selected ? (
            <Image systemName="checkmark" size={16} color={theme.primary} />
          ) : null}
        </HStack>
      </Button>
      {actions?.length ? (
        <Menu
          label={
            <Image systemName="ellipsis" size={18} color={theme.primary} />
          }
          modifiers={[accessibilityLabel(`Actions for ${row.title}`)]}
        >
          {actions}
        </Menu>
      ) : null}
    </HStack>
  );
  return actions?.length ? (
    <ContextMenu>
      <ContextMenu.Trigger>{body}</ContextMenu.Trigger>
      <ContextMenu.Items>{actions}</ContextMenu.Items>
    </ContextMenu>
  ) : (
    body
  );
}

/** SwiftUI List/Section provide real native rows, insets, scrolling and disclosure. */
export function NativeList({
  sections,
  sidebar,
}: NativeListProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
    >
      <List modifiers={[listStyle(sidebar ? "sidebar" : "insetGrouped")]}>
        {sections.map((section) => (
          <Section
            key={section.id}
            title={section.onPress ? undefined : section.title}
            header={
              section.onPress ? (
                <Button
                  onPress={section.onPress}
                  modifiers={[
                    buttonStyle("borderless"),
                    accessibilityLabel(`Open workspace ${section.title}`),
                  ]}
                >
                  <HStack>
                    <Text
                      modifiers={[
                        font({ textStyle: "subheadline", weight: "semibold" }),
                        foregroundStyle(
                          section.selected ? theme.primary : theme.text,
                        ),
                      ]}
                    >
                      {section.title}
                    </Text>
                    <Spacer />
                    {section.busy ? <ProgressView /> : null}
                  </HStack>
                </Button>
              ) : undefined
            }
            isExpanded={section.expanded}
            onIsExpandedChange={section.onExpandedChange}
          >
            {section.rows.map((row) => (
              <ListRow key={row.id} row={row} />
            ))}
          </Section>
        ))}
      </List>
    </Host>
  );
}
