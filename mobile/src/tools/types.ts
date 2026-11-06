import type { OpenCuriaClient } from "@opencuria/client";
import type { MediaCache } from "../platform";

/** The workspace tools sheet's sections, in header order. */
export type WorkspaceToolId = "git" | "desktop" | "terminal" | "files";

/**
 * Props every tools-sheet panel receives. Panels stay mounted while the sheet
 * is open (switching sections keeps terminal sessions and scroll positions);
 * `active` tells a panel whether it is the visible section, so it can pause
 * polling and keyboard focus while hidden.
 */
export interface WorkspaceToolProps {
  client: OpenCuriaClient;
  workspaceId: string;
  workspaceName: string;
  /** The panel is the visible section of the open sheet. */
  active: boolean;
  /** Private media cache of this workspace for image/file previews. */
  media: MediaCache | null;
  /** Bottom safe-area inset the panel's scrollables should pad by. */
  bottomInset: number;
  /**
   * The panel takes the whole screen (the desktop on a phone turned
   * sideways): the sheet hides its header and the status bar meanwhile.
   */
  onFullscreenChange?: (fullscreen: boolean) => void;
  /**
   * Appends text (terminal output, an `@file:` mention) to the chat composer
   * draft and closes the sheet; undefined while the chat cannot take input.
   */
  onInsertIntoChat?: (text: string) => void;
}
