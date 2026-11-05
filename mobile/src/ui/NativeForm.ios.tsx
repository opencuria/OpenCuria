import React, { useEffect, useRef } from "react";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  ContentUnavailableView,
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
  accessibilityLabel,
  autocorrectionDisabled,
  background,
  buttonBorderShape,
  buttonStyle,
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
  monospacedDigit,
  multilineTextAlignment,
  onSubmit,
  padding,
  pickerStyle,
  refreshable,
  scrollDismissesKeyboard,
  shapes,
  submitLabel,
  tag,
  textInputAutocapitalization,
  textSelection,
  tint,
} from "@expo/ui/swift-ui/modifiers";
import type { NativeSymbol } from "./controls.types";
import type {
  NativeFormCardRow,
  NativeFormDateRow,
  NativeFormHeaderRow,
  NativeFormItemRow,
  NativeFormLinkRow,
  NativeFormProfileRow,
  NativeFormProps,
  NativeFormRow,
  NativeFormSection,
  NativeFormSwipeAction,
  NativeFormTextRow,
  NativeFormTone,
  NativeFormWeekdaysRow,
} from "./NativeForm.types";
import { useTheme, type OpenCuriaTheme } from "./theme";
import {
  WEEKDAY_TOGGLES,
  toggleWeekday,
  wallClockDate,
  wallClockValue,
} from "./wallClock";

/** Quick actions are interactive liquid glass on iOS 26 and bordered before it. */
const QUICK_ACTION_STYLE = isLiquidGlassAvailable() ? "glass" : "bordered";

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
          : [tint(toneColor(theme, action.tone ?? "accent"))]
      }
    />
  ));
}

