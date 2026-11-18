import type {
  NativeFormEmptyState,
  NativeFormProps,
  NativeFormRow,
} from "./NativeForm.types";

/**
 * An inline problem inside a list that still shows content: the message as
 * a danger note led by a warning symbol, then Retry. The one pattern for
 * errors next to content on every sheet and settings page.
 */
export function errorRows(
  id: string,
  message: string,
  retry: () => void,
  busy = false,
): NativeFormRow[] {
  return [
    {
      kind: "note",
      id: `${id}-error`,
      text: message,
      tone: "danger",
      symbol: "exclamationmark.triangle",
    },
    {
      kind: "button",
      id: `${id}-retry`,
      title: "Retry",
      symbol: "arrow.clockwise",
      busy,
      onPress: retry,
    },
  ];
}

/**
 * The centered state of a list or page that could not load and has nothing
 * to show: "Couldn’t load <things>", the reason and Retry.
 */
export function loadErrorState(
  things: string,
  error: string,
  retry: () => void,
  busy = false,
): NativeFormEmptyState {
  return {
    title: `Couldn’t load ${things}`,
    description: error,
    symbol: "exclamationmark.triangle",
    action: {
      title: "Retry",
      symbol: "arrow.clockwise",
      busy,
      onPress: retry,
    },
  };
}

/** Whether a form currently shows its centered empty state with an action. */
export function showsEmptyAction(
  form: Pick<NativeFormProps, "sections" | "empty" | "loading">,
): boolean {
  return Boolean(!form.sections.length && !form.loading && form.empty?.action);
}

/**
 * One main action per screen: while the centered empty state offers the
 * same action (e.g. "New workspace"), the toolbar button or floating action
 * button is hidden. A different empty action, such as Retry after a load
 * failure, keeps it.
 */
export function visiblePrimaryAction<T extends { title: string }>(
  action: T | undefined,
  form: Pick<NativeFormProps, "sections" | "empty" | "loading"> | undefined,
): T | undefined {
  return action &&
    form &&
    showsEmptyAction(form) &&
    form.empty?.action?.title === action.title
    ? undefined
    : action;
}
