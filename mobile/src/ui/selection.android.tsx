import React, { useEffect, useState } from "react";
import {
  Box,
  Checkbox,
  Column,
  DropdownMenuItem,
  ExposedDropdownMenu,
  ExposedDropdownMenuBox,
  OutlinedTextField,
  useNativeState,
  Host,
  Icon,
  RadioButton,
  Row,
  Text,
} from "@expo/ui/jetpack-compose";
import {
  Shapes,
  background,
  clip,
  dropShadow,
  fillMaxHeight,
  height,
  menuAnchor,
  padding,
  paddingAll,
  selectable,
  selectableGroup,
  testID,
  toggleable,
  fillMaxWidth,
  semantics,
  tween,
  weight,
} from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import {
  fieldSelectionColors,
  outlinedFieldColors,
  outlinedFieldShape,
} from "./NativeTextField.android";
import type {
  NativeOptionProps,
  NativePickerProps,
  NativeSegmentedPickerProps,
  OptionRowMetrics,
} from "./selection.types";
import { useTheme } from "./theme";

/** A 24dp radio or checkbox beside a bodyLarge label, as in a Material list. */
export const OPTION_ROW: OptionRowMetrics = {
  inset: 16,
  indicator: 24,
  gap: 16,
  minHeight: 48,
  fontSize: 16,
  lineHeight: 24,
};

const SEGMENT_SHAPE = Shapes.RoundedCorner(9);
const TRANSPARENT = "#00000000";

/**
 * Compose segmented choice: Material's SegmentedButton reserves a checkmark
 * and a 58dp minimum per segment, so five or more labels no longer fit a
 * phone. Each segment is a selectable radio in a selectable group. Renders
 * inside an existing Compose host, e.g. a form row.
 */
export function SegmentedChoiceRow(
  props: NativeSegmentedPickerProps & { disabled?: boolean },
): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Row
      verticalAlignment="center"
      horizontalArrangement={{ spacedBy: 2 }}
      modifiers={[
        fillMaxWidth(),
        height(40),
        clip(Shapes.RoundedCorner(12)),
        background(theme.controlFill),
        paddingAll(3),
        selectableGroup(),
        ...(props.testID ? [testID(props.testID)] : []),
      ]}
    >
      {props.options.map((option) => {
        const selected = option.value === props.value;
        return (
          <Box
            key={option.value}
            contentAlignment="center"
            modifiers={[
              weight(1),
              fillMaxHeight(),
              ...(selected
                ? [
                    dropShadow(SEGMENT_SHAPE, {
                      radius: 3,
                      offsetY: 1,
                      alpha: 0.14,
                    }),
                  ]
                : []),
              clip(SEGMENT_SHAPE),
              background(selected ? theme.raised : TRANSPARENT, {
                animationSpec: tween({ durationMillis: 150 }),
              }),
              ...(props.disabled
                ? []
                : [
                    selectable(
                      selected,
                      () => {
                        if (!selected) props.onChange(option.value);
                      },
                      "radioButton",
                    ),
                  ]),
              semantics({
                contentDescription: option.accessibilityLabel ?? option.label,
              }),
            ]}
          >
            <Text
              color={
                props.disabled
                  ? theme.subtleText
                  : selected
                    ? theme.text
                    : theme.mutedText
              }
              maxLines={1}
              softWrap={false}
              overflow="ellipsis"
              style={{ fontSize: 14, fontWeight: selected ? "600" : "500" }}
            >
              {option.label}
            </Text>
          </Box>
        );
      })}
    </Row>
  );
}

export function NativeSegmentedPicker(
  props: NativeSegmentedPickerProps,
): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.seed}
      ignoreSafeAreaKeyboardInsets
    >
      <SegmentedChoiceRow {...props} />
    </Host>
  );
}