function ItemRow({ row }: { row: NativeFormItemRow }): React.JSX.Element {
  const { theme } = useTheme();
  const leading = [row.leadingText, row.leadingCaption]
    .filter(Boolean)
    .join(" ");
  const label = [leading, row.title, row.subtitle].filter(Boolean).join(", ");
  const leadingColor = toneColor(theme, row.leadingTone ?? "default");
  const content = (
    <HStack spacing={12}>
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
      ) : row.symbol ? (
        <Image
          systemName={row.symbol}
          size={15}
          color="#FFFFFF"
          modifiers={[
            frame({ width: 30, height: 30 }),
            background(
              row.tint ?? theme.primary,
              shapes.roundedRectangle({
                cornerRadius: 8,
                roundedCornerStyle: "continuous",
              }),
            ),
          ]}
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
      {row.badge ? (
        <Text
          modifiers={[
            font({ textStyle: "caption", weight: "semibold" }),
            foregroundStyle(theme.primary),
          ]}
        >
          {row.badge}
        </Text>
      ) : null}
      {row.selected ? (
        <Image systemName="checkmark" size={15} color={theme.primary} />
      ) : null}
    </HStack>
  );
  const body = (
    <HStack spacing={8}>
      <Button
        onPress={row.onPress}
        testID={row.testID ?? row.id}
        modifiers={[
          buttonStyle("borderless"),
          disabled(Boolean(row.disabled || !row.onPress)),
          accessibilityLabel(label),
          frame({ maxWidth: Infinity, alignment: "leading", minHeight: 44 }),
        ]}
      >
        {content}
      </Button>
      {row.onInfo ? (
        <Button
          onPress={row.onInfo}
          modifiers={[
            buttonStyle("borderless"),
            accessibilityLabel(row.infoLabel ?? `Details for ${row.title}`),
          ]}
        >
          <Image systemName="info.circle" size={20} color={theme.primary} />
        </Button>
      ) : null}
    </HStack>
  );
  const menu = row.actions?.length ? (
    <ContextMenu>
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
  if (!row.leadingSwipe?.length && !row.trailingSwipe?.length) return menu;
  return (
    <SwipeActions>
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

/** Hero header with liquid-glass quick actions, as in Contacts or Maps. */
function HeaderRow({ row }: { row: NativeFormHeaderRow }): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack
      spacing={14}
      modifiers={[
        frame({ maxWidth: Infinity }),
        listRowBackground("#00000000"),
        listRowInsets({ top: 4, leading: 0, bottom: 4, trailing: 0 }),
      ]}
    >
      <Image
        systemName={row.symbol}
        size={30}
        color="#FFFFFF"
        modifiers={[
          frame({ width: 64, height: 64 }),
          background(
            row.tint,
            shapes.roundedRectangle({
              cornerRadius: 16,
              roundedCornerStyle: "continuous",
            }),
          ),
        ]}
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
          <HStack spacing={6}>
            {row.busy ? (
              <ProgressView modifiers={[controlSize("small")]} />
            ) : null}
            <Text
              modifiers={[
                font({ textStyle: "subheadline" }),
                foregroundStyle(toneColor(theme, row.subtitleTone)),
              ]}
            >
              {row.subtitle}
            </Text>
          </HStack>
        ) : null}
      </VStack>
      {row.actions.length ? (
        <GlassEffectContainer spacing={10}>
          <HStack spacing={10}>
            {row.actions.map((action) => (
              <Button
                key={action.id}
                onPress={() => row.onAction(action.id)}
                testID={`${row.id}-${action.id}`}
                role={action.destructive ? "destructive" : undefined}
                modifiers={[
                  buttonStyle(QUICK_ACTION_STYLE),
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
                    size={19}
                    color={
                      action.destructive
                        ? theme.danger
                        : action.disabled
                          ? theme.subtleText
                          : theme.primary
                    }
                  />
                  <Text
                    modifiers={[
                      font({ textStyle: "caption2", weight: "medium" }),
                      foregroundStyle(
                        action.disabled ? theme.subtleText : theme.text,
                      ),
                      lineLimit(1),
                    ]}
                  >
                    {action.title}
                  </Text>
                </VStack>
              </Button>
            ))}
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

function TextRow({ row }: { row: NativeFormTextRow }): React.JSX.Element {
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
      ]}
    />
  );
}

/** A plain symbol in the text color, or a white symbol on a colored tile. */
function LeadingSymbol({
  symbol,
  tint: color,
}: {
  symbol: NativeSymbol;
  tint?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  if (color)
    return (
      <Image
        systemName={symbol}
        size={15}
        color="#FFFFFF"
        modifiers={[
          frame({ width: 30, height: 30 }),
          background(
            color,
            shapes.roundedRectangle({
              cornerRadius: 8,
              roundedCornerStyle: "continuous",
            }),
          ),
        ]}
      />
    );
  return (
    <Image
      systemName={symbol}
      modifiers={[
        font({ textStyle: "body" }),
        foregroundStyle(theme.text),
        frame({ width: 28 }),
      ]}
    />
  );
}

/** Pushes the row's page onto the settings navigation stack. */
function LinkRow({ row }: { row: NativeFormLinkRow }): React.JSX.Element {
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
      ]}
    >
      <HStack spacing={14} modifiers={[frame({ minHeight: 36 })]}>
        {row.symbol ? (
          <LeadingSymbol symbol={row.symbol} tint={row.tint} />
        ) : null}
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

/** The account's large initials avatar and name, centered above the sections. */
function ProfileRow({ row }: { row: NativeFormProfileRow }): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack
      spacing={12}
      modifiers={[
        frame({ maxWidth: Infinity }),
        listRowBackground("#00000000"),
        listRowInsets({ top: 4, leading: 0, bottom: 8, trailing: 0 }),
        accessibilityLabel(
          [row.title, row.subtitle].filter(Boolean).join(", "),
        ),
      ]}
    >
      <Text
        modifiers={[
          font({ size: 34, weight: "medium" }),
          foregroundStyle("#FFFFFF"),
          frame({ width: 88, height: 88 }),
          background(row.tint, shapes.circle()),
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

/** A promoted card with a capsule button (or menu), like ChatGPT's plan card. */
function CardRow({ row }: { row: NativeFormCardRow }): React.JSX.Element {
  const { theme } = useTheme();
  const capsule = [
    buttonStyle("borderedProminent"),
    buttonBorderShape("capsule"),
    controlSize("large"),
    tint(theme.text),
    disabled(Boolean(row.disabled)),
  ];
  const label = (
    <Text
      modifiers={[
        font({ textStyle: "body", weight: "semibold" }),
        foregroundStyle(theme.background),
      ]}
    >
      {row.actionTitle}
    </Text>
  );
  return (
    <HStack
      spacing={12}
      modifiers={[padding({ vertical: 6 }), frame({ minHeight: 64 })]}
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
}: {
  row: NativeFormWeekdaysRow;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <VStack alignment="leading" spacing={10}>
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
function DateRow({ row }: { row: NativeFormDateRow }): React.JSX.Element {
  const { theme } = useTheme();
  const picker = (
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
      modifiers={[disabled(Boolean(row.disabled))]}
    />
  );
  if (!row.onRemove) return picker;
  return (
    <HStack spacing={12}>
      <Button
        onPress={row.onRemove}
        testID={`${row.testID ?? row.id}-remove`}
        modifiers={[
          buttonStyle("plain"),
          disabled(Boolean(row.disabled)),
          accessibilityLabel(`Remove ${row.label}`),
        ]}
      >
        <Image systemName="minus.circle.fill" size={20} color={theme.danger} />
      </Button>
      {picker}
    </HStack>
  );
}

function Row({ row }: { row: NativeFormRow }): React.JSX.Element {
  const { theme } = useTheme();
  switch (row.kind) {
    case "item":
      return <ItemRow row={row} />;
    case "header":
      return <HeaderRow row={row} />;
    case "link":
      return <LinkRow row={row} />;
    case "profile":
      return <ProfileRow row={row} />;
    case "card":
      return <CardRow row={row} />;
    case "text":
      return <TextRow row={row} />;
    case "toggle":
      return (
        <Toggle
          isOn={row.value}
          testID={row.testID ?? row.id}
          onIsOnChange={row.onChange}
          modifiers={[
            disabled(Boolean(row.disabled)),
            accessibilityLabel(
              [row.label, row.subtitle].filter(Boolean).join(", "),
            ),
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
            disabled(Boolean(row.disabled)),
            accessibilityLabel(row.label),
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
        <HStack>
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
            modifiers={[accessibilityLabel(`${row.label}, ${row.value}`)]}
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
                size={13}
                color={theme.subtleText}
              />
            </HStack>
          </Button>
        );
      return (
        <HStack
          spacing={8}
          modifiers={[accessibilityLabel(`${row.label}, ${row.value}`)]}
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
      return <WeekdaysRow row={row} />;
    case "date":
      return <DateRow row={row} />;
    case "gauge":
      return (
        <VStack alignment="leading" spacing={6}>
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
                row.tone === "danger" || row.tone === "warning"
                  ? toneColor(theme, row.tone)
                  : theme.primary,
              ),
              accessibilityLabel(`${row.label}, ${row.valueLabel}`),
            ]}
          />
        </VStack>
      );
    case "button":
      return (
        <Button
          onPress={row.onPress}
          testID={row.testID ?? row.id}
          role={row.destructive ? "destructive" : undefined}
          modifiers={[
            disabled(Boolean(row.disabled || row.busy)),
            accessibilityLabel(row.title),
          ]}
        >
          <HStack spacing={8}>
            {row.symbol ? (
              <Image
                systemName={row.symbol}
                size={17}
                color={row.destructive ? theme.danger : theme.primary}
              />
            ) : null}
            <Text
              modifiers={[
                foregroundStyle(
                  row.disabled
                    ? theme.subtleText
                    : row.destructive
                      ? theme.danger
                      : theme.primary,
                ),
              ]}
            >
              {row.title}
            </Text>
            {row.busy ? (
              <>
                <Spacer />
                <ProgressView />
              </>
            ) : null}
          </HStack>
        </Button>
      );
    case "note":
      return (
        <Text
          modifiers={[
            font({ textStyle: "footnote" }),
            foregroundStyle(toneColor(theme, row.tone)),
          ]}
        >
          {row.text}
        </Text>
      );
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
      modifiers={section.prominent ? [headerProminence("increased")] : []}
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
 * The SwiftUI Form without a host, for screens that already run inside
 * SwiftUI, e.g. a page of the settings navigation stack.
 */
export function NativeFormContent({
  sections,
  onRefresh,
  empty,
}: Pick<
  NativeFormProps,
  "sections" | "onRefresh" | "empty"
>): React.JSX.Element {
  if (!sections.length && empty)
    return (
      <ContentUnavailableView
        title={empty.title}
        systemImage={empty.symbol}
        description={empty.description}
      />
    );
  return (
    <Form
      modifiers={[
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
      />
    </Host>
  );
}
