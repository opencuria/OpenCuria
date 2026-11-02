import React, { useEffect, useRef, useState } from "react";
import { View } from "react-native";
import {
  Box,
  Card,
  CircularProgressIndicator,
  Column,
  DropdownMenu,
  DropdownMenuItem,
  FilledTonalIconButton,
  HorizontalDivider,
  Host,
  Icon,
  IconButton,
  LazyColumn,
  LinearProgressIndicator,
  ListItem,
  OutlinedTextField,
  PullToRefreshBox,
  Row,
  Switch,
  Text,
  useNativeState,
} from "@expo/ui/jetpack-compose";
import {
  Shapes,
  background,
  clickable,
  clip,
  combinedClickable,
  fillMaxSize,
  fillMaxWidth,
  padding,
  semantics,
  size,
  testID as composeTestID,
} from "@expo/ui/jetpack-compose/modifiers";
import { WORKSPACE_ICON, androidIcon } from "./androidIcons";
import type {
  NativeFormHeaderRow,
  NativeFormItemRow,
  NativeFormPickerRow,
  NativeFormProps,
  NativeFormRow,
  NativeFormTextRow,
  NativeFormTone,
} from "./NativeForm.types";
import type { NativeMenuAction } from "./NativeMenu";
import { useTheme, type OpenCuriaTheme } from "./theme";

const TRANSPARENT = "#00000000";

function toneColor(theme: OpenCuriaTheme, tone: NativeFormTone = "muted") {
  switch (tone) {
    case "default":
      return theme.text;
    case "accent":
      return theme.primary;
    case "success":
      return theme.success;
    case "warning":
      return theme.warning;
    case "danger":
      return theme.danger;
    default:
      return theme.mutedText;
  }
}

function Tile({
  symbol,
  tint,
  large,
}: {
  symbol?: string;
  tint: string;
  large?: boolean;
}): React.JSX.Element {
  const edge = large ? 64 : 40;
  return (
    <Box
      contentAlignment="center"
      modifiers={[
        size(edge, edge),
        clip(Shapes.RoundedCorner(large ? 20 : 12)),
        background(tint),
      ]}
    >
      <Icon
        source={androidIcon(symbol) ?? WORKSPACE_ICON}
        tint="#FFFFFF"
        size={large ? 32 : 22}
      />
    </Box>
  );
}

