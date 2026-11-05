import type { NativeFormSection } from "../ui/NativeForm.types";
import { autosaveText, type Autosave } from "./useAutosave";

/** A page that could not load: the reason and a way to try again. */
export function loadErrorSections(
  error: string,
  retry: () => void,
): NativeFormSection[] {
  return [
    {
      id: "load-error",
      footer: error,
      footerTone: "danger",
      rows: [
        {
          kind: "button",
          id: "reload",
          title: "Try again",
          symbol: "arrow.clockwise",
          onPress: retry,
        },
      ],
    },
  ];
}

/**
 * The save state below automatically saved settings, with Retry after a
 * failure; read-only viewers get `readOnlyNote` instead.
 */
export function autosaveSection(
  autosave: Autosave,
  options: { editable?: boolean; readOnlyNote?: string; id?: string } = {},
): NativeFormSection {
  const id = options.id ?? "save-status";
  if (options.editable === false)
    return { id, footer: options.readOnlyNote, rows: [] };
  return {
    id,
    footer: autosaveText(autosave),
    footerTone: autosave.status === "error" ? "danger" : "muted",
    rows:
      autosave.status === "error"
        ? [
            {
              kind: "button",
              id: `${id}-retry`,
              title: "Retry",
              symbol: "arrow.clockwise",
              onPress: autosave.retry,
            },
          ]
        : [],
  };
}
