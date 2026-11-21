import React, { useEffect, useRef } from "react";
import {
  Host,
  Icon,
  IconButton,
  Shape,
  Text,
  TextField,
  useNativeState,
  type TextFieldRef,
} from "@expo/ui/jetpack-compose";
import {
  fillMaxWidth,
  padding,
  semantics,
  type ModifierConfig,
} from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import type { ModalSearch } from "./ModalFrame.types";
import { useTheme } from "./theme";

const TRANSPARENT = "#00000000";

/**
 * Search below the toolbar of sheets and settings pages: a monochrome filled
 * pill instead of the seed-tinted Material search bar.
 */
export function SearchField({
  search,
  accessibilityLabel,
  autoFocus,
  modifiers = [fillMaxWidth(), padding(16, 4, 16, 8)],
}: {
  search: ModalSearch;
  /** Spoken name; defaults to the placeholder. */
  accessibilityLabel?: string;
  autoFocus?: boolean;
  /** Size and outer padding; full width below a toolbar by default. */
  modifiers?: ModifierConfig[];
}): React.JSX.Element {
  const { theme } = useTheme();
  const text = useNativeState(search.value);
  const latest = useRef(search.value);
  const field = useRef<TextFieldRef>(null);
  useEffect(() => {
    if (search.value !== latest.current) {
      latest.current = search.value;
      text.set(search.value);
    }
  }, [search.value, text]);
  const change = (value: string) => {
    latest.current = value;
    search.onChange(value);
  };
  return (
    <Host
      matchContents={{ vertical: true }}
      colorScheme={theme.scheme}
      seedColor={theme.seed}
      ignoreSafeAreaKeyboardInsets
    >
      <TextField
        ref={field}
        value={text}
        autoFocus={autoFocus}
        singleLine
        shape={Shape.Pill({})}
        colors={{
          focusedContainerColor: theme.controlFill,
          unfocusedContainerColor: theme.controlFill,
          focusedIndicatorColor: TRANSPARENT,
          unfocusedIndicatorColor: TRANSPARENT,
          cursorColor: theme.text,
          focusedTextColor: theme.text,
          unfocusedTextColor: theme.text,
          focusedPlaceholderColor: theme.mutedText,
          unfocusedPlaceholderColor: theme.mutedText,
          focusedLeadingIconColor: theme.mutedText,
          unfocusedLeadingIconColor: theme.mutedText,
          focusedTrailingIconColor: theme.mutedText,
          unfocusedTrailingIconColor: theme.mutedText,
        }}
        textSelectionColors={{
          handleColor: theme.text,
          backgroundColor: theme.separator,
        }}
        keyboardOptions={{
          capitalization: "none",
          autoCorrectEnabled: false,
          imeAction: "search",
        }}
        keyboardActions={{ onSearch: () => void field.current?.blur() }}
        onValueChange={change}
        modifiers={[
          ...modifiers,
          semantics({
            contentDescription: accessibilityLabel ?? search.placeholder,
          }),
        ]}
      >
        <TextField.LeadingIcon>
          <Icon source={androidIcon("magnifyingglass")!} size={24} />
        </TextField.LeadingIcon>
        <TextField.Placeholder>
          <Text>{search.placeholder}</Text>
        </TextField.Placeholder>
        {search.value ? (
          <TextField.TrailingIcon>
            <IconButton
              onClick={() => {
                text.set("");
                change("");
              }}
              colors={{ contentColor: theme.mutedText }}
              modifiers={[semantics({ contentDescription: "Clear search" })]}
            >
              <Icon source={androidIcon("xmark")!} size={24} />
            </IconButton>
          </TextField.TrailingIcon>
        ) : null}
      </TextField>
    </Host>
  );
}
