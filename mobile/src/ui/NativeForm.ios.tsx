import React, { useEffect, useRef } from "react";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  Circle,
  ContextMenu,
  DatePicker,
  Form,
  Gauge,
  GlassEffectContainer,
  HStack,
  Host,
  Image,
  Menu,
  NavigationLink,
  Picker,
  ProgressView,
  SecureField,
  Section,
  Spacer,
  Stepper,
  SwipeActions,
  Text,
  TextField,
  Toggle,
  VStack,
  useNativeState,
} from "@expo/ui/swift-ui";
import {
  accessibilityAddTraits,
  accessibilityHidden,
  accessibilityLabel,
  aspectRatio,
  autocorrectionDisabled,
  background,
  buttonBorderShape,
  buttonStyle,
  clipShape,
  contentShape,
  controlSize,
  disabled,
  font,
  foregroundStyle,
  frame,
  gaugeStyle,
  headerProminence,
  keyboardType,
  labelsHidden,
  lineLimit,
  listRowBackground,
  listRowInsets,
  menuStyle,
  minimumScaleFactor,
  monospacedDigit,
  multilineTextAlignment,
  onSubmit,
  padding,
  pickerStyle,
  refreshable,
  resizable,
  scrollContentBackground,
  scrollDismissesKeyboard,
  shapes,
  submitLabel,
  tag,
  textInputAutocapitalization,
  textSelection,
  tint,
  type ModifierConfig,
} from "@expo/ui/swift-ui/modifiers";
import { ProminentLabel } from "./controls.ios";
import type { NativeSymbol } from "./controls.types";
import type {
  NativeFormAccessory,
  NativeFormCardRow,
  NativeFormDateRow,
  NativeFormEmptyState,
  NativeFormHeaderRow,
  NativeFormItemRow,
  NativeFormLinkRow,
  NativeFormProfileRow,
  NativeFormProps,
  NativeFormRow,
  NativeFormSection,
  NativeFormStatus,
  NativeFormSwipeAction,
  NativeFormTextRow,
  NativeFormTone,
  NativeFormWeekdaysRow,
} from "./NativeForm.types";
import { actionFill, statusColor, switchOnColor } from "./statusColors";
import { useTheme, type OpenCuriaTheme } from "./theme";
import {
  WEEKDAY_TOGGLES,
  toggleWeekday,
  wallClockDate,
  wallClockValue,
} from "./wallClock";

/** Glass on iOS 26 for floating controls; bordered styles before it. */
const GLASS = isLiquidGlassAvailable();
const QUICK_ACTION_STYLE = GLASS ? "glass" : "bordered";
const PROMINENT_STYLE = GLASS ? "glassProminent" : "borderedProminent";

/** Row background of hero and profile rows, which sit on the canvas. */
const CLEAR = "#00000000";

/** Modifiers every row adds at its top level, e.g. the card background. */
type RowSurface = ModifierConfig[];

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

/**
 * A symbol on a neutral rounded tile, or a small image filling it: the
 * identity of a workspace or app in a row (40 pt) or a hero header (72 pt).
 */