export function NativePicker(props: NativePickerProps): React.JSX.Element {
  const { theme } = useTheme();
  const [expanded, setExpanded] = useState(false);
  const selectedLabel =
    props.options.find((option) => option.value === props.value)?.label ?? "";
  const value = useNativeState(selectedLabel);
  useEffect(() => value.set(selectedLabel), [value, selectedLabel]);
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.seed}
      ignoreSafeAreaKeyboardInsets
    >
      <ExposedDropdownMenuBox
        expanded={expanded}
        onExpandedChange={props.disabled ? undefined : setExpanded}
      >
        <OutlinedTextField
          value={value}
          readOnly
          enabled={!props.disabled}
          shape={outlinedFieldShape()}
          colors={outlinedFieldColors(theme)}
          textSelectionColors={fieldSelectionColors(theme)}
          modifiers={[
            fillMaxWidth(),
            menuAnchor(),
            semantics({ contentDescription: props.label }),
          ]}
        >
          <OutlinedTextField.Label>
            <Text>{props.label}</Text>
          </OutlinedTextField.Label>
          <OutlinedTextField.TrailingIcon>
            <Icon
              source={
                expanded
                  ? require("../../assets/native-icons/expand-less.xml")
                  : require("../../assets/native-icons/expand-more.xml")
              }
              size={24}
            />
          </OutlinedTextField.TrailingIcon>
        </OutlinedTextField>
        <ExposedDropdownMenu
          expanded={expanded}
          containerColor={theme.raised}
          onDismissRequest={() => setExpanded(false)}
        >
          {props.options.map((option) => (
            <DropdownMenuItem
              key={option.value}
              enabled={!option.disabled}
              elementColors={{
                textColor: theme.text,
                disabledTextColor: theme.subtleText,
                trailingIconColor: theme.text,
              }}
              onClick={() => {
                setExpanded(false);
                props.onChange(option.value);
              }}
            >
              <DropdownMenuItem.Text>
                <Text
                  style={{
                    fontWeight: option.value === props.value ? "600" : "400",
                  }}
                >
                  {option.label}
                </Text>
              </DropdownMenuItem.Text>
              {option.value === props.value ? (
                <DropdownMenuItem.TrailingIcon>
                  <Icon source={androidIcon("checkmark")!} size={20} />
                </DropdownMenuItem.TrailingIcon>
              ) : null}
            </DropdownMenuItem>
          ))}
        </ExposedDropdownMenu>
      </ExposedDropdownMenuBox>
    </Host>
  );
}

/**
 * A choice row with a leading radio (one choice) or checkbox (any number).
 * Its height follows the label and description, unlike a Material list item
 * with its fixed two- and three-line heights; the whole row is the target.
 */
export function NativeOption(props: NativeOptionProps): React.JSX.Element {
  const { theme } = useTheme();
  const vertical = (OPTION_ROW.minHeight - OPTION_ROW.lineHeight) / 2;
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.seed}
      ignoreSafeAreaKeyboardInsets
    >
      <Row
        verticalAlignment="top"
        horizontalArrangement={{ spacedBy: OPTION_ROW.gap }}
        modifiers={[
          fillMaxWidth(),
          semantics({
            contentDescription: [props.label, props.description]
              .filter(Boolean)
              .join(", "),
          }),
          ...(!props.disabled
            ? [
                props.multiple
                  ? toggleable(props.selected, props.onPress, {
                      role: "checkbox",
                    })
                  : selectable(props.selected, props.onPress, "radioButton"),
              ]
            : []),
          padding(OPTION_ROW.inset, vertical, OPTION_ROW.inset, vertical),
        ]}
      >
        {props.multiple ? (
          <Checkbox
            value={props.selected}
            enabled={!props.disabled}
            colors={{
              checkedColor: theme.primary,
              checkmarkColor: theme.primaryForeground,
              uncheckedColor: theme.mutedText,
              disabledCheckedColor: theme.subtleText,
              disabledUncheckedColor: theme.subtleText,
            }}
          />
        ) : (
          <RadioButton
            selected={props.selected}
            enabled={!props.disabled}
            colors={{
              selectedColor: theme.primary,
              unselectedColor: theme.mutedText,
              disabledSelectedColor: theme.subtleText,
              disabledUnselectedColor: theme.subtleText,
            }}
          />
        )}
        <Column
          verticalArrangement={{ spacedBy: 2 }}
          modifiers={[weight(1)]}
        >
          <Text
            color={props.disabled ? theme.subtleText : theme.text}
            style={{
              fontSize: OPTION_ROW.fontSize,
              lineHeight: OPTION_ROW.lineHeight,
            }}
          >
            {props.label}
          </Text>
          {props.description ? (
            <Text
              color={theme.mutedText}
              style={{ fontSize: 14, lineHeight: 20 }}
            >
              {props.description}
            </Text>
          ) : null}
        </Column>
      </Row>
    </Host>
  );
}
