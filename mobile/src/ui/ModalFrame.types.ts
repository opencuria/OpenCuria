import type React from "react";
import type { NativeMenuAction } from "./NativeMenu";

export interface ModalFrameProps {
  title: string;
  /** Second header line, e.g. the folder of an opened file. */
  subtitle?: string;
  onClose?: () => void;
  /** Close sits trailing (confirmation) by default, leading for viewers. */
  closePlacement?: "leading" | "trailing";
  /** Overflow ("…") menu at the trailing edge. */
  actions?: NativeMenuAction[];
  onAction?: (id: string) => void;
  children: React.ReactNode;
}
