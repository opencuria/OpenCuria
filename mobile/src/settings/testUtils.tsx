import React from "react";
import { render } from "@testing-library/react-native";
import type {
  AuthUser,
  OpenCuriaClient,
  Organization,
} from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { ResourceStore } from "./resources";
import {
  SettingsEnvironmentContext,
  SettingsNavigationContext,
  type SettingsEnvironment,
  type SettingsNavigation,
} from "./SettingsContext";

/** Test helpers for settings pages; not part of the app bundle. */
export const testOrganization: Organization = {
  id: "org-1",
  name: "Acme",
  slug: "acme",
  role: "admin",
  created_at: "2026-01-01T00:00:00Z",
};

export const testUser: AuthUser = {
  id: 7,
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  organizations: [testOrganization],
};

type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends (...args: never[]) => unknown
    ? T[K]
    : T[K] extends object
      ? DeepPartial<T[K]>
      : T[K];
};

/** A client whose namespaces only contain the given (mocked) methods. */
export function mockClient(
  overrides: DeepPartial<OpenCuriaClient>,
): OpenCuriaClient {
  return overrides as unknown as OpenCuriaClient;
}

export interface RenderSettingsOptions {
  client: OpenCuriaClient;
  admin?: boolean;
  environment?: Partial<SettingsEnvironment>;
}

/** Renders one settings page with a recorded navigation and environment. */
export function renderSettingsPage(
  page: React.ReactElement,
  { client, admin = true, environment = {} }: RenderSettingsOptions,
) {
  const organization = {
    ...testOrganization,
    role: admin ? "admin" : "member",
  };
  const navigation: jest.Mocked<SettingsNavigation> = {
    push: jest.fn(),
    pop: jest.fn(),
    replace: jest.fn(),
    popToRoot: jest.fn(),
    close: jest.fn(),
    choose: jest.fn(),
    present: jest.fn(),
  };
  const value: SettingsEnvironment = {
    client,
    user: { ...testUser, organizations: [organization] },
    organizations: [organization],
    organizationId: organization.id,
    organization,
    isAdmin: admin,
    serverUrl: "https://curia.example",
    resources: new ResourceStore(),
    workspaces: [],
    refreshWorkspaces: jest.fn(async () => undefined),
    onCatalogChanged: jest.fn(),
    onOpenWorkspace: jest.fn(),
    selectOrganization: jest.fn(),
    createOrganization: jest.fn(async () => undefined),
    signOut: jest.fn(),
    ...environment,
  };
  const result = render(
    <ThemeProvider>
      <SettingsEnvironmentContext.Provider value={value}>
        <SettingsNavigationContext.Provider value={navigation}>
          {page}
        </SettingsNavigationContext.Provider>
      </SettingsEnvironmentContext.Provider>
    </ThemeProvider>,
  );
  return { ...result, navigation, environment: value };
}
