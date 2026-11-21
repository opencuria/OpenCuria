import type React from "react";
import type { NativeMenuAction } from "./NativeMenu";

/** A subagent chat: the header becomes a pushed page leading back to its parent. */
export interface DrawerDetail {
  /** The subagent's task. */
  title: string;
  /** Agent type and state, e.g. "Explorer · Working…". */
  subtitle: string;
  /** Parent chat the back button returns to. */
  backTitle: string;
  onBack: () => void;
}

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
  /**
   * Set inside a subagent chat: Back replaces the sidebar button, the title
   * names the subagent, and only the overflow menu stays on the trailing side.
   */
  detail?: DrawerDetail;
}
