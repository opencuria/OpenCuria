import React, { useEffect, useState } from "react";
import {
  Checkbox,
  DropdownMenuItem,
  ExposedDropdownMenu,
  ExposedDropdownMenuBox,
  TextField,
  useNativeState,
  Host,
  ListItem,
  RadioButton,
  Text,
} from "@expo/ui/jetpack-compose";
import {
  menuAnchor,
  selectable,
  toggleable,
  fillMaxWidth,
  semantics,
} from "@expo/ui/jetpack-compose/modifiers";
import type { NativeOptionProps, NativePickerProps } from "./selection.types";
import { useTheme } from "./theme";

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
