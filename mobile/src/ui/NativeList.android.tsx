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
import { useTheme, type OpenCuriaTheme } from "./theme";

const TRANSPARENT = "#00000000";

/** Ink icon buttons; the content color also tints the ripple. */
function iconButtonColors(theme: OpenCuriaTheme) {
  return {
    containerColor: TRANSPARENT,
    contentColor: theme.text,
    disabledContainerColor: TRANSPARENT,
    disabledContentColor: theme.subtleText,
  };
}

function ListRow({ row }: { row: NativeListRow }): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const { theme } = useTheme();
  return (
    <ListItem
      colors={{
        containerColor: row.selected ? theme.controlFill : TRANSPARENT,
        contentColor: row.disabled ? theme.subtleText : theme.text,
        supportingContentColor: theme.mutedText,
        leadingContentColor: theme.text,
        trailingContentColor: theme.text,
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
            <CircularProgressIndicator
              color={theme.mutedText}
              strokeWidth={2}
              modifiers={[size(18, 18)]}
            />
          ) : row.selected ? (
            <Icon
              source={require("../../assets/native-icons/check.xml")}
              tint={theme.text}
              size={18}
            />
          ) : null}
          {row.actions?.length ? (
            <DropdownMenu
              expanded={expanded}
              color={theme.raised}
              onDismissRequest={() => setExpanded(false)}
            >
              <DropdownMenu.Trigger>
                <IconButton
                  onClick={() => setExpanded(true)}
                  colors={iconButtonColors(theme)}
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
                    elementColors={{
                      textColor: action.destructive ? theme.danger : theme.text,
                      disabledTextColor: theme.subtleText,
                    }}
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
      seedColor={theme.seed}
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
                      colors={{
                        containerColor: TRANSPARENT,
                        contentColor: theme.text,
                      }}
                      modifiers={[
                        weight(1),
                        semantics({
                          contentDescription: `Open workspace ${section.title}`,
                        }),
                      ]}
                    >
                      <Row modifiers={[fillMaxWidth()]}>
                        <Text color={theme.text} style={{ fontWeight: "600" }}>
                          {section.title}
                        </Text>
                      </Row>
                    </TextButton>
                  ) : (
                    <Text
                      color={theme.text}
                      modifiers={[weight(1), padding(8, 8, 8, 8)]}
                      style={{ fontWeight: "600" }}
                    >
                      {section.title}
                    </Text>
                  )}
                  {section.busy ? (
                    <CircularProgressIndicator
                      color={theme.mutedText}
                      strokeWidth={2}
                      modifiers={[size(18, 18)]}
                    />
                  ) : null}
                  {section.onExpandedChange ? (
                    <IconButton
                      onClick={() =>
                        section.onExpandedChange?.(!section.expanded)
                      }
                      colors={iconButtonColors(theme)}
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