function Tile({
  symbol,
  image,
  size,
  radius,
  fill,
  symbolSize,
}: {
  symbol?: NativeSymbol;
  image?: string;
  size: number;
  radius: number;
  fill: string;
  symbolSize: number;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const shape = shapes.roundedRectangle({
    cornerRadius: radius,
    roundedCornerStyle: "continuous",
  });
  if (image)
    return (
      <Image
        uiImage={image}
        modifiers={[
          resizable(),
          aspectRatio({ contentMode: "fill" }),
          frame({ width: size, height: size }),
          background(fill, shape),
          clipShape("roundedRectangle", radius),
        ]}
      />
    );
  if (!symbol) return null;
  return (
    <Image
      systemName={symbol}
      modifiers={[
        font({ size: symbolSize, weight: "medium" }),
        foregroundStyle(theme.text),
        frame({ width: size, height: size }),
        background(fill, shape),
      ]}
    />
  );
}

/** A plain glyph in a fixed column, ink unless a status colors it. */
function LeadingSymbol({
  symbol,
  color,
}: {
  symbol: NativeSymbol;
  color?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Image
      systemName={symbol}
      modifiers={[
        font({ textStyle: "body" }),
        foregroundStyle(color ?? theme.text),
        frame({ width: 28 }),
      ]}
    />
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
    <Circle
      modifiers={[
        foregroundStyle(statusColor(status, theme)),
        frame({ width: 7, height: 7 }),
        accessibilityHidden(true),
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
    <HStack spacing={6}>
      {busy ? (
        <ProgressView modifiers={[controlSize("small")]} />
      ) : status ? (
        <StatusDot status={status} />
      ) : null}
      <Text
        modifiers={[
          font({ textStyle: "subheadline" }),
          foregroundStyle(toneColor(theme, tone)),
          lineLimit(centered ? 2 : 1),
          ...(centered ? [multilineTextAlignment("center")] : []),
        ]}
      >
        {text}
      </Text>
    </HStack>
  );
}

function SwipeButtons({
  actions,
  onAction,
}: {
  actions: NativeFormSwipeAction[];
  onAction?: (id: string) => void;
}): React.JSX.Element[] {
  const { theme } = useTheme();
  return actions.map((action) => (
    <Button
      key={action.id}
      label={action.title}
      systemImage={action.symbol}
      role={action.destructive ? "destructive" : undefined}
      onPress={() => onAction?.(action.id)}
      modifiers={
        action.destructive
          ? []
          : [
              tint(
                actionFill(
                  !action.tone || action.tone === "default"
                    ? "accent"
                    : action.tone,
                  theme,
                ),
              ),
            ]
      }
    />
  ));
}

/**
 * A trailing capsule with its own tap target, as in the App Store: ink for
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
      <ProgressView
        testID={accessory.testID}
        modifiers={[accessibilityLabel(label)]}
      />
    );
  const off = Boolean(inactive || accessory.disabled);
  const fill = off
    ? theme.controlFill
    : accessory.prominent
      ? theme.primary
      : theme.controlFill;
  const color = off
    ? theme.subtleText
    : accessory.prominent
      ? theme.primaryForeground
      : theme.text;
  return (
    <Button
      onPress={accessory.onPress}
      testID={accessory.testID}
      modifiers={[
        buttonStyle("borderless"),
        disabled(off),
        accessibilityLabel(label),
      ]}
    >
      <Text
        modifiers={[
          font({ textStyle: "subheadline", weight: "semibold" }),
          foregroundStyle(color),
          lineLimit(1),
          padding({ horizontal: 14, vertical: 6 }),
          background(fill, shapes.capsule()),
          // The capsule stays compact; the 44 pt frame around it is the target.
          frame({ minHeight: 44 }),
          contentShape(shapes.rectangle()),
        ]}
      >
        {accessory.title}
      </Text>
    </Button>
  );
}

function ItemRow({
  row,
  surface,
}: {
  row: NativeFormItemRow;
  surface: RowSurface;
}): React.JSX.Element {
  const { theme } = useTheme();
  const leading = [row.leadingText, row.leadingCaption]
    .filter(Boolean)
    .join(" ");
  const label = [leading, row.title, row.subtitle].filter(Boolean).join(", ");
  const leadingColor = toneColor(theme, row.leadingTone ?? "default");
  // [busy] [badge] [accessory] [✓] [ⓘ]: without an accessory the checkmark
  // stays inside the row's button, so it is part of the tap target.
  const checkmark = row.selected ? (
    <Image
      systemName="checkmark"
      modifiers={[
        font({ textStyle: "body", weight: "semibold" }),
        foregroundStyle(theme.text),
        // The row's button announces the selection.
        accessibilityHidden(true),
      ]}
    />
  ) : null;
  const content = (
    // The label fills the row so its whole width and height is the target.
    <HStack
      spacing={12}
      modifiers={[
        frame({ maxWidth: Infinity, alignment: "leading", minHeight: 44 }),
        contentShape(shapes.rectangle()),
      ]}
    >
      {row.leadingText ? (
        // Tabular figures in a minimum width keep an agenda's times aligned;
        // larger Dynamic Type sizes may widen the column.
        <HStack
          alignment="firstTextBaseline"
          spacing={2}
          modifiers={[
            frame({
              minWidth: row.leadingCaption ? 84 : 62,
              alignment: "leading",
            }),
          ]}
        >
          <Text
            modifiers={[
              font({ textStyle: "title3", weight: "semibold" }),
              monospacedDigit(),
              foregroundStyle(leadingColor),
              lineLimit(1),
            ]}
          >
            {row.leadingText}
          </Text>
          {row.leadingCaption ? (
            <Text
              modifiers={[
                font({ textStyle: "caption", weight: "semibold" }),
                foregroundStyle(leadingColor),
                lineLimit(1),
              ]}
            >
              {row.leadingCaption}
            </Text>
          ) : null}
        </HStack>
      ) : row.image || (row.symbol && row.tile) ? (
        <Tile
          symbol={row.symbol}
          image={row.image}
          size={40}
          radius={10}
          fill={theme.controlFill}
          symbolSize={18}
        />
      ) : row.symbol ? (
        <LeadingSymbol
          symbol={row.symbol}
          color={
            row.symbolStatus ? statusColor(row.symbolStatus, theme) : undefined
          }
        />
      ) : null}
      <VStack alignment="leading" spacing={2}>
        <Text
          modifiers={[
            font({
              textStyle: "body",
              weight: row.selected ? "semibold" : "regular",
            }),
            foregroundStyle(theme.text),
            lineLimit(1),
          ]}
        >
          {row.title}
        </Text>
        {row.subtitle ? (
          <StatusLine
            text={row.subtitle}
            tone={row.subtitleTone}
            status={row.status}
          />
        ) : null}
      </VStack>
      <Spacer />
      {row.busy ? <ProgressView /> : null}
      {row.badge && !row.accessory ? (
        <Text
          modifiers={[
            font({ textStyle: "caption", weight: "semibold" }),
            foregroundStyle(theme.text),
            lineLimit(1),
            padding({ horizontal: 8, vertical: 3 }),
            background(theme.controlFill, shapes.capsule()),
          ]}
        >
          {row.badge}
        </Text>
      ) : null}
      {row.accessory ? null : checkmark}
    </HStack>
  );
  const swipes = Boolean(row.leadingSwipe?.length || row.trailingSwipe?.length);
  const menus = Boolean(row.actions?.length);
  // Only the outermost view carries the row background, so List reads it.
  const body = (
    <HStack spacing={8} modifiers={swipes || menus ? [] : surface}>
      <Button
        onPress={row.onPress}
        testID={row.testID ?? row.id}
        modifiers={[
          buttonStyle("borderless"),
          disabled(Boolean(row.disabled || !row.onPress)),
          accessibilityLabel(label),
          accessibilityAddTraits(row.selected ? ["isSelected"] : []),
        ]}
      >
        {content}
      </Button>
      {row.accessory ? (
        <>
          <AccessoryButton accessory={row.accessory} inactive={row.disabled} />
          {checkmark}
        </>
      ) : null}
      {row.onInfo ? (
        <Button
          onPress={row.onInfo}
          modifiers={[
            buttonStyle("borderless"),
            accessibilityLabel(row.infoLabel ?? `Details for ${row.title}`),
          ]}
        >
          <Image
            systemName="info.circle"
            size={20}
            color={theme.text}
            // The glyph stays 20 pt at the trailing edge; the 44 pt frame
            // around it is the target.
            modifiers={[
              frame({ minWidth: 44, minHeight: 44, alignment: "trailing" }),
              contentShape(shapes.rectangle()),
            ]}
          />
        </Button>
      ) : null}
    </HStack>
  );
  const menu = row.actions?.length ? (
    <ContextMenu modifiers={swipes ? [] : surface}>
      <ContextMenu.Trigger>{body}</ContextMenu.Trigger>
      <ContextMenu.Items>
        {row.actions.map((action) => (
          <Button
            key={action.id}
            label={action.title}
            systemImage={
              action.symbol as React.ComponentProps<
                typeof Button
              >["systemImage"]
            }
            role={action.destructive ? "destructive" : undefined}
            modifiers={[disabled(Boolean(action.disabled))]}
            onPress={() => row.onAction?.(action.id)}
          />
        ))}
      </ContextMenu.Items>
    </ContextMenu>
  ) : (
    body
  );
  if (!swipes) return menu;
  return (
    <SwipeActions modifiers={surface}>
      {menu}
      {row.leadingSwipe?.length ? (
        <SwipeActions.Actions edge="leading">
          {SwipeButtons({ actions: row.leadingSwipe, onAction: row.onAction })}
        </SwipeActions.Actions>
      ) : null}
      {row.trailingSwipe?.length ? (
        <SwipeActions.Actions edge="trailing" allowsFullSwipe={false}>
          {SwipeButtons({
            actions: row.trailingSwipe,
            onAction: row.onAction,
          })}
        </SwipeActions.Actions>
      ) : null}
    </SwipeActions>
  );
}

/** Hero header on the canvas with liquid-glass quick actions, as in Contacts. */
function HeaderRow({ row }: { row: NativeFormHeaderRow }): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack
      spacing={14}
      modifiers={[
        frame({ maxWidth: Infinity }),
        listRowBackground(CLEAR),
        listRowInsets({ top: 4, leading: 0, bottom: 4, trailing: 0 }),
      ]}
    >
      <Tile
        symbol={row.symbol}
        image={row.image}
        size={72}
        radius={18}
        fill={theme.card}
        symbolSize={30}
      />
      <VStack spacing={4}>
        <Text
          modifiers={[
            font({ textStyle: "title2", weight: "bold" }),
            foregroundStyle(theme.text),
            lineLimit(2),
            multilineTextAlignment("center"),
          ]}
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
      </VStack>
      {row.actions.length ? (
        <GlassEffectContainer spacing={10}>
          <HStack spacing={10}>
            {row.actions.map((action) => {
              const color = action.disabled
                ? theme.subtleText
                : action.destructive
                  ? theme.danger
                  : theme.text;
              return (
                <Button
                  key={action.id}
                  onPress={() => row.onAction(action.id)}
                  testID={`${row.id}-${action.id}`}
                  role={action.destructive ? "destructive" : undefined}
                  modifiers={[
                    buttonStyle(QUICK_ACTION_STYLE),
                    tint(theme.text),
                    disabled(Boolean(action.disabled)),
                    accessibilityLabel(action.title),
                  ]}
                >
                  <VStack
                    spacing={4}
                    modifiers={[frame({ width: 64, height: 46 })]}
                  >
                    <Image
                      systemName={action.symbol}
                      modifiers={[
                        font({ size: 19, weight: "medium" }),
                        foregroundStyle(color),
                      ]}
                    />
                    <Text
                      modifiers={[
                        font({ textStyle: "caption", weight: "medium" }),
                        foregroundStyle(
                          action.disabled ? theme.subtleText : theme.text,
                        ),
                        lineLimit(1),
                        minimumScaleFactor(0.8),
                      ]}
                    >
                      {action.title}
                    </Text>
                  </VStack>
                </Button>
              );
            })}
          </HStack>
        </GlassEffectContainer>
      ) : null}
    </VStack>
  );
}

const KEYBOARDS = {
  default: "default",
  url: "url",
  email: "email-address",
  number: "numeric",
} as const;

function TextRow({
  row,
  surface,
}: {
  row: NativeFormTextRow;
  surface: RowSurface;
}): React.JSX.Element {
  const text = useNativeState(row.value);
  const latest = useRef(row.value);
  useEffect(() => {
    if (row.value !== latest.current) {
      latest.current = row.value;
      text.set(row.value);
    }
  }, [row.value, text]);
  const common = {
    text,
    testID: row.testID ?? row.id,
    placeholder: row.placeholder ?? row.label,
    autoFocus: row.autoFocus,
    onTextChange: (value: string) => {
      latest.current = value;
      row.onChange(value);
    },
  };
  if (row.secure)
    return (
      <SecureField
        {...common}
        modifiers={[
          accessibilityLabel(row.label),
          disabled(Boolean(row.disabled)),
          submitLabel("done"),
          onSubmit(() => row.onSubmit?.()),
          ...surface,
        ]}
      />
    );
  return (
    <TextField
      {...common}
      axis={row.multiline ? "vertical" : "horizontal"}
      modifiers={[
        accessibilityLabel(row.label),
        disabled(Boolean(row.disabled)),
        keyboardType(KEYBOARDS[row.keyboard ?? "default"]),
        autocorrectionDisabled(!row.prose),
        textInputAutocapitalization(row.prose ? "sentences" : "never"),
        ...(row.monospace ? [font({ design: "monospaced" })] : []),
        // Return starts a new line in multiline prose instead of submitting.
        ...(row.multiline && row.prose
          ? []
          : [submitLabel("done"), onSubmit(() => row.onSubmit?.())]),
        ...(row.multiline
          ? [lineLimit({ min: row.lines?.[0] ?? 2, max: row.lines?.[1] ?? 6 })]
          : []),
        ...surface,
      ]}
    />
  );
}

/** Pushes the row's page onto the settings navigation stack. */
function LinkRow({
  row,
  surface,
}: {
  row: NativeFormLinkRow;
  surface: RowSurface;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <NavigationLink
      value={row.route}
      testID={row.testID ?? row.id}
      modifiers={[
        disabled(Boolean(row.disabled)),
        accessibilityLabel(
          [row.title, row.subtitle, row.value].filter(Boolean).join(", "),
        ),
        ...surface,
      ]}
    >
      <HStack spacing={14} modifiers={[frame({ minHeight: 36 })]}>
        {row.symbol ? <LeadingSymbol symbol={row.symbol} /> : null}
        <VStack alignment="leading" spacing={2}>
          <Text modifiers={[foregroundStyle(theme.text), lineLimit(1)]}>
            {row.title}
          </Text>
          {row.subtitle ? (
            <Text
              modifiers={[
                font({ textStyle: "subheadline" }),
                foregroundStyle(toneColor(theme, row.subtitleTone)),
                lineLimit(1),
              ]}
            >
              {row.subtitle}
            </Text>
          ) : null}
        </VStack>
        <Spacer />
        {row.busy ? <ProgressView /> : null}
        {row.value ? (
          <Text
            modifiers={[
              foregroundStyle(toneColor(theme, row.valueTone)),
              lineLimit(1),
            ]}
          >
            {row.value}
          </Text>
        ) : null}
      </HStack>
    </NavigationLink>
  );
}

/** The account's initials on a neutral circle and name, above the sections. */
function ProfileRow({ row }: { row: NativeFormProfileRow }): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack
      spacing={12}
      modifiers={[
        frame({ maxWidth: Infinity }),
        listRowBackground(CLEAR),
        listRowInsets({ top: 4, leading: 0, bottom: 8, trailing: 0 }),
        accessibilityLabel(
          [row.title, row.subtitle].filter(Boolean).join(", "),
        ),
      ]}
    >
      <Text
        modifiers={[
          font({ size: 34, weight: "medium" }),
          foregroundStyle(theme.text),
          frame({ width: 88, height: 88 }),
          background(theme.card, shapes.circle()),
        ]}
      >
        {row.initials}
      </Text>
      <VStack spacing={3}>
        <Text
          modifiers={[
            font({ textStyle: "title2", weight: "semibold" }),
            foregroundStyle(theme.text),
            lineLimit(1),
          ]}
        >
          {row.title}
        </Text>
        {row.subtitle ? (
          <Text
            modifiers={[
              font({ textStyle: "subheadline" }),
              foregroundStyle(theme.mutedText),
              lineLimit(1),
            ]}
          >
            {row.subtitle}
          </Text>
        ) : null}
      </VStack>
    </VStack>
  );
}

