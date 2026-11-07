import type React from "react";
import type { NativeMenuAction } from "./NativeMenu";

export interface DrawerProps {
  visible: boolean;
  onOpen: () => void;
  onClose: () => void;
  /** Current workspace; titles the overflow menu instead of the chat header. */
  title: string;
  /** Sidebar content, including its own account and New chat controls. */
  navigation: React.ReactNode;
  children: React.ReactNode;
  onCreate: () => void;
  canCreate: boolean;
  /** Opens Git, Desktop, Terminal & Files; sits between New chat and the overflow menu. */
  onTools: () => void;
  /** The workspace can run its tools (running VM, online runner). */
  canOpenTools: boolean;
  /** Actions of the chat header's overflow ("…") menu; none hides it. */
  menuActions: NativeMenuAction[];
  onMenuAction: (id: string) => void;
}
