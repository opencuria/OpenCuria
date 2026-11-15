import type React from "react";

export interface SettingsNavigatorProps {
  /** Routes pushed above the root page, oldest first. */
  path: string[];
  onPathChange: (path: string[]) => void;
  root: React.ReactNode;
  renderRoute: (route: string) => React.ReactNode;
}