/** A promoted card with an ink capsule (or menu), like ChatGPT's plan card. */
function CardRow({
  row,
  surface,
}: {
  row: NativeFormCardRow;
  surface: RowSurface;
}): React.JSX.Element {
  const { theme } = useTheme();
  const capsule = [
    buttonStyle("borderedProminent"),
    buttonBorderShape("capsule"),
    controlSize("large"),
    tint(theme.primary),
    disabled(Boolean(row.disabled)),
  ];
  const label = (
    <ProminentLabel title={row.actionTitle} enabled={!row.disabled} />
  );
  return (
    <HStack
      spacing={12}
      modifiers={[
        padding({ vertical: 6 }),
        frame({ minHeight: 64 }),
        ...surface,
      ]}
    >
      <VStack alignment="leading" spacing={3}>
        <Text
          modifiers={[
            font({ textStyle: "headline" }),
            foregroundStyle(theme.text),
            lineLimit(2),
          ]}
        >
          {row.title}
        </Text>
        {row.subtitle ? (
          <Text
            modifiers={[
              font({ textStyle: "subheadline" }),
              foregroundStyle(theme.mutedText),
              lineLimit(3),
            ]}
          >
            {row.subtitle}
          </Text>
        ) : null}
      </VStack>
      <Spacer />
      {row.onAction || !row.actions?.length ? (
        <Button
          onPress={row.onAction}
          testID={row.testID ?? row.id}
          modifiers={[...capsule, accessibilityLabel(row.actionTitle)]}
        >
          {label}
        </Button>
      ) : (
        <Menu
          label={label}
          testID={row.testID ?? row.id}
          modifiers={[
            menuStyle("button"),
            ...capsule,
            accessibilityLabel(row.actionTitle),
          ]}
        >
          {row.actions.map((action) => (
            <Button
              key={action.id}
              label={action.title}
              systemImage={
                action.selected
                  ? "checkmark"
                  : (action.symbol as NativeSymbol | undefined)
              }
              role={action.destructive ? "destructive" : undefined}
              modifiers={[disabled(Boolean(action.disabled))]}
              onPress={() => row.onSelect?.(action.id)}
            />
          ))}
        </Menu>
      )}
    </HStack>
  );
}

