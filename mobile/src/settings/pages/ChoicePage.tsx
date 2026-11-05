import React, { useState } from "react";
import type { NativeFormSection } from "../../ui/NativeForm.types";
import { useSettingsNavigation, type SettingsChoice } from "../SettingsContext";
import { SettingsPage } from "../SettingsPage";

/** One value from a list, like a picker pushed in the Settings app. */
export function ChoicePage({
  choice,
}: {
  choice: SettingsChoice;
}): React.JSX.Element {
  const navigation = useSettingsNavigation();
  const [query, setQuery] = useState("");
  const term = query.trim().toLowerCase();
  const sections: NativeFormSection[] = choice.groups
    .map((group, index) => ({
      id: `group-${index}`,
      title: group.title,
      rows: group.options
        .filter(
          (option) =>
            !term ||
            `${option.label} ${option.subtitle ?? ""} ${option.value}`
              .toLowerCase()
              .includes(term),
        )
        .map((option) => ({
          kind: "item" as const,
          id: `choice-${option.value || "none"}`,
          title: option.label,
          subtitle: option.subtitle,
          selected: option.value === choice.value,
          disabled: option.disabled,
          onPress: () => {
            choice.onSelect(option.value);
            navigation.pop();
          },
        })),
    }))
    .filter((section) => section.rows.length);
  if (sections.length && choice.footer)
    sections[sections.length - 1] = {
      ...sections[sections.length - 1]!,
      footer: choice.footer,
    };
  return (
    <SettingsPage
      title={choice.title}
      sections={sections}
      search={
        choice.searchPlaceholder
          ? {
              value: query,
              onChange: setQuery,
              placeholder: choice.searchPlaceholder,
            }
          : undefined
      }
      empty={{
        title: term ? "No results" : "Nothing to choose",
        description: term
          ? `Nothing matches “${query.trim()}”.`
          : choice.emptyText,
        symbol: term ? "magnifyingglass" : "info.circle",
      }}
      testID="settings-choice"
    />
  );
}