function ItemRow({ row }: { row: NativeFormItemRow }): React.JSX.Element {
  const { theme } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  // Material has no swipe actions; their actions join the row's menu.
  const menu: NativeMenuAction[] = [
    ...(row.leadingSwipe ?? []).filter(
      (action) => !row.actions?.some((entry) => entry.id === action.id),
    ),
    ...(row.actions ?? []),
    ...(row.trailingSwipe ?? []).filter(
      (action) => !row.actions?.some((entry) => entry.id === action.id),
    ),
  ].map((action) => ({
    id: action.id,
    title: action.title,
    destructive: action.destructive,
    disabled: "disabled" in action ? action.disabled : undefined,
  }));
  return (
    <ListItem
      colors={{
        containerColor: row.selected ? theme.surfaceMuted : TRANSPARENT,
      }}
      modifiers={[
        fillMaxWidth(),
        composeTestID(row.testID ?? row.id),
        semantics({
          contentDescription: [row.title, row.subtitle]
            .filter(Boolean)
            .join(", "),
        }),
        ...(!row.disabled && (row.onPress || menu.length)
          ? [
              combinedClickable({
                onClick: row.onPress,
                onLongClick: menu.length ? () => setMenuOpen(true) : undefined,
              }),
            ]
          : []),
      ]}
    >
      {row.symbol ? (
        <ListItem.LeadingContent>
          <Tile symbol={row.symbol} tint={row.tint ?? theme.primary} />
        </ListItem.LeadingContent>
      ) : null}
      <ListItem.HeadlineContent>
        <Text
          maxLines={1}
          overflow="ellipsis"
          style={{ fontWeight: row.selected ? "600" : "400" }}
        >
          {row.title}
        </Text>
      </ListItem.HeadlineContent>
      {row.subtitle ? (
        <ListItem.SupportingContent>
          <Text
            maxLines={1}
            overflow="ellipsis"
            color={toneColor(theme, row.subtitleTone)}
          >
            {row.subtitle}
          </Text>
        </ListItem.SupportingContent>
      ) : null}
      <ListItem.TrailingContent>
        <Row verticalAlignment="center">
          {row.busy ? (
            <CircularProgressIndicator modifiers={[size(18, 18)]} />
          ) : null}
          {row.badge ? (
            <Text
              color={theme.primary}
              style={{ typography: "labelMedium" }}
              modifiers={[padding(8, 0, 4, 0)]}
            >
              {row.badge}
            </Text>
          ) : null}
          {row.selected ? (
            <Icon source={androidIcon("checkmark")!} size={20} />
          ) : null}
          {/* Material rows keep one trailing action; details join the menu. */}
          {row.onInfo && !menu.length ? (
            <IconButton
              onClick={row.onInfo}
              modifiers={[
                semantics({
                  contentDescription:
                    row.infoLabel ?? `Details for ${row.title}`,
                }),
              ]}
            >
              <Icon source={androidIcon("info.circle")!} size={22} />
            </IconButton>
          ) : null}
          {menu.length ? (
            <DropdownMenu
              expanded={menuOpen}
              onDismissRequest={() => setMenuOpen(false)}
            >
              <DropdownMenu.Trigger>
                <IconButton
                  onClick={() => setMenuOpen(true)}
                  modifiers={[
                    semantics({
                      contentDescription: `Actions for ${row.title}`,
                    }),
                  ]}
                >
                  <Icon source={androidIcon("ellipsis")!} size={24} />
                </IconButton>
              </DropdownMenu.Trigger>
              <DropdownMenu.Items>
                {menu.map((action) => (
                  <DropdownMenuItem
                    key={action.id}
                    enabled={!action.disabled}
                    elementColors={
                      action.destructive
                        ? { textColor: theme.danger }
                        : undefined
                    }
                    onClick={() => {
                      setMenuOpen(false);
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

function HeaderRow({ row }: { row: NativeFormHeaderRow }): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Column
      horizontalAlignment="center"
      verticalArrangement={{ spacedBy: 10 }}
      modifiers={[fillMaxWidth(), padding(16, 8, 16, 16)]}
    >
      <Tile symbol={row.symbol} tint={row.tint} large />
      <Text style={{ typography: "headlineSmall", textAlign: "center" }}>
        {row.title}
      </Text>
      {row.subtitle ? (
        <Row verticalAlignment="center" horizontalArrangement={{ spacedBy: 6 }}>
          {row.busy ? (
            <CircularProgressIndicator modifiers={[size(14, 14)]} />
          ) : null}
          <Text
            color={toneColor(theme, row.subtitleTone)}
            style={{ typography: "bodyMedium" }}
          >
            {row.subtitle}
          </Text>
        </Row>
      ) : null}
      {row.actions.length ? (
        <Row
          horizontalArrangement={{ spacedBy: 12 }}
          modifiers={[padding(0, 6, 0, 0)]}
        >
          {row.actions.map((action) => (
            <Column
              key={action.id}
              horizontalAlignment="center"
              verticalArrangement={{ spacedBy: 4 }}
            >
              <FilledTonalIconButton
                onClick={() => row.onAction(action.id)}
                enabled={!action.disabled}
                modifiers={[
                  size(52, 52),
                  composeTestID(`${row.id}-${action.id}`),
                  semantics({ contentDescription: action.title }),
                ]}
              >
                <Icon
                  source={androidIcon(action.symbol) ?? WORKSPACE_ICON}
                  tint={action.destructive ? theme.danger : undefined}
                  size={22}
                />
              </FilledTonalIconButton>
              <Text
                style={{ typography: "labelMedium" }}
                color={action.disabled ? theme.subtleText : theme.text}
                maxLines={1}
              >
                {action.title}
              </Text>
            </Column>
          ))}
        </Row>
      ) : null}
    </Column>
  );
}

function TextRow({ row }: { row: NativeFormTextRow }): React.JSX.Element {
  const text = useNativeState(row.value);
  const latest = useRef(row.value);
  useEffect(() => {
    if (row.value !== latest.current) {
      latest.current = row.value;
      text.set(row.value);
    }
  }, [row.value, text]);
  return (
    <OutlinedTextField
      value={text}
      autoFocus={row.autoFocus}
      enabled={!row.disabled}
      singleLine={!row.multiline}
      minLines={row.multiline ? 2 : undefined}
      maxLines={row.multiline ? 6 : undefined}
      onValueChange={(value) => {
        latest.current = value;
        row.onChange(value);
      }}
      keyboardOptions={{
        capitalization: "none",
        autoCorrectEnabled: false,
        keyboardType: row.keyboard === "url" ? "uri" : "text",
        imeAction: "done",
      }}
      keyboardActions={row.onSubmit ? { onDone: row.onSubmit } : undefined}
      modifiers={[
        fillMaxWidth(),
        padding(16, 8, 16, 8),
        composeTestID(row.testID ?? row.id),
        semantics({ contentDescription: row.label }),
      ]}
    >
      <OutlinedTextField.Label>
        <Text>{row.label}</Text>
      </OutlinedTextField.Label>
      {row.placeholder ? (
        <OutlinedTextField.Placeholder>
          <Text>{row.placeholder}</Text>
        </OutlinedTextField.Placeholder>
      ) : null}
    </OutlinedTextField>
  );
}

function PickerRow({ row }: { row: NativeFormPickerRow }): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const selected = row.groups
    .flatMap((group) => group.options)
    .find((option) => option.value === row.value);
  return (
    <DropdownMenu expanded={open} onDismissRequest={() => setOpen(false)}>
      <DropdownMenu.Trigger>
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({
              contentDescription: `${row.label}, ${selected?.label ?? ""}`,
            }),
            ...(row.disabled ? [] : [clickable(() => setOpen(true))]),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text color={row.disabled ? theme.subtleText : undefined}>
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Text color={theme.mutedText} maxLines={1} overflow="ellipsis">
              {selected?.label ?? "Choose"}
            </Text>
          </ListItem.SupportingContent>
          <ListItem.TrailingContent>
            <Icon
              source={require("../../assets/native-icons/expand-more.xml")}
              size={24}
            />
          </ListItem.TrailingContent>
        </ListItem>
      </DropdownMenu.Trigger>
      <DropdownMenu.Items>
        {row.groups.flatMap((group, index) => [
          ...(group.title
            ? [
                <DropdownMenuItem
                  key={`group-${index}`}
                  enabled={false}
                  elementColors={{ disabledTextColor: theme.primary }}
                >
                  <DropdownMenuItem.Text>
                    <Text style={{ typography: "labelMedium" }}>
                      {group.title}
                    </Text>
                  </DropdownMenuItem.Text>
                </DropdownMenuItem>,
              ]
            : []),
          ...group.options.map((option) => (
            <DropdownMenuItem
              key={`${index}-${option.value}`}
              enabled={!option.disabled}
              onClick={() => {
                setOpen(false);
                row.onChange(option.value);
              }}
            >
              <DropdownMenuItem.Text>
                <Text
                  style={{
                    fontWeight: option.value === row.value ? "600" : "400",
                  }}
                >
                  {option.label}
                </Text>
              </DropdownMenuItem.Text>
            </DropdownMenuItem>
          )),
        ])}
      </DropdownMenu.Items>
    </DropdownMenu>
  );
}

function FormRow({ row }: { row: NativeFormRow }): React.JSX.Element {
  const { theme } = useTheme();
  switch (row.kind) {
    case "item":
      return <ItemRow row={row} />;
    case "header":
      return <HeaderRow row={row} />;
    case "text":
      return <TextRow row={row} />;
    case "picker":
      return <PickerRow row={row} />;
    case "toggle":
      return (
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({
              contentDescription: [row.label, row.subtitle]
                .filter(Boolean)
                .join(", "),
            }),
            ...(row.disabled
              ? []
              : [clickable(() => row.onChange(!row.value))]),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text color={row.disabled ? theme.subtleText : undefined}>
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          {row.subtitle ? (
            <ListItem.SupportingContent>
              <Text color={toneColor(theme, row.subtitleTone)}>
                {row.subtitle}
              </Text>
            </ListItem.SupportingContent>
          ) : null}
          <ListItem.TrailingContent>
            <Switch
              value={row.value}
              enabled={!row.disabled}
              onCheckedChange={row.onChange}
            />
          </ListItem.TrailingContent>
        </ListItem>
      );
    case "stepper":
      return (
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({
              contentDescription: `${row.label}, ${row.format(row.value)}`,
            }),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text>{row.label}</Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Text color={theme.mutedText}>{row.format(row.value)}</Text>
          </ListItem.SupportingContent>
          <ListItem.TrailingContent>
            <Row verticalAlignment="center">
              <FilledTonalIconButton
                enabled={!row.disabled && row.value > row.min}
                onClick={() =>
                  row.onChange(Math.max(row.min, row.value - row.step))
                }
                modifiers={[
                  semantics({ contentDescription: `Decrease ${row.label}` }),
                ]}
              >
                <Icon source={androidIcon("minus")!} size={20} />
              </FilledTonalIconButton>
              <FilledTonalIconButton
                enabled={!row.disabled && row.value < row.max}
                onClick={() =>
                  row.onChange(Math.min(row.max, row.value + row.step))
                }
                modifiers={[
                  semantics({ contentDescription: `Increase ${row.label}` }),
                ]}
              >
                <Icon source={androidIcon("plus")!} size={20} />
              </FilledTonalIconButton>
            </Row>
          </ListItem.TrailingContent>
        </ListItem>
      );
    case "value":
      return (
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[fillMaxWidth()]}
        >
          <ListItem.HeadlineContent>
            <Text>{row.label}</Text>
          </ListItem.HeadlineContent>
          <ListItem.TrailingContent>
            <Text color={toneColor(theme, row.tone)} maxLines={2}>
              {row.value}
            </Text>
          </ListItem.TrailingContent>
        </ListItem>
      );
    case "gauge":
      return (
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[
            fillMaxWidth(),
            semantics({
              contentDescription: `${row.label}, ${row.valueLabel}`,
            }),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text>{row.label}</Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Column
              verticalArrangement={{ spacedBy: 6 }}
              modifiers={[padding(0, 6, 0, 0)]}
            >
              <LinearProgressIndicator
                progress={Math.min(1, Math.max(0, row.value))}
                color={
                  row.tone === "danger" || row.tone === "warning"
                    ? toneColor(theme, row.tone)
                    : theme.primary
                }
                modifiers={[fillMaxWidth()]}
              />
              <Text
                color={toneColor(
                  theme,
                  row.tone === "default" ? "muted" : row.tone,
                )}
              >
                {row.valueLabel}
              </Text>
            </Column>
          </ListItem.SupportingContent>
        </ListItem>
      );
    case "button": {
      const color = row.disabled
        ? theme.subtleText
        : row.destructive
          ? theme.danger
          : theme.primary;
      const icon = androidIcon(row.symbol);
      return (
        <ListItem
          colors={{ containerColor: TRANSPARENT }}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({ contentDescription: row.title }),
            ...(row.disabled || row.busy ? [] : [clickable(row.onPress)]),
          ]}
        >
          {icon ? (
            <ListItem.LeadingContent>
              <Icon source={icon} tint={color} size={22} />
            </ListItem.LeadingContent>
          ) : null}
          <ListItem.HeadlineContent>
            <Text color={color} style={{ fontWeight: "500" }}>
              {row.title}
            </Text>
          </ListItem.HeadlineContent>
          {row.busy ? (
            <ListItem.TrailingContent>
              <CircularProgressIndicator modifiers={[size(18, 18)]} />
            </ListItem.TrailingContent>
          ) : null}
        </ListItem>
      );
    }
    case "note":
      return (
        <Text
          color={toneColor(theme, row.tone)}
          style={{ typography: "bodySmall" }}
          modifiers={[padding(16, 10, 16, 10)]}
        >
          {row.text}
        </Text>
      );
  }
}

/** Material 3 grouped settings list: section labels, tonal cards and native controls. */
export function NativeForm({
  sections,
  onRefresh,
  empty,
  contentInsetBottom,
  testID,
}: NativeFormProps): React.JSX.Element {
  const { theme } = useTheme();
  const [refreshing, setRefreshing] = useState(false);
  const list = (
    <LazyColumn
      modifiers={[fillMaxSize()]}
      contentPadding={{ top: 4, bottom: 24 + (contentInsetBottom ?? 0) }}
    >
      {sections.map((section) => {
        // The hero header sits on the screen background, not in a card.
        const header = section.rows.filter((row) => row.kind === "header");
        const rows = section.rows.filter((row) => row.kind !== "header");
        return (
          <Column key={section.id} modifiers={[fillMaxWidth()]}>
            {header.map((row) => (
              <FormRow key={row.id} row={row} />
            ))}
            {section.title ? (
              <Text
                color={theme.primary}
                style={{ typography: "titleSmall" }}
                modifiers={[padding(28, 16, 28, 8)]}
              >
                {section.title}
              </Text>
            ) : null}
            {rows.length ? (
              <Card
                colors={{ containerColor: theme.surfaceMuted }}
                modifiers={[fillMaxWidth(), padding(16, 4, 16, 4)]}
              >
                <Column modifiers={[fillMaxWidth(), padding(0, 4, 0, 4)]}>
                  {rows.map((row, index) => (
                    <Column key={row.id} modifiers={[fillMaxWidth()]}>
                      {index ? (
                        <HorizontalDivider
                          color={theme.border}
                          modifiers={[padding(16, 0, 16, 0)]}
                        />
                      ) : null}
                      <FormRow row={row} />
                    </Column>
                  ))}
                </Column>
              </Card>
            ) : null}
            {section.footer ? (
              <Text
                color={toneColor(theme, section.footerTone)}
                style={{ typography: "bodySmall" }}
                modifiers={[padding(28, 6, 28, 4)]}
              >
                {section.footer}
              </Text>
            ) : null}
          </Column>
        );
      })}
    </LazyColumn>
  );
  return (
    <View style={{ flex: 1 }} testID={testID}>
      <Host
        style={{ flex: 1 }}
        colorScheme={theme.scheme}
        seedColor={theme.primary}
        ignoreSafeAreaKeyboardInsets
      >
        {!sections.length && empty ? (
          <Column
            horizontalAlignment="center"
            verticalArrangement="center"
            modifiers={[fillMaxSize(), padding(32, 32, 32, 32)]}
          >
            <Tile symbol={empty.symbol} tint={theme.subtleText} large />
            <Text
              style={{ typography: "titleMedium" }}
              modifiers={[padding(0, 16, 0, 4)]}
            >
              {empty.title}
            </Text>
            {empty.description ? (
              <Text
                color={theme.mutedText}
                style={{ typography: "bodyMedium", textAlign: "center" }}
              >
                {empty.description}
              </Text>
            ) : null}
          </Column>
        ) : onRefresh ? (
          <PullToRefreshBox
            isRefreshing={refreshing}
            onRefresh={() => {
              setRefreshing(true);
              void onRefresh().finally(() => setRefreshing(false));
            }}
            modifiers={[fillMaxSize()]}
          >
            {list}
          </PullToRefreshBox>
        ) : (
          list
        )}
      </Host>
    </View>
  );
}