/** Seven round day toggles below the label, as in Clock and Reminders. */
function WeekdaysRow({
  row,
  surface,
}: {
  row: NativeFormWeekdaysRow;
  surface: RowSurface;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack alignment="leading" spacing={10} modifiers={surface}>
      <HStack>
        <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
        <Spacer />
        {row.summary ? (
          <Text modifiers={[foregroundStyle(theme.mutedText)]}>
            {row.summary}
          </Text>
        ) : null}
      </HStack>
      <HStack spacing={0}>
        {WEEKDAY_TOGGLES.map((day) => {
          const on = row.value.includes(day.value);
          return (
            <Button
              key={day.value}
              testID={`${row.testID ?? row.id}-${day.value}`}
              onPress={() => row.onChange(toggleWeekday(row.value, day.value))}
              // Plain buttons keep each toggle its own tap target in a row.
              modifiers={[
                buttonStyle("plain"),
                frame({ maxWidth: Infinity }),
                disabled(Boolean(row.disabled)),
                accessibilityLabel(day.name),
                accessibilityAddTraits(on ? ["isSelected"] : []),
              ]}
            >
              <Text
                modifiers={[
                  font({ size: 15, weight: "semibold" }),
                  foregroundStyle(on ? theme.primaryForeground : theme.text),
                  frame({ width: 34, height: 34 }),
                  background(
                    on ? theme.primary : theme.controlFill,
                    shapes.circle(),
                  ),
                ]}
              >
                {day.short}
              </Text>
            </Button>
          );
        })}
      </HStack>
    </VStack>
  );
}

