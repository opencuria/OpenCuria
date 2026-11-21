import React, { useEffect, useRef, useState } from "react";
import { View } from "react-native";
import {
  Box,
  Button,
  CircularProgressIndicator,
  Column,
  DatePickerDialog,
  DropdownMenu,
  DropdownMenuItem,
  FilledTonalIconButton,
  Host,
  Icon,
  IconButton,
  Image,
  LazyColumn,
  LinearProgressIndicator,
  ListItem,
  OutlinedTextField,
  PullToRefreshBox,
  Row,
  Shape,
  Switch,
  Text,
  TimePickerDialog,
  useNativeState,
} from "@expo/ui/jetpack-compose";
import {
  Shapes,
  background,
  clickable,
  clip,
  combinedClickable,
  defaultMinSize,
  fillMaxSize,
  fillMaxWidth,
  padding,
  semantics,
  size,
  testID as composeTestID,
  toggleable,
  tween,
  weight,
  width,
} from "@expo/ui/jetpack-compose/modifiers";
import { WORKSPACE_ICON, androidIcon } from "./androidIcons";
import { useFormNavigation } from "./formNavigation";
import type {
  NativeFormAccessory,
  NativeFormCardRow,
  NativeFormDateRow,
  NativeFormEmptyState,
  NativeFormHeaderRow,
  NativeFormItemRow,
  NativeFormLinkRow,
  NativeFormPickerRow,
  NativeFormProfileRow,
  NativeFormProps,
  NativeFormRow,
  NativeFormSection,
  NativeFormStatus,
  NativeFormTextRow,
  NativeFormTone,
  NativeFormValueRow,
  NativeFormWeekdaysRow,
} from "./NativeForm.types";
import {
  fieldSelectionColors,
  outlinedFieldColors,
  outlinedFieldShape,
} from "./NativeTextField.android";
import { SegmentedChoiceRow } from "./selection.android";
import { statusColor } from "./statusColors";
import { useTheme, type OpenCuriaTheme } from "./theme";
import {
  WEEKDAY_TOGGLES,
  formatWallClock,
  toggleWeekday,
  uses24HourClock,
  wallClockDate,
  wallClockValue,
} from "./wallClock";

const TRANSPARENT = "#00000000";

/** Outer corners of a segmented group and the small corners between its rows. */
const OUTER_RADIUS = 20;
const INNER_RADIUS = 4;

/** Capsule clip: Compose caps corner radii at half the height. */
const CAPSULE = Shapes.RoundedCorner(100);

type MenuItemColors = React.ComponentProps<
  typeof DropdownMenuItem
>["elementColors"];

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

/** Ink list item colors on a segment; never the seeded palette. */
function rowColors(theme: OpenCuriaTheme) {
  return {
    containerColor: TRANSPARENT,
    contentColor: theme.text,
    leadingContentColor: theme.text,
    trailingContentColor: theme.text,
    supportingContentColor: theme.mutedText,
    overlineContentColor: theme.mutedText,
  };
}

/** Ink menu entries; destructive ones in the danger color. */
function menuItemColors(
  theme: OpenCuriaTheme,
  destructive?: boolean,
): MenuItemColors {
  const color = destructive ? theme.danger : theme.text;
  return {
    textColor: color,
    leadingIconColor: color,
    trailingIconColor: color,
    disabledTextColor: theme.subtleText,
    disabledLeadingIconColor: theme.subtleText,
    disabledTrailingIconColor: theme.subtleText,
  };
}

/** Switch in ink: a filled track when on, an outlined neutral one when off. */
function switchColors(theme: OpenCuriaTheme) {
  return {
    checkedTrackColor: theme.primary,
    checkedThumbColor: theme.primaryForeground,
    checkedBorderColor: TRANSPARENT,
    checkedIconColor: theme.primary,
    uncheckedTrackColor: theme.controlFill,
    uncheckedThumbColor: theme.subtleText,
    uncheckedBorderColor: theme.subtleText,
    uncheckedIconColor: theme.controlFill,
    disabledCheckedTrackColor: theme.subtleText,
    disabledCheckedThumbColor: theme.card,
    disabledCheckedBorderColor: TRANSPARENT,
    disabledCheckedIconColor: theme.subtleText,
    disabledUncheckedTrackColor: theme.controlFill,
    disabledUncheckedThumbColor: theme.separator,
    disabledUncheckedBorderColor: theme.separator,
    disabledUncheckedIconColor: theme.controlFill,
  };
}

/** Monochrome date and time dialogs: ink selection on the menu surface. */
function pickerColors(theme: OpenCuriaTheme) {
  return {
    containerColor: theme.raised,
    titleContentColor: theme.mutedText,
    headlineContentColor: theme.text,
    weekdayContentColor: theme.mutedText,
    subheadContentColor: theme.mutedText,
    navigationContentColor: theme.text,
    yearContentColor: theme.text,
    disabledYearContentColor: theme.subtleText,
    currentYearContentColor: theme.text,
    selectedYearContentColor: theme.primaryForeground,
    selectedYearContainerColor: theme.primary,
    dayContentColor: theme.text,
    disabledDayContentColor: theme.subtleText,
    selectedDayContentColor: theme.primaryForeground,
    selectedDayContainerColor: theme.primary,
    todayContentColor: theme.text,
    todayDateBorderColor: theme.text,
    dividerColor: theme.separator,
    clockDialColor: theme.controlFill,
    clockDialSelectedContentColor: theme.primaryForeground,
    clockDialUnselectedContentColor: theme.text,
    selectorColor: theme.primary,
    periodSelectorBorderColor: theme.separator,
    periodSelectorSelectedContainerColor: theme.primary,
    periodSelectorUnselectedContainerColor: TRANSPARENT,
    periodSelectorSelectedContentColor: theme.primaryForeground,
    periodSelectorUnselectedContentColor: theme.mutedText,
    timeSelectorSelectedContainerColor: theme.primary,
    timeSelectorSelectedContentColor: theme.primaryForeground,
    timeSelectorUnselectedContainerColor: theme.controlFill,
    timeSelectorUnselectedContentColor: theme.text,
  };
}

