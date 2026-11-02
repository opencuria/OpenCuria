import React, { useEffect, useRef } from "react";
import { isLiquidGlassAvailable } from "expo-glass-effect";
import {
  Button,
  ContentUnavailableView,
  ContextMenu,
  Form,
  Gauge,
  GlassEffectContainer,
  HStack,
  Host,
  Image,
  LabeledContent,
  Picker,
  ProgressView,
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
  accessibilityLabel,
  autocorrectionDisabled,
  background,
  buttonStyle,
  controlSize,
  disabled,
  font,
  foregroundStyle,
  frame,
  gaugeStyle,
  keyboardType,
  labelsHidden,
  lineLimit,
  listRowBackground,
  listRowInsets,
  monospacedDigit,
  multilineTextAlignment,
  onSubmit,
  pickerStyle,
  refreshable,
  shapes,
  submitLabel,
  tag,
  textInputAutocapitalization,
  tint,
} from "@expo/ui/swift-ui/modifiers";
import type {
  NativeFormHeaderRow,
  NativeFormItemRow,
  NativeFormProps,
  NativeFormRow,
  NativeFormSwipeAction,
  NativeFormTextRow,
  NativeFormTone,
} from "./NativeForm.types";
import { useTheme, type OpenCuriaTheme } from "./theme";

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
  const label = [row.title, row.subtitle].filter(Boolean).join(", ");
  const content = (
    <HStack spacing={12}>
      {row.symbol ? (
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

function TextRow({ row }: { row: NativeFormTextRow }): React.JSX.Element {
  const text = useNativeState(row.value);
  const latest = useRef(row.value);
  useEffect(() => {
    if (row.value !== latest.current) {
      latest.current = row.value;
      text.set(row.value);
    }
  }, [row.value, text]);
  const field = (
    <TextField
      text={text}
      testID={row.testID ?? row.id}
      placeholder={row.placeholder ?? row.label}
      autoFocus={row.autoFocus}
      axis={row.multiline ? "vertical" : "horizontal"}
      onTextChange={(value) => {
        latest.current = value;
        row.onChange(value);
      }}
      modifiers={[
        accessibilityLabel(row.label),
        disabled(Boolean(row.disabled)),
        keyboardType(row.keyboard === "url" ? "url" : "default"),
        autocorrectionDisabled(),
        textInputAutocapitalization("never"),
        submitLabel("done"),
        onSubmit(() => row.onSubmit?.()),
        ...(row.multiline ? [lineLimit({ min: 2, max: 6 })] : []),
      ]}
    />
  );
  return field;
}

function Row({ row }: { row: NativeFormRow }): React.JSX.Element {
  const { theme } = useTheme();
  switch (row.kind) {
    case "item":
      return <ItemRow row={row} />;
    case "header":
      return <HeaderRow row={row} />;
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
        </Toggle>
      );
    case "picker":
      return (
        <Picker
          label={row.label}
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
    case "value":
      return (
        <LabeledContent label={row.label}>
          <Text
            modifiers={[
              foregroundStyle(toneColor(theme, row.tone)),
              multilineTextAlignment("trailing"),
            ]}
          >
            {row.value}
          </Text>
        </LabeledContent>
      );
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
      {!sections.length && empty ? (
        <ContentUnavailableView
          title={empty.title}
          systemImage={empty.symbol}
          description={empty.description}
        />
      ) : (
        <Form modifiers={onRefresh ? [refreshable(onRefresh)] : []}>
          {sections.map((section) => (
            <Section
              key={section.id}
              title={section.title}
              footer={
                section.footer ? (
                  <Text
                    modifiers={[
                      foregroundStyle(
                        toneColor(theme, section.footerTone ?? "muted"),
                      ),
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
          ))}
        </Form>
      )}
    </Host>
  );
}