/** Compact SwiftUI date or time picker; a red minus removes a listed entry. */
function DateRow({
  row,
  surface,
}: {
  row: NativeFormDateRow;
  surface: RowSurface;
}): React.JSX.Element {
  const { theme } = useTheme();
  // The calendar draws the selected day in white on the tint; near-white ink
  // would hide it in dark mode.
  const pickerTint = theme.scheme === "dark" ? "#8E8E93" : theme.primary;
  const picker = (extra: ModifierConfig[]) => (
    <DatePicker
      title={row.label}
      selection={wallClockDate(row.mode, row.value)}
      displayedComponents={[row.mode === "date" ? "date" : "hourAndMinute"]}
      range={
        row.minimumDate
          ? { start: wallClockDate("date", row.minimumDate) }
          : undefined
      }
      onDateChange={(date) => {
        const value = wallClockValue(row.mode, date);
        if (value !== row.value) row.onChange(value);
      }}
      testID={row.testID ?? row.id}
      modifiers={[tint(pickerTint), disabled(Boolean(row.disabled)), ...extra]}
    />
  );
  if (!row.onRemove) return picker(surface);
  return (
    <HStack spacing={12} modifiers={surface}>
      <Button
        onPress={row.onRemove}
        testID={`${row.testID ?? row.id}-remove`}
        modifiers={[
          buttonStyle("plain"),
          disabled(Boolean(row.disabled)),
          accessibilityLabel(`Remove ${row.label}`),
        ]}
      >
        <Image
          systemName="minus.circle.fill"
          size={20}
          color={statusColor("critical", theme)}
        />
      </Button>
      {picker([])}
    </HStack>
  );
}

