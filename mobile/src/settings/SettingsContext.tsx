import React, { createContext, useContext } from "react";
import type {
  AuthUser,
  OpenCuriaClient,
  Organization,
  Workspace,
} from "@opencuria/client";
import type { ResourceStore } from "./resources";

export interface SettingsChoiceOption {
  value: string;
  label: string;
  subtitle?: string;
  disabled?: boolean;
}

/** A pushed page that picks one value from a (searchable) list. */
export interface SettingsChoice {
  title: string;
  value: string;
  groups: Array<{ title?: string; options: SettingsChoiceOption[] }>;
  /** Called with the picked value; the page then goes back. */
  onSelect: (value: string) => void;
  /** Adds a search field, e.g. for model catalogs. */
  searchPlaceholder?: string;
  footer?: string;
  /** Shown when there is nothing to choose from. */
  emptyText?: string;
}

/** Moves through the settings pages; routes are strings such as "skill:<id>". */
export interface SettingsNavigation {
  push: (route: string) => void;
  /** Goes back one page. */
  pop: () => void;
  /** Replaces the top page, e.g. a form with the item it created. */
  replace: (route: string) => void;
  popToRoot: () => void;
  /** Closes Settings. */
  close: () => void;
  /** Pushes a list to pick one value from, with a checkmark on the current one. */
  choose: (choice: SettingsChoice) => void;
  /**
   * Shows a sheet of its own above Settings, e.g. the workspace capture
   * sheet; `null` dismisses it. It renders outside the native page stack.
   */
  present: (sheet: React.ReactNode | null) => void;
}

/** What every settings page may use: the session, its server and shared data. */
export interface SettingsEnvironment {
  client: OpenCuriaClient;
  user: AuthUser;
  organizations: Organization[];
  organizationId: string | null;
  organization: Organization | null;
  /** Admins manage organization-wide settings, runners and services. */
  isAdmin: boolean;
  /** The server's web address, e.g. for handing a flow over to the webapp. */
  serverUrl: string;
  resources: ResourceStore;
  workspaces: Workspace[];
  refreshWorkspaces: () => Promise<void>;
  /** Provider accounts, models or agent defaults changed; reload chat catalogs. */
  onCatalogChanged: () => void;
  /** Open a workspace's chat, closing Settings. */
  onOpenWorkspace: (workspace: Workspace) => void;
  selectOrganization: (organizationId: string) => void;
  createOrganization: (name: string) => Promise<void>;
  signOut: () => void;
}

const noop = () => undefined;

export const SettingsNavigationContext = createContext<SettingsNavigation>({
  push: noop,
  pop: noop,
  replace: noop,
  popToRoot: noop,
  close: noop,
  choose: noop,
  present: noop,
});

export const SettingsEnvironmentContext =
  createContext<SettingsEnvironment | null>(null);

export function useSettingsNavigation(): SettingsNavigation {
  return useContext(SettingsNavigationContext);
}

export function useSettings(): SettingsEnvironment {
  const value = useContext(SettingsEnvironmentContext);
  if (!value) throw new Error("useSettings must be used inside Settings.");
  return value;
}
