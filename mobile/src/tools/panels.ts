import type React from "react";
import { GitPanel } from "./git";
import { FilesPanel } from "./files";
import { TerminalPanel } from "./terminal";
import type { WorkspaceToolId, WorkspaceToolProps } from "./types";

/** One panel per tools-sheet section. */
export const TOOL_PANELS: Record<
  WorkspaceToolId,
  React.ComponentType<WorkspaceToolProps>
> = {
  git: GitPanel,
  terminal: TerminalPanel,
  files: FilesPanel,
};
