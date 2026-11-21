import { errorRows, loadErrorState } from "../ui/formStates";
import type {
  NativeFormEmptyState,
  NativeFormSection,
} from "../ui/NativeForm.types";
import { autosaveText, type Autosave } from "./useAutosave";

/**
 * The centered state of a page whose data could not load while it has
 * nothing to show: "Couldn’t load <things>", the reason and Retry. Pages
 * pass it as `empty` together with no sections; `undefined` without error.
 */
export function loadFailure(
  things: string,
  resource: { error: string | null; reload: () => Promise<unknown> },
): NativeFormEmptyState | undefined {
  return resource.error
    ? loadErrorState(things, resource.error, () => void resource.reload())
    : undefined;
}

/**
 * A refresh that failed while the page still shows older data: the reason
 * and Retry above the list, as in the sheets. None without such a failure.
 */
export function refreshError(
  id: string,
  resource: {
    data: unknown;
    error: string | null;
    loading: boolean;
    reload: () => Promise<unknown>;
  },
): NativeFormSection[] {
  return resource.error && resource.data !== undefined
    ? [
        {
          id: `${id}-refresh`,
          rows: errorRows(
            id,
            resource.error,
            () => void resource.reload(),
            resource.loading,
          ),
        },
      ]
    : [];
}

/**
 * A section whose data is still loading (a note led by a spinner) or could
 * not load (the reason and Retry), e.g. the permissions of an API key.
 */
export function pendingSection(
  id: string,
  {
    loading,
    error,
    retry,
  }: { loading: string; error: string | null; retry: () => void },
): NativeFormSection {
  return {
    id,
    rows: error
      ? errorRows(id, error, retry)
      : [{ kind: "note", id: `${id}-loading`, text: loading, busy: true }],
  };
}

/**
 * The save state below automatically saved settings; after a failure the
 * reason and Retry.
 */
export function autosaveSection(
  autosave: Autosave,
  options: { id?: string } = {},
): NativeFormSection {
  const id = options.id ?? "save-status";
  return autosave.status === "error"
    ? { id, rows: errorRows(id, autosaveText(autosave), autosave.retry) }
    : { id, footer: autosaveText(autosave), footerTone: "muted", rows: [] };
}
