import React, { useState } from "react";
import {
  DropdownMenu,
  DropdownMenuItem,
  Host,
  IconButton,
  Icon,
  LazyColumn,
  ListItem,
  Row,
  Text,
  TextButton,
  CircularProgressIndicator,
} from "@expo/ui/jetpack-compose";
import {
  clickable,
  fillMaxSize,
  fillMaxWidth,
  padding,
  semantics,
  size,
  weight,
} from "@expo/ui/jetpack-compose/modifiers";
import type { NativeListProps, NativeListRow } from "./NativeList.types";
import { useTheme } from "./theme";

function ListRow({ row }: { row: NativeListRow }): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const { theme } = useTheme();
  return (
    <ListItem
      colors={{
        containerColor: row.selected ? theme.surfaceMuted : "transparent",
      }}
      modifiers={[
        fillMaxWidth(),
        semantics({
          contentDescription: [row.title, row.subtitle]
            .filter(Boolean)
            .join(", "),
        }),
        ...(row.onPress && !row.disabled ? [clickable(row.onPress)] : []),
      ]}
    >
      <ListItem.HeadlineContent>
        <Text style={{ fontWeight: row.unread ? "600" : "400" }}>
          {row.title}
        </Text>
      </ListItem.HeadlineContent>
      {row.subtitle ? (
        <ListItem.SupportingContent>
          <Text>{row.subtitle}</Text>
        </ListItem.SupportingContent>
      ) : null}
      <ListItem.TrailingContent>
        <Row verticalAlignment="center">
          {row.busy ? (
            <CircularProgressIndicator modifiers={[size(18, 18)]} />
          ) : row.selected ? (
            <Icon
              source={require("../../assets/native-icons/check.xml")}
              size={18}
            />
          ) : null}
          {row.actions?.length ? (
            <DropdownMenu
              expanded={expanded}
              onDismissRequest={() => setExpanded(false)}
            >
              <DropdownMenu.Trigger>
                <IconButton
                  onClick={() => setExpanded(true)}
                  modifiers={[
                    semantics({
                      contentDescription: `Actions for ${row.title}`,
                    }),
                  ]}
                >
                  <Icon
                    source={require("../../assets/native-icons/more-vert.xml")}
                    size={24}
                  />
                </IconButton>
              </DropdownMenu.Trigger>
              <DropdownMenu.Items>
                {row.actions.map((action) => (
                  <DropdownMenuItem
                    key={action.id}
                    enabled={!action.disabled}
                    elementColors={
                      action.destructive
                        ? { textColor: theme.danger }
                        : undefined
                    }
                    onClick={() => {
                      setExpanded(false);
                      row.onAction?.(action.id);
                    }}
                  >
                    <DropdownMenuItem.Text>
                      <Text>{action.title}</Text>
                    </DropdownMenuItem.Text>
                  </DropdownMenuItem>
                ))}
              </DropdownMenu.Items>
            </DropdownMenu>
          ) : null}
        </Row>
      </ListItem.TrailingContent>
    </ListItem>
  );
}

/** Material LazyColumn/ListItem own rows and their native interaction feedback. */
export function NativeList({ sections }: NativeListProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeAreaKeyboardInsets
    >
      <LazyColumn modifiers={[fillMaxSize()]} contentPadding={{ bottom: 16 }}>
        {sections.flatMap((section) => [
          ...(section.title
            ? [
                <Row
                  key={`${section.id}-header`}
                  verticalAlignment="center"
                  modifiers={[fillMaxWidth(), padding(8, 4, 8, 4)]}
                >
                  {section.onPress ? (
                    <TextButton
                      onClick={section.onPress}
                      modifiers={[
                        weight(1),
                        semantics({
                          contentDescription: `Open workspace ${section.title}`,
                        }),
                      ]}
                    >
                      <Row modifiers={[fillMaxWidth()]}>
                        <Text style={{ fontWeight: "600" }}>
                          {section.title}
                        </Text>
                      </Row>
                    </TextButton>
                  ) : (
                    <Text
                      modifiers={[weight(1), padding(8, 8, 8, 8)]}
                      style={{ fontWeight: "600" }}
                    >
                      {section.title}
                    </Text>
                  )}
                  {section.busy ? (
                    <CircularProgressIndicator modifiers={[size(18, 18)]} />
                  ) : null}
                  {section.onExpandedChange ? (
                    <IconButton
                      onClick={() =>
                        section.onExpandedChange?.(!section.expanded)
                      }
                      modifiers={[
                        semantics({
                          contentDescription: `${section.expanded ? "Collapse" : "Expand"} workspace ${section.title}`,
                        }),
                      ]}
                    >
                      <Icon
                        source={
                          section.expanded
                            ? require("../../assets/native-icons/expand-less.xml")
                            : require("../../assets/native-icons/expand-more.xml")
                        }
                        size={24}
                      />
                    </IconButton>
                  ) : null}
                </Row>,
              ]
            : []),
          ...(section.expanded === false
            ? []
            : section.rows.map((row) => <ListRow key={row.id} row={row} />)),
        ])}
      </LazyColumn>
    </Host>
  );
}
