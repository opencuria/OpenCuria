import type React from "react";
import type { ModalPrimaryAction as Action } from "./ModalFrame.types";

/** iOS shows the primary action in the navigation bar (see `ModalFrame.ios`). */
export function ModalPrimaryAction(_props: {
  action: Action;
}): React.JSX.Element | null {
  return null;
}