function Row({ row }: { row: NativeFormRow }): React.JSX.Element {
  const { theme } = useTheme();
  // Grouped rows sit on a card on the canvas; hero and profile rows do not.
  const surface: RowSurface = [listRowBackground(theme.card)];
  switch (row.kind) {
    case "item":
      return <ItemRow row={row} surface={surface} />;
    case "header":
      return <HeaderRow row={row} />;
    case "link":
      return <LinkRow row={row} surface={surface} />;
    case "profile":
      return <ProfileRow row={row} />;
    case "card":
      return <CardRow row={row} surface={surface} />;
    case "text":
      return <TextRow row={row} surface={surface} />;
    case "toggle":
      return (
        <Toggle
          isOn={row.value}
          testID={row.testID ?? row.id}
          onIsOnChange={row.onChange}
          modifiers={[
            // An ink tint would put the white knob on white in dark mode.
            tint(switchOnColor(theme)),
            disabled(Boolean(row.disabled)),
            accessibilityLabel(
              [row.label, row.subtitle].filter(Boolean).join(", "),
            ),
            ...surface,
          ]}
        >
          <HStack spacing={14}>
            {row.symbol ? <LeadingSymbol symbol={row.symbol} /> : null}
            <VStack alignment="leading" spacing={2}>
              <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
              {row.subtitle ? (
                <Text
                  modifiers={[
                    font({ textStyle: "footnote" }),
                    foregroundStyle(toneColor(theme, row.subtitleTone)),
                  ]}
                >
                  {row.subtitle}
                </Text>
              ) : null}
            </VStack>
          </HStack>
        </Toggle>
      );
    case "picker":
      return (
        <Picker
          label={
            row.symbol ? (
              <HStack spacing={14}>
                <LeadingSymbol symbol={row.symbol} />
                <Text modifiers={[foregroundStyle(theme.text)]}>
                  {row.label}
                </Text>
              </HStack>
            ) : (
              row.label
            )
          }
          selection={row.value}
          testID={row.testID ?? row.id}
          onSelectionChange={(value: string) => {
            const option = row.groups
              .flatMap((group) => group.options)
              .find((entry) => entry.value === value);
            if (option && !option.disabled) row.onChange(value);
          }}
          modifiers={[
            pickerStyle("menu"),
            tint(theme.primary),
            disabled(Boolean(row.disabled)),
            accessibilityLabel(row.label),
            ...surface,
          ]}
        >
          {row.groups.map((group, index) =>
            group.title ? (
              <Section key={group.title} title={group.title}>
                {group.options.map((option) => (
                  <Text
                    key={option.value}
                    modifiers={[
                      tag(option.value),
                      disabled(Boolean(option.disabled)),
                    ]}
                  >
                    {option.label}
                  </Text>
                ))}
              </Section>
            ) : (
              group.options.map((option) => (
                <Text
                  key={`${index}-${option.value}`}
                  modifiers={[
                    tag(option.value),
                    disabled(Boolean(option.disabled)),
                  ]}
                >
                  {option.label}
                </Text>
              ))
            ),
          )}
        </Picker>
      );
    case "stepper":
      return (
        <HStack modifiers={surface}>
          <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
          <Spacer />
          <Text
            modifiers={[foregroundStyle(theme.mutedText), monospacedDigit()]}
          >
            {row.format(row.value)}
          </Text>
          <Stepper
            label={row.label}
            value={row.value}
            min={row.min}
            max={row.max}
            step={row.step}
            onValueChange={(value) =>
              row.onChange(Math.min(row.max, Math.max(row.min, value)))
            }
            modifiers={[
              labelsHidden(),
              disabled(Boolean(row.disabled)),
              accessibilityLabel(`${row.label}, ${row.format(row.value)}`),
            ]}
          />
        </HStack>
      );
    case "value": {
      const label = row.symbol ? (
        <HStack spacing={14}>
          <LeadingSymbol symbol={row.symbol} />
          <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
        </HStack>
      ) : (
        <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
      );
      if (row.monospace || row.stacked)
        return (
          <VStack
            alignment="leading"
            spacing={6}
            modifiers={[
              accessibilityLabel(`${row.label}, ${row.value}`),
              ...surface,
            ]}
          >
            {label}
            <Text
              modifiers={[
                font(
                  row.monospace
                    ? { textStyle: "callout", design: "monospaced" }
                    : { textStyle: "body" },
                ),
                foregroundStyle(
                  toneColor(
                    theme,
                    row.tone ?? (row.monospace ? "default" : "muted"),
                  ),
                ),
                textSelection(true),
              ]}
            >
              {row.value}
            </Text>
          </VStack>
        );
      if (row.onPress)
        return (
          <Button
            onPress={row.onPress}
            testID={row.testID ?? row.id}
            modifiers={[
              disabled(Boolean(row.disabled)),
              accessibilityLabel(`${row.label}, ${row.value}`),
              ...surface,
            ]}
          >
            <HStack spacing={8}>
              {label}
              <Spacer />
              <Text
                modifiers={[
                  foregroundStyle(toneColor(theme, row.tone)),
                  lineLimit(1),
                ]}
              >
                {row.value}
              </Text>
              <Image
                systemName="chevron.right"
                modifiers={[
                  font({ textStyle: "footnote", weight: "semibold" }),
                  foregroundStyle(theme.subtleText),
                ]}
              />
            </HStack>
          </Button>
        );
      return (
        <HStack
          spacing={8}
          modifiers={[
            accessibilityLabel(`${row.label}, ${row.value}`),
            ...surface,
          ]}
        >
          {label}
          <Spacer />
          <Text
            modifiers={[
              foregroundStyle(toneColor(theme, row.tone)),
              multilineTextAlignment("trailing"),
              lineLimit(2),
            ]}
          >
            {row.value}
          </Text>
        </HStack>
      );
    }
    case "segmented":
      return (
        <Picker
          label={row.label}
          selection={row.value}
          testID={row.testID ?? row.id}
          onSelectionChange={(value: string) => {
            if (value !== row.value) row.onChange(value);
          }}
          modifiers={[
            pickerStyle("segmented"),
            labelsHidden(),
            disabled(Boolean(row.disabled)),
            ...surface,
          ]}
        >
          {row.options.map((option) => (
            <Text
              key={option.value}
              modifiers={[
                tag(option.value),
                ...(option.accessibilityLabel
                  ? [accessibilityLabel(option.accessibilityLabel)]
                  : []),
              ]}
            >
              {option.label}
            </Text>
          ))}
        </Picker>
      );
    case "weekdays":
      return <WeekdaysRow row={row} surface={surface} />;
    case "date":
      return <DateRow row={row} surface={surface} />;
    case "gauge":
      return (
        <VStack alignment="leading" spacing={6} modifiers={surface}>
          <HStack>
            <Text modifiers={[foregroundStyle(theme.text)]}>{row.label}</Text>
            <Spacer />
            <Text
              modifiers={[
                foregroundStyle(
                  toneColor(theme, row.tone === "default" ? "muted" : row.tone),
                ),
                monospacedDigit(),
              ]}
            >
              {row.valueLabel}
            </Text>
          </HStack>
          <Gauge
            value={Math.min(1, Math.max(0, row.value))}
            modifiers={[
              gaugeStyle("linearCapacity"),
              tint(
                row.tone === "danger"
                  ? statusColor("critical", theme)
                  : row.tone === "warning"
                    ? statusColor("attention", theme)
                    : theme.primary,
              ),
              accessibilityLabel(`${row.label}, ${row.valueLabel}`),
            ]}
          />
        </VStack>
      );
    case "button": {
      const color = row.disabled
        ? theme.subtleText
        : row.destructive
          ? theme.danger
          : theme.text;
      return (
        <Button
          onPress={row.onPress}
          testID={row.testID ?? row.id}
          role={row.destructive ? "destructive" : undefined}
          modifiers={[
            disabled(Boolean(row.disabled || row.busy)),
            accessibilityLabel(row.title),
            ...surface,
          ]}
        >
          <HStack spacing={8}>
            {row.symbol ? (
              <Image
                systemName={row.symbol}
                modifiers={[
                  font({ textStyle: "body" }),
                  foregroundStyle(color),
                ]}
              />
            ) : null}
            <Text modifiers={[foregroundStyle(color)]}>{row.title}</Text>
            {row.busy ? (
              <>
                <Spacer />
                <ProgressView />
              </>
            ) : null}
          </HStack>
        </Button>
      );
    }
    case "note": {
      const color = toneColor(theme, row.tone);
      const text = [font({ textStyle: "footnote" }), foregroundStyle(color)];
      if (!row.busy && !row.symbol)
        return <Text modifiers={[...text, ...surface]}>{row.text}</Text>;
      return (
        <HStack
          spacing={8}
          alignment={row.busy ? "center" : "firstTextBaseline"}
          modifiers={surface}
        >
          {row.busy ? (
            <ProgressView modifiers={[controlSize("small")]} />
          ) : row.symbol ? (
            <Image
              systemName={row.symbol}
              modifiers={[
                font({ textStyle: "footnote", weight: "semibold" }),
                foregroundStyle(color),
              ]}
            />
          ) : null}
          <Text modifiers={text}>{row.text}</Text>
        </HStack>
      );
    }
  }
}

