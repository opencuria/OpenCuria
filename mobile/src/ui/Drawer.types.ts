import type React from "react";
import type { NativeMenuAction } from "./NativeMenu";

export interface DrawerProps {
  visible: boolean;
  onOpen: () => void;
  onClose: () => void;
  /** Current workspace; titles the overflow menu instead of the chat header. */
  title: string;
  navigation: React.ReactNode;
  children: React.ReactNode;
  profileTitle: string;
  onProfile: () => void;
  onCreate: () => void;
  canCreate: boolean;
  /** Actions of the chat header's overflow ("…") menu. */
  menuActions: NativeMenuAction[];
  onMenuAction: (id: string) => void;
}
