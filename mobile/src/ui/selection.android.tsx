import React, { useEffect, useState } from "react";
import {
  Box,
  Checkbox,
  DropdownMenuItem,
  ExposedDropdownMenu,
  ExposedDropdownMenuBox,
  TextField,
  useNativeState,
  Host,
  ListItem,
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
import type {
  NativeOptionProps,
  NativePickerProps,
  NativeSegmentedPickerProps,
} from "./selection.types";
import { useTheme } from "./theme";

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
      seedColor={theme.primary}
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
      seedColor={theme.primary}
      ignoreSafeAreaKeyboardInsets
    >
      <ExposedDropdownMenuBox
        expanded={expanded}
        onExpandedChange={props.disabled ? undefined : setExpanded}
      >
        <TextField
          value={value}
          readOnly
          enabled={!props.disabled}
          modifiers={[
            fillMaxWidth(),
            menuAnchor(),
            semantics({ contentDescription: props.label }),
          ]}
        >
          <TextField.Label>
            <Text>{props.label}</Text>
          </TextField.Label>
        </TextField>
        <ExposedDropdownMenu
          expanded={expanded}
          onDismissRequest={() => setExpanded(false)}
        >
          {props.options.map((option) => (
            <DropdownMenuItem
              key={option.value}
              enabled={!option.disabled}
              onClick={() => {
                setExpanded(false);
                props.onChange(option.value);
              }}
            >
              <DropdownMenuItem.Text>
                <Text>{option.label}</Text>
              </DropdownMenuItem.Text>
            </DropdownMenuItem>
          ))}
        </ExposedDropdownMenu>
      </ExposedDropdownMenuBox>
    </Host>
  );
}

export function NativeOption(props: NativeOptionProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.primary}
      ignoreSafeAreaKeyboardInsets
    >
      <ListItem
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
        ]}
      >
        <ListItem.LeadingContent>
          {props.multiple ? (
            <Checkbox value={props.selected} enabled={!props.disabled} />
          ) : (
            <RadioButton selected={props.selected} enabled={!props.disabled} />
          )}
        </ListItem.LeadingContent>
        <ListItem.HeadlineContent>
          <Text>{props.label}</Text>
        </ListItem.HeadlineContent>
        {props.description ? (
          <ListItem.SupportingContent>
            <Text>{props.description}</Text>
          </ListItem.SupportingContent>
        ) : null}
      </ListItem>
    </Host>
  );
}