function FormSection({
  section,
}: {
  section: NativeFormSection;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Section
      title={section.title}
      // Every header is the sentence-case, bold ink header of the sheet.
      modifiers={[headerProminence("increased")]}
      footer={
        section.footer ? (
          <Text
            modifiers={[
              foregroundStyle(toneColor(theme, section.footerTone ?? "muted")),
            ]}
          >
            {section.footer}
          </Text>
        ) : undefined
      }
    >
      {section.rows.map((row) => (
        <Row key={row.id} row={row} />
      ))}
    </Section>
  );
}

/**
 * Empty, no-results or load-failure state: a quiet symbol, a title, a
 * description and at most one prominent ink action.
 */
function EmptyState({
  empty,
}: {
  empty: NativeFormEmptyState;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { action } = empty;
  return (
    <VStack spacing={8} modifiers={[padding({ horizontal: 32 })]}>
      <Image
        systemName={empty.symbol}
        modifiers={[
          font({ size: 44 }),
          foregroundStyle(theme.subtleText),
          padding({ bottom: 6 }),
          accessibilityHidden(true),
        ]}
      />
      <Text
        modifiers={[
          font({ textStyle: "title3", weight: "semibold" }),
          foregroundStyle(theme.text),
          multilineTextAlignment("center"),
        ]}
      >
        {empty.title}
      </Text>
      {empty.description ? (
        <Text
          modifiers={[
            font({ textStyle: "subheadline" }),
            foregroundStyle(theme.mutedText),
            multilineTextAlignment("center"),
          ]}
        >
          {empty.description}
        </Text>
      ) : null}
      {action ? (
        action.busy ? (
          <ProgressView
            testID={action.testID}
            modifiers={[accessibilityLabel(action.title), padding({ top: 14 })]}
          />
        ) : (
          <Button
            onPress={action.onPress}
            testID={action.testID}
            modifiers={[
              buttonStyle(PROMINENT_STYLE),
              buttonBorderShape("capsule"),
              controlSize("large"),
              tint(theme.primary),
              accessibilityLabel(action.title),
              padding({ top: 14 }),
            ]}
          >
            <ProminentLabel title={action.title} symbol={action.symbol} />
          </Button>
        )
      ) : null}
    </VStack>
  );
}

/**
 * The SwiftUI Form without a host, for screens that already run inside
 * SwiftUI: a page of the settings navigation stack or a sheet's form.
 */
export function NativeFormContent({
  sections,
  onRefresh,
  empty,
  loading,
  testID,
}: Pick<
  NativeFormProps,
  "sections" | "onRefresh" | "empty" | "loading" | "testID"
>): React.JSX.Element {
  const { theme } = useTheme();
  if (!sections.length && (loading || empty))
    return (
      // Fills the host or page so the state sits in the middle of the canvas.
      <VStack
        testID={testID}
        modifiers={[
          frame({ maxWidth: Infinity, maxHeight: Infinity }),
          background(theme.background),
        ]}
      >
        {loading || !empty ? <ProgressView /> : <EmptyState empty={empty} />}
      </VStack>
    );
  return (
    <Form
      testID={testID}
      modifiers={[
        scrollContentBackground("hidden"),
        background(theme.background),
        scrollDismissesKeyboard("interactively"),
        ...(onRefresh ? [refreshable(onRefresh)] : []),
      ]}
    >
      {sections.map((section) => (
        <FormSection key={section.id} section={section} />
      ))}
    </Form>
  );
}

/** SwiftUI Form: grouped sections, native controls, swipe actions and context menus. */
export function NativeForm({
  sections,
  onRefresh,
  empty,
  loading,
  testID,
}: NativeFormProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      style={{ flex: 1 }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      testID={testID}
    >
      <NativeFormContent
        sections={sections}
        onRefresh={onRefresh}
        empty={empty}
        loading={loading}
      />
    </Host>
  );
}