/** TalkBack name and test handle of a control, kept while it shows a spinner. */
function controlSemantics(label: string, testID?: string) {
  return [
    ...(testID ? [composeTestID(testID)] : []),
    semantics({ contentDescription: label }),
  ];
}

/** A small indeterminate spinner, muted unless a color is given. */
function Spinner({
  edge = 18,
  stroke = 2,
  color,
}: {
  edge?: number;
  stroke?: number;
  color?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <CircularProgressIndicator
      color={color ?? theme.mutedText}
      trackColor={TRANSPARENT}
      strokeWidth={stroke}
      modifiers={[size(edge, edge)]}
    />
  );
}

/**
 * A symbol on a neutral rounded tile, or a small image filling it: the
 * identity of a workspace or app in a row (40 dp) or a hero header (72 dp).
 */
function Tile({
  symbol,
  image,
  edge,
  radius,
  fill,
  iconSize,
}: {
  symbol?: string;
  image?: string;
  edge: number;
  radius: number;
  fill: string;
  iconSize: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  const shape = Shapes.RoundedCorner(radius);
  if (image)
    return (
      <Image
        source={{ uri: image }}
        contentScale="crop"
        contentDescription={null}
        modifiers={[size(edge, edge), clip(shape), background(fill)]}
      />
    );
  return (
    <Box
      contentAlignment="center"
      modifiers={[size(edge, edge), clip(shape), background(fill)]}
    >
      <Icon
        source={androidIcon(symbol) ?? WORKSPACE_ICON}
        tint={theme.text}
        size={iconSize}
      />
    </Box>
  );
}

/** The small colored dot before a status line; the text names the status. */
function StatusDot({
  status,
}: {
  status: NativeFormStatus;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Box
      modifiers={[
        size(7, 7),
        clip(Shapes.Circle),
        background(statusColor(status, theme)),
      ]}
    />
  );
}

/** Secondary line led by a status dot, or by a small spinner while busy. */
function StatusLine({
  text,
  tone,
  status,
  busy,
  centered,
}: {
  text: string;
  tone?: NativeFormTone;
  status?: NativeFormStatus;
  busy?: boolean;
  centered?: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Row verticalAlignment="center" horizontalArrangement={{ spacedBy: 6 }}>
      {busy ? (
        <Spinner edge={14} />
      ) : status ? (
        <StatusDot status={status} />
      ) : null}
      <Text
        color={toneColor(theme, tone)}
        maxLines={centered ? 2 : 1}
        overflow="ellipsis"
        style={{
          typography: "bodyMedium",
          ...(centered ? { textAlign: "center" as const } : {}),
        }}
      >
        {text}
      </Text>
    </Row>
  );
}

/** A plain Material icon in ink, when one is bundled. */
function LeadingIcon({
  symbol,
}: {
  symbol?: string;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const icon = androidIcon(symbol);
  return icon ? <Icon source={icon} tint={theme.text} size={24} /> : null;
}

/**
 * A trailing capsule with its own click target, as in Google Play: ink for
 * the action that changes state, a neutral fill otherwise.
 */
function AccessoryButton({
  accessory,
  inactive,
}: {
  accessory: NativeFormAccessory;
  inactive?: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  const label = accessory.accessibilityLabel ?? accessory.title;
  if (accessory.busy)
    return (
      <Box
        contentAlignment="center"
        modifiers={[
          defaultMinSize({ minWidth: 48, minHeight: 48 }),
          ...controlSemantics(label, accessory.testID),
        ]}
      >
        <Spinner edge={20} />
      </Box>
    );
  return (
    <Button
      onClick={accessory.onPress}
      enabled={!(inactive || accessory.disabled)}
      shape={Shape.Pill({})}
      colors={{
        containerColor: accessory.prominent ? theme.primary : theme.controlFill,
        contentColor: accessory.prominent
          ? theme.primaryForeground
          : theme.text,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      }}
      contentPadding={{ start: 16, top: 6, end: 16, bottom: 6 }}
      modifiers={[
        defaultMinSize({ minHeight: 36 }),
        ...controlSemantics(label, accessory.testID),
      ]}
    >
      <Text
        maxLines={1}
        style={{ typography: "labelLarge", fontWeight: "600" }}
      >
        {accessory.title}
      </Text>
    </Button>
  );
}

function ItemRow({ row }: { row: NativeFormItemRow }): React.JSX.Element {
  const { theme } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  // Material has no swipe actions; their actions join the row's menu.
  const menu = [
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
    icon: androidIcon(action.symbol),
    destructive: action.destructive,
    disabled: "disabled" in action ? action.disabled : undefined,
  }));
  // Icons only when every entry has one, so the labels stay aligned.
  const menuIcons = menu.every((action) => action.icon);
  const leadingColor = toneColor(theme, row.leadingTone ?? "default");
  const badge = row.badge && !row.accessory ? row.badge : undefined;
  // Material rows keep one trailing action; details join the menu.
  const info = row.onInfo && !menu.length ? row.onInfo : undefined;
  const trailing = Boolean(
    row.busy || badge || row.accessory || row.selected || info || menu.length,
  );
  return (
    <ListItem
      colors={rowColors(theme)}
      modifiers={[
        fillMaxWidth(),
        composeTestID(row.testID ?? row.id),
        semantics({
          contentDescription: [
            [row.leadingText, row.leadingCaption].filter(Boolean).join(" "),
            row.title,
            row.subtitle,
          ]
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
      {row.leadingText ? (
        <ListItem.LeadingContent>
          {/* Roboto figures are tabular, so a column of times aligns. */}
          <Row
            verticalAlignment="bottom"
            horizontalArrangement={{ spacedBy: 3 }}
            modifiers={[
              defaultMinSize({ minWidth: row.leadingCaption ? 92 : 72 }),
            ]}
          >
            <Text
              color={leadingColor}
              maxLines={1}
              softWrap={false}
              style={{ typography: "titleLarge", fontWeight: "500" }}
            >
              {row.leadingText}
            </Text>
            {row.leadingCaption ? (
              <Text
                color={leadingColor}
                maxLines={1}
                softWrap={false}
                style={{ typography: "labelMedium", fontWeight: "600" }}
                modifiers={[padding(0, 0, 0, 3)]}
              >
                {row.leadingCaption}
              </Text>
            ) : null}
          </Row>
        </ListItem.LeadingContent>
      ) : row.image || (row.symbol && row.tile) ? (
        <ListItem.LeadingContent>
          <Tile
            symbol={row.symbol}
            image={row.image}
            edge={40}
            radius={12}
            fill={theme.controlFill}
            iconSize={22}
          />
        </ListItem.LeadingContent>
      ) : row.symbol ? (
        <ListItem.LeadingContent>
          <Icon
            source={androidIcon(row.symbol) ?? WORKSPACE_ICON}
            tint={
              row.symbolStatus
                ? statusColor(row.symbolStatus, theme)
                : theme.text
            }
            size={24}
          />
        </ListItem.LeadingContent>
      ) : null}
      <ListItem.HeadlineContent>
        <Text
          color={row.disabled ? theme.subtleText : theme.text}
          maxLines={1}
          overflow="ellipsis"
          style={{
            typography: "bodyLarge",
            fontWeight: row.selected ? "600" : "400",
          }}
        >
          {row.title}
        </Text>
      </ListItem.HeadlineContent>
      {row.subtitle ? (
        <ListItem.SupportingContent>
          <StatusLine
            text={row.subtitle}
            tone={row.subtitleTone}
            status={row.status}
          />
        </ListItem.SupportingContent>
      ) : null}
      {trailing ? (
        <ListItem.TrailingContent>
          <Row
            verticalAlignment="center"
            horizontalArrangement={{ spacedBy: 4 }}
          >
            {row.busy ? <Spinner /> : null}
            {badge ? (
              <Text
                color={theme.text}
                maxLines={1}
                style={{ typography: "labelMedium", fontWeight: "600" }}
                modifiers={[
                  clip(CAPSULE),
                  background(theme.controlFill),
                  padding(8, 3, 8, 3),
                ]}
              >
                {badge}
              </Text>
            ) : null}
            {row.accessory ? (
              <AccessoryButton
                accessory={row.accessory}
                inactive={row.disabled}
              />
            ) : null}
            {row.selected ? (
              <Icon
                source={androidIcon("checkmark")!}
                tint={theme.text}
                size={22}
              />
            ) : null}
            {info ? (
              <IconButton
                onClick={info}
                colors={{
                  contentColor: theme.text,
                  disabledContentColor: theme.subtleText,
                }}
                modifiers={[
                  semantics({
                    contentDescription:
                      row.infoLabel ?? `Details for ${row.title}`,
                  }),
                ]}
              >
                <Icon
                  source={androidIcon("info.circle")!}
                  tint={theme.text}
                  size={22}
                />
              </IconButton>
            ) : null}
            {menu.length ? (
              <DropdownMenu
                expanded={menuOpen}
                color={theme.raised}
                onDismissRequest={() => setMenuOpen(false)}
              >
                <DropdownMenu.Trigger>
                  <IconButton
                    onClick={() => setMenuOpen(true)}
                    colors={{ contentColor: theme.text }}
                    modifiers={[
                      semantics({
                        contentDescription: `Actions for ${row.title}`,
                      }),
                    ]}
                  >
                    <Icon
                      source={androidIcon("ellipsis")!}
                      tint={theme.text}
                      size={24}
                    />
                  </IconButton>
                </DropdownMenu.Trigger>
                <DropdownMenu.Items>
                  {menu.map((action) => (
                    <DropdownMenuItem
                      key={action.id}
                      enabled={!action.disabled}
                      elementColors={menuItemColors(theme, action.destructive)}
                      onClick={() => {
                        setMenuOpen(false);
                        row.onAction?.(action.id);
                      }}
                    >
                      {menuIcons && action.icon ? (
                        <DropdownMenuItem.LeadingIcon>
                          <Icon source={action.icon} size={20} />
                        </DropdownMenuItem.LeadingIcon>
                      ) : null}
                      <DropdownMenuItem.Text>
                        <Text style={{ typography: "labelLarge" }}>
                          {action.title}
                        </Text>
                      </DropdownMenuItem.Text>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenu.Items>
              </DropdownMenu>
            ) : null}
          </Row>
        </ListItem.TrailingContent>
      ) : null}
    </ListItem>
  );
}

/** Hero header on the canvas with tonal quick actions, as in Contacts. */
function HeaderRow({
  row,
  top,
}: {
  row: NativeFormHeaderRow;
  top: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Column
      horizontalAlignment="center"
      verticalArrangement={{ spacedBy: 12 }}
      modifiers={[fillMaxWidth(), padding(16, top, 16, 8)]}
    >
      <Tile
        symbol={row.symbol}
        image={row.image}
        edge={72}
        radius={20}
        fill={theme.card}
        iconSize={32}
      />
      <Column
        horizontalAlignment="center"
        verticalArrangement={{ spacedBy: 4 }}
      >
        <Text
          color={theme.text}
          maxLines={2}
          overflow="ellipsis"
          style={{
            typography: "headlineSmall",
            fontWeight: "600",
            textAlign: "center",
          }}
        >
          {row.title}
        </Text>
        {row.subtitle ? (
          <StatusLine
            text={row.subtitle}
            tone={row.subtitleTone}
            status={row.status}
            busy={row.busy}
            centered
          />
        ) : null}
      </Column>
      {row.actions.length ? (
        // Fixed columns keep five actions on a 360 dp phone.
        <Row
          horizontalArrangement={{
            spacedBy: row.actions.length >= 5 ? 0 : 8,
          }}
          modifiers={[padding(0, 4, 0, 0)]}
        >
          {row.actions.map((action) => (
            <Column
              key={action.id}
              horizontalAlignment="center"
              verticalArrangement={{ spacedBy: 6 }}
              modifiers={[width(64)]}
            >
              <FilledTonalIconButton
                onClick={() => row.onAction(action.id)}
                enabled={!action.disabled}
                colors={{
                  containerColor: theme.card,
                  contentColor: theme.text,
                  disabledContainerColor: theme.card,
                  disabledContentColor: theme.subtleText,
                }}
                modifiers={[
                  size(52, 52),
                  composeTestID(`${row.id}-${action.id}`),
                  semantics({ contentDescription: action.title }),
                ]}
              >
                <Icon
                  source={androidIcon(action.symbol) ?? WORKSPACE_ICON}
                  tint={
                    action.disabled
                      ? theme.subtleText
                      : action.destructive
                        ? theme.danger
                        : theme.text
                  }
                  size={22}
                />
              </FilledTonalIconButton>
              <Text
                color={action.disabled ? theme.subtleText : theme.text}
                maxLines={1}
                overflow="ellipsis"
                style={{ typography: "labelMedium", textAlign: "center" }}
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
  const { theme } = useTheme();
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
      minLines={row.multiline ? (row.lines?.[0] ?? 2) : undefined}
      maxLines={row.multiline ? (row.lines?.[1] ?? 6) : undefined}
      onValueChange={(value) => {
        latest.current = value;
        row.onChange(value);
      }}
      shape={outlinedFieldShape()}
      colors={outlinedFieldColors(theme)}
      textSelectionColors={fieldSelectionColors(theme)}
      textStyle={row.monospace ? { fontFamily: "monospace" } : undefined}
      visualTransformation={row.secure ? "password" : "none"}
      keyboardOptions={{
        capitalization: row.prose ? "sentences" : "none",
        autoCorrectEnabled: Boolean(row.prose),
        keyboardType: row.secure
          ? "password"
          : row.keyboard === "url"
            ? "uri"
            : row.keyboard === "email"
              ? "email"
              : row.keyboard === "number"
                ? "number"
                : "text",
        // Enter starts a new line in multiline prose.
        imeAction: row.multiline && row.prose ? "default" : "done",
      }}
      keyboardActions={row.onSubmit ? { onDone: row.onSubmit } : undefined}
      modifiers={[
        fillMaxWidth(),
        padding(16, 8, 16, 14),
        composeTestID(row.testID ?? row.id),
        semantics({ contentDescription: row.label }),
      ]}
    >
      {/* Unstyled, so Material shrinks the label into the outline. */}
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
    <DropdownMenu
      expanded={open}
      color={theme.raised}
      onDismissRequest={() => setOpen(false)}
      modifiers={[fillMaxWidth()]}
    >
      <DropdownMenu.Trigger>
        <ListItem
          colors={rowColors(theme)}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({
              contentDescription: `${row.label}, ${selected?.label ?? ""}`,
            }),
            ...(row.disabled ? [] : [clickable(() => setOpen(true))]),
          ]}
        >
          {row.symbol ? (
            <ListItem.LeadingContent>
              <LeadingIcon symbol={row.symbol} />
            </ListItem.LeadingContent>
          ) : null}
          <ListItem.HeadlineContent>
            <Text
              color={row.disabled ? theme.subtleText : theme.text}
              style={{ typography: "bodyLarge" }}
            >
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Text
              color={theme.mutedText}
              maxLines={1}
              overflow="ellipsis"
              style={{ typography: "bodyMedium" }}
            >
              {selected?.label ?? "Choose"}
            </Text>
          </ListItem.SupportingContent>
          <ListItem.TrailingContent>
            <Icon
              source={require("../../assets/native-icons/expand-more.xml")}
              tint={theme.mutedText}
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
                  elementColors={{ disabledTextColor: theme.mutedText }}
                >
                  <DropdownMenuItem.Text>
                    <Text
                      style={{ typography: "labelMedium", fontWeight: "600" }}
                    >
                      {group.title}
                    </Text>
                  </DropdownMenuItem.Text>
                </DropdownMenuItem>,
              ]
            : []),
          ...group.options.map((option) => {
            const chosen = option.value === row.value;
            return (
              <DropdownMenuItem
                key={`${index}-${option.value}`}
                enabled={!option.disabled}
                elementColors={menuItemColors(theme)}
                onClick={() => {
                  setOpen(false);
                  row.onChange(option.value);
                }}
              >
                <DropdownMenuItem.Text>
                  <Text
                    style={{
                      typography: "labelLarge",
                      fontWeight: chosen ? "600" : "400",
                    }}
                  >
                    {option.label}
                  </Text>
                </DropdownMenuItem.Text>
                {chosen ? (
                  <DropdownMenuItem.TrailingIcon>
                    <Icon source={androidIcon("checkmark")!} size={20} />
                  </DropdownMenuItem.TrailingIcon>
                ) : null}
              </DropdownMenuItem>
            );
          }),
        ])}
      </DropdownMenu.Items>
    </DropdownMenu>
  );
}

/** A row that opens another choice, like a picker: label, value, chevron. */
function NavigationRow({
  row,
}: {
  row: NativeFormValueRow & { onPress: () => void };
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <ListItem
      colors={rowColors(theme)}
      modifiers={[
        fillMaxWidth(),
        composeTestID(row.testID ?? row.id),
        semantics({ contentDescription: `${row.label}, ${row.value}` }),
        ...(row.disabled ? [] : [clickable(row.onPress)]),
      ]}
    >
      {row.symbol ? (
        <ListItem.LeadingContent>
          <LeadingIcon symbol={row.symbol} />
        </ListItem.LeadingContent>
      ) : null}
      <ListItem.HeadlineContent>
        <Text
          color={row.disabled ? theme.subtleText : theme.text}
          style={{ typography: "bodyLarge" }}
        >
          {row.label}
        </Text>
      </ListItem.HeadlineContent>
      <ListItem.SupportingContent>
        <Text
          color={toneColor(theme, row.tone)}
          maxLines={1}
          overflow="ellipsis"
          style={{ typography: "bodyMedium" }}
        >
          {row.value}
        </Text>
      </ListItem.SupportingContent>
      <ListItem.TrailingContent>
        <Icon
          source={androidIcon("chevron.right")!}
          tint={theme.subtleText}
          size={24}
        />
      </ListItem.TrailingContent>
    </ListItem>
  );
}

/** Seven round day toggles, as in the Clock app's alarm editor. */
function WeekdaysRow({
  row,
}: {
  row: NativeFormWeekdaysRow;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Column
      verticalArrangement={{ spacedBy: 12 }}
      modifiers={[fillMaxWidth(), padding(16, 12, 16, 14)]}
    >
      <Row verticalAlignment="center" modifiers={[fillMaxWidth()]}>
        <Text
          color={row.disabled ? theme.subtleText : theme.text}
          style={{ typography: "bodyLarge" }}
          modifiers={[weight(1)]}
        >
          {row.label}
        </Text>
        {row.summary ? (
          <Text color={theme.mutedText} style={{ typography: "bodyMedium" }}>
            {row.summary}
          </Text>
        ) : null}
      </Row>
      <Row
        horizontalArrangement="spaceBetween"
        verticalAlignment="center"
        modifiers={[fillMaxWidth()]}
      >
        {WEEKDAY_TOGGLES.map((day) => {
          const on = row.value.includes(day.value);
          return (
            <Box
              key={day.value}
              contentAlignment="center"
              modifiers={[
                size(38, 38),
                clip(Shapes.Circle),
                background(on ? theme.primary : theme.controlFill, {
                  animationSpec: tween({ durationMillis: 150 }),
                }),
                ...(row.disabled
                  ? []
                  : [
                      toggleable(
                        on,
                        () => row.onChange(toggleWeekday(row.value, day.value)),
                        { role: "checkbox" },
                      ),
                    ]),
                semantics({ contentDescription: day.name }),
                composeTestID(`${row.testID ?? row.id}-${day.value}`),
              ]}
            >
              <Text
                color={
                  on
                    ? theme.primaryForeground
                    : row.disabled
                      ? theme.subtleText
                      : theme.text
                }
                style={{ fontSize: 15, fontWeight: "600" }}
              >
                {day.short}
              </Text>
            </Box>
          );
        })}
      </Row>
    </Column>
  );
}

/**
 * The value opens Material's date or time picker dialog. Material dates are
 * UTC midnights; times are device-local, like the pickers' own calendars.
 */
function DateRow({ row }: { row: NativeFormDateRow }): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const shown = formatWallClock(row.mode, row.value);
  const close = () => setOpen(false);
  return (
    <>
      <ListItem
        colors={rowColors(theme)}
        modifiers={[
          fillMaxWidth(),
          composeTestID(row.testID ?? row.id),
          semantics({ contentDescription: `${row.label}, ${shown}` }),
          ...(row.disabled ? [] : [clickable(() => setOpen(true))]),
        ]}
      >
        <ListItem.HeadlineContent>
          <Text
            color={row.disabled ? theme.subtleText : theme.text}
            style={{ typography: "bodyLarge" }}
          >
            {row.label}
          </Text>
        </ListItem.HeadlineContent>
        <ListItem.TrailingContent>
          <Row verticalAlignment="center">
            <Text
              color={row.disabled ? theme.subtleText : theme.text}
              style={{ typography: "titleMedium" }}
            >
              {shown}
            </Text>
            {row.onRemove ? (
              <IconButton
                onClick={row.onRemove}
                enabled={!row.disabled}
                colors={{
                  contentColor: theme.mutedText,
                  disabledContentColor: theme.subtleText,
                }}
                modifiers={[
                  semantics({ contentDescription: `Remove ${row.label}` }),
                  composeTestID(`${row.testID ?? row.id}-remove`),
                ]}
              >
                <Icon source={androidIcon("xmark")!} size={20} />
              </IconButton>
            ) : null}
          </Row>
        </ListItem.TrailingContent>
      </ListItem>
      {open && row.mode === "date" ? (
        <DatePickerDialog
          initialDate={`${row.value}T00:00:00Z`}
          selectableDates={
            row.minimumDate
              ? { start: new Date(`${row.minimumDate}T00:00:00Z`) }
              : undefined
          }
          // The tint also colors the dialog's buttons.
          color={theme.primary}
          elementColors={pickerColors(theme)}
          // Text input mode keeps seeded colors that `elementColors` can't reach.
          showVariantToggle={false}
          onDateSelected={(date) => {
            close();
            const value = date.toISOString().slice(0, 10);
            if (value !== row.value) row.onChange(value);
          }}
          onDismissRequest={close}
        />
      ) : null}
      {open && row.mode === "time" ? (
        <TimePickerDialog
          initialDate={wallClockDate("time", row.value).toISOString()}
          is24Hour={uses24HourClock()}
          color={theme.primary}
          elementColors={pickerColors(theme)}
          onDateSelected={(date) => {
            close();
            const value = wallClockValue("time", date);
            if (value !== row.value) row.onChange(value);
          }}
          onDismissRequest={close}
        />
      ) : null}
    </>
  );
}

/** Opens another settings page: icon, title, supporting text, value, chevron. */
function LinkRow({ row }: { row: NativeFormLinkRow }): React.JSX.Element {
  const { theme } = useTheme();
  const navigation = useFormNavigation();
  const icon = androidIcon(row.symbol);
  return (
    <ListItem
      colors={rowColors(theme)}
      modifiers={[
        fillMaxWidth(),
        composeTestID(row.testID ?? row.id),
        semantics({
          contentDescription: [row.title, row.subtitle, row.value]
            .filter(Boolean)
            .join(", "),
        }),
        ...(row.disabled || !navigation
          ? []
          : [clickable(() => navigation.push(row.route))]),
      ]}
    >
      {icon ? (
        <ListItem.LeadingContent>
          <Icon source={icon} tint={theme.text} size={24} />
        </ListItem.LeadingContent>
      ) : null}
      <ListItem.HeadlineContent>
        <Text
          maxLines={1}
          overflow="ellipsis"
          color={row.disabled ? theme.subtleText : theme.text}
          style={{ typography: "bodyLarge" }}
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
            style={{ typography: "bodyMedium" }}
          >
            {row.subtitle}
          </Text>
        </ListItem.SupportingContent>
      ) : null}
      <ListItem.TrailingContent>
        <Row verticalAlignment="center" horizontalArrangement={{ spacedBy: 4 }}>
          {row.busy ? <Spinner /> : null}
          {row.value ? (
            <Text
              color={toneColor(theme, row.valueTone)}
              maxLines={1}
              style={{ typography: "bodyMedium" }}
              modifiers={[padding(4, 0, 0, 0)]}
            >
              {row.value}
            </Text>
          ) : null}
          <Icon
            source={androidIcon("chevron.right")!}
            tint={theme.subtleText}
            size={24}
          />
        </Row>
      </ListItem.TrailingContent>
    </ListItem>
  );
}

/** The account's initials on a neutral circle and name, above the sections. */
function ProfileRow({
  row,
  top,
}: {
  row: NativeFormProfileRow;
  top: number;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Column
      horizontalAlignment="center"
      verticalArrangement={{ spacedBy: 12 }}
      modifiers={[
        fillMaxWidth(),
        padding(16, top, 16, 8),
        semantics({
          contentDescription: [row.title, row.subtitle]
            .filter(Boolean)
            .join(", "),
        }),
      ]}
    >
      <Box
        contentAlignment="center"
        modifiers={[size(88, 88), clip(Shapes.Circle), background(theme.card)]}
      >
        <Text
          color={theme.text}
          maxLines={1}
          style={{ typography: "headlineLarge", fontWeight: "500" }}
        >
          {row.initials}
        </Text>
      </Box>
      <Column
        horizontalAlignment="center"
        verticalArrangement={{ spacedBy: 2 }}
      >
        <Text
          color={theme.text}
          maxLines={1}
          overflow="ellipsis"
          style={{
            typography: "headlineSmall",
            fontWeight: "600",
            textAlign: "center",
          }}
        >
          {row.title}
        </Text>
        {row.subtitle ? (
          <Text
            color={theme.mutedText}
            maxLines={1}
            overflow="ellipsis"
            style={{ typography: "bodyMedium" }}
          >
            {row.subtitle}
          </Text>
        ) : null}
      </Column>
    </Column>
  );
}

/** A promoted card with an ink capsule action or menu. */
function CardRow({ row }: { row: NativeFormCardRow }): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const button = (
    <Button
      onClick={() =>
        row.onAction ? row.onAction() : row.actions?.length && setOpen(true)
      }
      enabled={!row.disabled}
      shape={Shape.Pill({})}
      colors={{
        containerColor: theme.primary,
        contentColor: theme.primaryForeground,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      }}
      modifiers={[composeTestID(row.testID ?? row.id)]}
    >
      <Text style={{ typography: "labelLarge", fontWeight: "600" }}>
        {row.actionTitle}
      </Text>
    </Button>
  );
  return (
    <Row
      verticalAlignment="center"
      horizontalArrangement={{ spacedBy: 12 }}
      modifiers={[fillMaxWidth(), padding(16, 14, 16, 14)]}
    >
      <Column verticalArrangement={{ spacedBy: 2 }} modifiers={[weight(1)]}>
        <Text color={theme.text} style={{ typography: "titleMedium" }}>
          {row.title}
        </Text>
        {row.subtitle ? (
          <Text color={theme.mutedText} style={{ typography: "bodyMedium" }}>
            {row.subtitle}
          </Text>
        ) : null}
      </Column>
      {row.onAction || !row.actions?.length ? (
        button
      ) : (
        <DropdownMenu
          expanded={open}
          color={theme.raised}
          onDismissRequest={() => setOpen(false)}
        >
          <DropdownMenu.Trigger>{button}</DropdownMenu.Trigger>
          <DropdownMenu.Items>
            {row.actions.map((action) => (
              <DropdownMenuItem
                key={action.id}
                enabled={!action.disabled}
                elementColors={menuItemColors(theme, action.destructive)}
                onClick={() => {
                  setOpen(false);
                  row.onSelect?.(action.id);
                }}
              >
                <DropdownMenuItem.Text>
                  <Text
                    style={{
                      typography: "labelLarge",
                      fontWeight: action.selected ? "600" : "400",
                    }}
                  >
                    {action.title}
                  </Text>
                </DropdownMenuItem.Text>
                {action.selected ? (
                  <DropdownMenuItem.TrailingIcon>
                    <Icon source={androidIcon("checkmark")!} size={20} />
                  </DropdownMenuItem.TrailingIcon>
                ) : null}
              </DropdownMenuItem>
            ))}
          </DropdownMenu.Items>
        </DropdownMenu>
      )}
    </Row>
  );
}

/** A grouped row; hero and profile rows render in `FormSection` instead. */
function FormRow({ row }: { row: NativeFormRow }): React.JSX.Element | null {
  const { theme } = useTheme();
  switch (row.kind) {
    case "item":
      return <ItemRow row={row} />;
    case "header":
      return <HeaderRow row={row} top={8} />;
    case "link":
      return <LinkRow row={row} />;
    case "profile":
      return <ProfileRow row={row} top={8} />;
    case "card":
      return <CardRow row={row} />;
    case "text":
      return <TextRow row={row} />;
    case "picker":
      return <PickerRow row={row} />;
    case "toggle":
      return (
        <ListItem
          colors={rowColors(theme)}
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
          {row.symbol ? (
            <ListItem.LeadingContent>
              <LeadingIcon symbol={row.symbol} />
            </ListItem.LeadingContent>
          ) : null}
          <ListItem.HeadlineContent>
            <Text
              color={row.disabled ? theme.subtleText : theme.text}
              style={{ typography: "bodyLarge" }}
            >
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          {row.subtitle ? (
            <ListItem.SupportingContent>
              <Text
                color={toneColor(theme, row.subtitleTone)}
                style={{ typography: "bodyMedium" }}
              >
                {row.subtitle}
              </Text>
            </ListItem.SupportingContent>
          ) : null}
          <ListItem.TrailingContent>
            <Switch
              value={row.value}
              enabled={!row.disabled}
              onCheckedChange={row.onChange}
              colors={switchColors(theme)}
            />
          </ListItem.TrailingContent>
        </ListItem>
      );
    case "stepper": {
      const stepColors = {
        containerColor: theme.controlFill,
        contentColor: theme.text,
        disabledContainerColor: theme.controlFill,
        disabledContentColor: theme.subtleText,
      };
      return (
        <ListItem
          colors={rowColors(theme)}
          modifiers={[
            fillMaxWidth(),
            composeTestID(row.testID ?? row.id),
            semantics({
              contentDescription: `${row.label}, ${row.format(row.value)}`,
            }),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text
              color={row.disabled ? theme.subtleText : theme.text}
              style={{ typography: "bodyLarge" }}
            >
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Text color={theme.mutedText} style={{ typography: "bodyMedium" }}>
              {row.format(row.value)}
            </Text>
          </ListItem.SupportingContent>
          <ListItem.TrailingContent>
            <Row
              verticalAlignment="center"
              horizontalArrangement={{ spacedBy: 8 }}
            >
              <FilledTonalIconButton
                enabled={!row.disabled && row.value > row.min}
                colors={stepColors}
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
                colors={stepColors}
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
    }
    case "segmented":
      return (
        <Box modifiers={[fillMaxWidth(), padding(16, 10, 16, 10)]}>
          <SegmentedChoiceRow
            label={row.label}
            value={row.value}
            options={row.options}
            onChange={row.onChange}
            disabled={row.disabled}
            testID={row.testID ?? row.id}
          />
        </Box>
      );
    case "weekdays":
      return <WeekdaysRow row={row} />;
    case "date":
      return <DateRow row={row} />;
    case "value":
      if (row.onPress)
        return <NavigationRow row={{ ...row, onPress: row.onPress }} />;
      if (row.monospace || row.stacked)
        return (
          <ListItem
            colors={rowColors(theme)}
            modifiers={[
              fillMaxWidth(),
              composeTestID(row.testID ?? row.id),
              semantics({ contentDescription: `${row.label}, ${row.value}` }),
            ]}
          >
            {row.symbol ? (
              <ListItem.LeadingContent>
                <LeadingIcon symbol={row.symbol} />
              </ListItem.LeadingContent>
            ) : null}
            <ListItem.HeadlineContent>
              <Text color={theme.text} style={{ typography: "bodyLarge" }}>
                {row.label}
              </Text>
            </ListItem.HeadlineContent>
            <ListItem.SupportingContent>
              <Text
                color={toneColor(
                  theme,
                  row.tone ?? (row.monospace ? "default" : "muted"),
                )}
                style={{
                  typography: "bodyMedium",
                  ...(row.monospace ? { fontFamily: "monospace" } : {}),
                }}
              >
                {row.value}
              </Text>
            </ListItem.SupportingContent>
          </ListItem>
        );
      return (
        <ListItem
          colors={rowColors(theme)}
          modifiers={[fillMaxWidth(), composeTestID(row.testID ?? row.id)]}
        >
          {row.symbol ? (
            <ListItem.LeadingContent>
              <LeadingIcon symbol={row.symbol} />
            </ListItem.LeadingContent>
          ) : null}
          <ListItem.HeadlineContent>
            <Text color={theme.text} style={{ typography: "bodyLarge" }}>
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          <ListItem.TrailingContent>
            <Text
              color={toneColor(theme, row.tone)}
              maxLines={2}
              style={{ typography: "bodyMedium" }}
            >
              {row.value}
            </Text>
          </ListItem.TrailingContent>
        </ListItem>
      );
    case "gauge":
      return (
        <ListItem
          colors={rowColors(theme)}
          modifiers={[
            fillMaxWidth(),
            semantics({
              contentDescription: `${row.label}, ${row.valueLabel}`,
            }),
          ]}
        >
          <ListItem.HeadlineContent>
            <Text color={theme.text} style={{ typography: "bodyLarge" }}>
              {row.label}
            </Text>
          </ListItem.HeadlineContent>
          <ListItem.SupportingContent>
            <Column
              verticalArrangement={{ spacedBy: 6 }}
              modifiers={[padding(0, 6, 0, 0)]}
            >
              <LinearProgressIndicator
                progress={Math.min(1, Math.max(0, row.value))}
                color={
                  row.tone === "danger"
                    ? statusColor("critical", theme)
                    : row.tone === "warning"
                      ? statusColor("attention", theme)
                      : theme.primary
                }
                trackColor={theme.controlFill}
                modifiers={[fillMaxWidth()]}
              />
              <Text
                color={toneColor(
                  theme,
                  row.tone === "default" ? "muted" : row.tone,
                )}
                style={{ typography: "bodyMedium" }}
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
          : theme.text;
      const icon = androidIcon(row.symbol);
      return (
        <ListItem
          colors={rowColors(theme)}
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
            <Text
              color={color}
              style={{ typography: "bodyLarge", fontWeight: "500" }}
            >
              {row.title}
            </Text>
          </ListItem.HeadlineContent>
          {row.busy ? (
            <ListItem.TrailingContent>
              <Spinner />
            </ListItem.TrailingContent>
          ) : null}
        </ListItem>
      );
    }
    case "note": {
      const color = toneColor(theme, row.tone);
      const icon = row.busy ? null : androidIcon(row.symbol);
      return (
        <Row
          verticalAlignment={row.busy ? "center" : "top"}
          horizontalArrangement={{ spacedBy: 10 }}
          modifiers={[fillMaxWidth(), padding(16, 14, 16, 14)]}
        >
          {row.busy ? (
            <Spinner edge={16} />
          ) : icon ? (
            <Icon source={icon} tint={color} size={16} />
          ) : null}
          <Text color={color} style={{ typography: "bodySmall" }}>
            {row.text}
          </Text>
        </Row>
      );
    }
  }
}

/**
 * One row of an M3 Expressive segmented list: its own rounded card with
 * larger outer corners on the first and last row of the section.
 */
function Segment({
  index,
  count,
  top,
  children,
}: React.PropsWithChildren<{
  index: number;
  count: number;
  top: number;
}>): React.JSX.Element {
  const { theme } = useTheme();
  const upper = index === 0 ? OUTER_RADIUS : INNER_RADIUS;
  const lower = index === count - 1 ? OUTER_RADIUS : INNER_RADIUS;
  return (
    <Box
      modifiers={[
        padding(16, top, 16, 0),
        fillMaxWidth(),
        clip(
          Shapes.RoundedCorner({
            topStart: upper,
            topEnd: upper,
            bottomStart: lower,
            bottomEnd: lower,
          }),
        ),
        background(theme.card),
      ]}
    >
      {children}
    </Box>
  );
}

/**
 * A section as one lazy list item: hero and profile rows on the canvas, the
 * header, one segment per row (2 dp apart) and the footer. The lazy list
 * keys items by position, so a row inserted into a section must stay inside
 * its item; otherwise every later item, e.g. a focused field, is rebuilt.
 */
function FormSection({
  section,
  first,
}: {
  section: NativeFormSection;
  first: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  const canvas = section.rows.filter(
    (row) => row.kind === "header" || row.kind === "profile",
  );
  const rows = section.rows.filter(
    (row) => row.kind !== "header" && row.kind !== "profile",
  );
  const lead = first ? 8 : 24;
  // Space above the first header or segment after any hero rows.
  const groupTop = canvas.length ? 16 : lead;
  return (
    <Column modifiers={[fillMaxWidth()]}>
      {canvas.map((row, index) => {
        const top = index ? 8 : lead;
        return row.kind === "header" ? (
          <HeaderRow key={`${section.id}/${row.id}`} row={row} top={top} />
        ) : row.kind === "profile" ? (
          <ProfileRow key={`${section.id}/${row.id}`} row={row} top={top} />
        ) : null;
      })}
      {section.title ? (
        <Text
          key={`${section.id}/title`}
          color={theme.text}
          style={{ typography: "titleMedium", fontWeight: "600" }}
          modifiers={[padding(20, groupTop, 20, 8)]}
        >
          {section.title}
        </Text>
      ) : null}
      {rows.map((row, index) => (
        <Segment
          key={`${section.id}/${row.id}`}
          index={index}
          count={rows.length}
          top={index ? 2 : section.title ? 0 : groupTop}
        >
          <FormRow row={row} />
        </Segment>
      ))}
      {section.footer ? (
        <Text
          key={`${section.id}/footer`}
          color={toneColor(theme, section.footerTone)}
          style={{ typography: "bodySmall" }}
          modifiers={[padding(20, 8, 20, 0)]}
        >
          {section.footer}
        </Text>
      ) : null}
    </Column>
  );
}

/**
 * Empty, no-results or load-failure state: a quiet icon, a title, a
 * description and at most one ink action.
 */
function EmptyState({
  empty,
}: {
  empty: NativeFormEmptyState;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { action } = empty;
  const icon = androidIcon(empty.symbol);
  const actionIcon = androidIcon(action?.symbol);
  return (
    <Column horizontalAlignment="center" verticalArrangement={{ spacedBy: 8 }}>
      {icon ? <Icon source={icon} tint={theme.subtleText} size={44} /> : null}
      <Text
        color={theme.text}
        style={{ typography: "titleLarge", textAlign: "center" }}
        modifiers={[padding(0, icon ? 8 : 0, 0, 0)]}
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
      {action ? (
        <Box
          contentAlignment="center"
          modifiers={[padding(0, 12, 0, 0), defaultMinSize({ minHeight: 48 })]}
        >
          {action.busy ? (
            <Box
              contentAlignment="center"
              modifiers={[
                defaultMinSize({ minWidth: 48, minHeight: 48 }),
                ...controlSemantics(action.title, action.testID),
              ]}
            >
              <Spinner edge={24} stroke={3} color={theme.text} />
            </Box>
          ) : (
            <Button
              onClick={action.onPress}
              shape={Shape.Pill({})}
              colors={{
                containerColor: theme.primary,
                contentColor: theme.primaryForeground,
                disabledContainerColor: theme.controlFill,
                disabledContentColor: theme.subtleText,
              }}
              contentPadding={{
                start: actionIcon ? 20 : 24,
                top: 10,
                end: 24,
                bottom: 10,
              }}
              modifiers={[
                defaultMinSize({ minHeight: 48 }),
                ...controlSemantics(action.title, action.testID),
              ]}
            >
              <Row
                verticalAlignment="center"
                horizontalArrangement={{ spacedBy: 8 }}
              >
                {actionIcon ? <Icon source={actionIcon} size={18} /> : null}
                <Text style={{ typography: "labelLarge", fontWeight: "600" }}>
                  {action.title}
                </Text>
              </Row>
            </Button>
          )}
        </Box>
      ) : null}
    </Column>
  );
}

/** Material 3 Expressive grouped list: section headers and segmented rows. */
export function NativeForm({
  sections,
  onRefresh,
  empty,
  loading,
  contentInsetBottom,
  testID,
}: NativeFormProps): React.JSX.Element {
  const { theme } = useTheme();
  const [refreshing, setRefreshing] = useState(false);
  const list = (
    <LazyColumn
      modifiers={[fillMaxSize(), background(theme.background)]}
      contentPadding={{ bottom: 24 + (contentInsetBottom ?? 0) }}
    >
      {sections.map((section, index) => (
        <FormSection key={section.id} section={section} first={!index} />
      ))}
    </LazyColumn>
  );
  return (
    <View
      style={{ flex: 1, backgroundColor: theme.background }}
      testID={testID}
    >
      <Host
        style={{ flex: 1 }}
        colorScheme={theme.scheme}
        seedColor={theme.seed}
        ignoreSafeAreaKeyboardInsets
      >
        {!sections.length && (loading || empty) ? (
          <Column
            horizontalAlignment="center"
            verticalArrangement="center"
            modifiers={[
              fillMaxSize(),
              background(theme.background),
              padding(32, 32, 32, 32),
            ]}
          >
            {loading || !empty ? (
              <CircularProgressIndicator
                color={theme.text}
                trackColor={TRANSPARENT}
              />
            ) : (
              <EmptyState empty={empty} />
            )}
          </Column>
        ) : onRefresh ? (
          <PullToRefreshBox
            isRefreshing={refreshing}
            onRefresh={() => {
              setRefreshing(true);
              void onRefresh().finally(() => setRefreshing(false));
            }}
            indicator={{ color: theme.text, containerColor: theme.raised }}
            modifiers={[fillMaxSize(), background(theme.background)]}
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
