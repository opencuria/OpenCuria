import type { ModalPrimaryAction, ModalSearch } from "../ui/ModalFrame.types";
import type {
  NativeFormProps,
  NativeFormSection,
} from "../ui/NativeForm.types";
import type { NativeMenuAction } from "../ui/NativeMenu";

/** One page of the settings navigation stack. */
export interface SettingsPageProps {
  title: string;
  sections: NativeFormSection[];
  /**
   * The first page: closes Settings with a liquid-glass × at the leading
   * edge (as every sheet does) instead of going back.
   */
  root?: boolean;
  /** "Save", "Add" (+) and similar: navigation bar on iOS, button or FAB on Android. */
  primaryAction?: ModalPrimaryAction;
  /** Overflow ("…") menu. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  onRefresh?: () => Promise<void>;
  empty?: NativeFormProps["empty"];
  /** Shows a spinner until the page's first load finished. */
  loading?: boolean;
  /** Native search: in the navigation bar on iOS, a docked bar on Android. */
  search?: ModalSearch;
  testID?: string;
}
