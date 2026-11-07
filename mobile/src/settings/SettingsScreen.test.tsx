import React from "react";
import { Alert, type AlertButton } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import type { Organization } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import {
  DeletionPage,
  deletionRoute,
  parseDeletionRoute,
} from "./pages/DeletionPage";
import { WorkspacePolicyPage } from "./pages/WorkspacePolicyPage";
import { SettingsScreen } from "./SettingsScreen";
import {
  mockClient,
  renderSettingsPage,
  testOrganization,
  testUser,
} from "./testUtils";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

function renderScreen(role = "admin", offline = false) {
  const organizations = [
    { ...testOrganization, role },
    { ...testOrganization, id: "org-2", name: "Beta", role: "member" },
  ];
  const props = {
    visible: true,
    onClose: jest.fn(),
    client: mockClient({}),
    user: { ...testUser, organizations },
    organizations,
    organizationId: "org-1",
    serverUrl: "https://curia.example",
    offline,
    workspaces: [],
    refreshWorkspaces: jest.fn(async () => undefined),
    onCatalogChanged: jest.fn(),
    onOpenWorkspace: jest.fn(),
    selectOrganization: jest.fn(),
    createOrganization: jest.fn(async () => undefined),
    signOut: jest.fn(),
  };
  render(
    <ThemeProvider>
      <SettingsScreen {...props} />
    </ThemeProvider>,
  );
  return props;
}

it("shows the profile, organization and every settings area like ChatGPT's settings", () => {
  renderScreen();
  expect(screen.getByText("AL")).toBeTruthy();
  expect(screen.getByText("Ada Lovelace")).toBeTruthy();
  expect(screen.getByText("Acme")).toBeTruthy();
  for (const title of [
    "Provider & Models",
    "Agents",
    "Skills",
    "Plugins",
    "Credentials",
    "Workspace policy",
    "Captured images",
    "Image definitions",
    "Runners",
    "API keys",
    "Credential services",
    "Notifications",
  ])
    expect(
      screen.getByRole("link", { name: new RegExp(`^${title}`) }),
    ).toBeTruthy();
  expect(screen.getByText("Email: ada@example.com")).toBeTruthy();
  expect(screen.getByText("Server: curia.example")).toBeTruthy();
});

it("hides admin-only areas from members", () => {
  renderScreen("member");
  expect(screen.queryByRole("link", { name: /^Runners/ })).toBeNull();
  expect(
    screen.queryByRole("link", { name: /^Credential services/ }),
  ).toBeNull();
  expect(screen.getByText("Role: Member")).toBeTruthy();
});

it("switches organizations from the card and confirms signing out", () => {
  const props = renderScreen();
  fireEvent.press(screen.getByRole("button", { name: "Switch" }));
  fireEvent.press(screen.getByRole("button", { name: "Beta" }));
  expect(props.selectOrganization).toHaveBeenCalledWith("org-2");

  const alert = jest.spyOn(Alert, "alert");
  fireEvent.press(screen.getByTestId("sign-out"));
  const buttons = alert.mock.calls[0]![2] as AlertButton[];
  act(() => buttons.find((button) => button.text === "Sign out")!.onPress!());
  expect(props.signOut).toHaveBeenCalled();
  alert.mockRestore();
});

it("offers only appearance and the account while offline", () => {
  const props = renderScreen("admin", true);
  expect(screen.getByText("Ada Lovelace")).toBeTruthy();
  expect(screen.getByText(/You’re offline/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Switch" })).toBeNull();
  for (const title of ["Provider & Models", "Runners", "Notifications"])
    expect(
      screen.queryByRole("link", { name: new RegExp(`^${title}`) }),
    ).toBeNull();
  expect(screen.getByText("Email: ada@example.com")).toBeTruthy();

  const alert = jest.spyOn(Alert, "alert");
  fireEvent.press(screen.getByTestId("sign-out"));
  const buttons = alert.mock.calls[0]![2] as AlertButton[];
  act(() => buttons.find((button) => button.text === "Sign out")!.onPress!());
  expect(props.signOut).toHaveBeenCalled();
  alert.mockRestore();
});

it("pushes pages from link rows and goes back", () => {
  renderScreen();
  fireEvent.press(screen.getByRole("link", { name: /^Notifications/ }));
  expect(screen.getByTestId("settings-notifications")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByTestId("settings-root")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Close settings" }));
});

describe("workspace policy", () => {
  const organization: Organization = {
    ...testOrganization,
    workspace_auto_stop_timeout_minutes: null,
    image_versions_to_keep: 2,
  };

  it("saves changes automatically for admins", async () => {
    const updateWorkspacePolicy = jest.fn(
      async (_id: string, input: object) => ({
        ...organization,
        ...input,
      }),
    );
    renderSettingsPage(<WorkspacePolicyPage />, {
      client: mockClient({
        organizations: {
          get: jest.fn(async () => organization),
          updateWorkspacePolicy,
        },
      }),
    });
    const toggle = await screen.findByRole("switch", {
      name: "Stop inactive workspaces",
    });
    expect(screen.getByText("All changes saved")).toBeTruthy();
    fireEvent.press(toggle);
    expect(screen.getByText("Inactive workspaces stop after 4h.")).toBeTruthy();
    await waitFor(() =>
      expect(updateWorkspacePolicy).toHaveBeenCalledWith("org-1", {
        workspace_auto_stop_timeout_minutes: 240,
        image_versions_to_keep: 2,
      }),
    );
    await screen.findByText("All changes saved");

    fireEvent.press(screen.getByRole("button", { name: "Custom" }));
    fireEvent.changeText(screen.getByLabelText("Minutes"), "0");
    expect(
      screen.getByText("Enter a whole number of minutes between 1 and 10080."),
    ).toBeTruthy();
    expect(screen.getByText("Check your input")).toBeTruthy();
  });

  it("is read-only for members", async () => {
    renderSettingsPage(<WorkspacePolicyPage />, {
      admin: false,
      client: mockClient({
        organizations: { get: jest.fn(async () => organization) },
      }),
    });
    const toggle = await screen.findByRole("switch", {
      name: "Stop inactive workspaces",
    });
    expect(toggle).toBeDisabled();
    expect(
      screen.getByText(
        "Only organization admins can change the workspace policy.",
      ),
    ).toBeTruthy();
  });

  it("offers a retry when the policy cannot load", async () => {
    const get = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce(organization);
    renderSettingsPage(<WorkspacePolicyPage />, {
      client: mockClient({ organizations: { get } }),
    });
    expect(await screen.findByText("Server unavailable")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("switch", { name: "Stop inactive workspaces" }),
    ).toBeTruthy();
  });
});

describe("deletion", () => {
  const target = { target_type: "captured_image" as const, target_id: "img-1" };
  const graph = {
    fingerprint: "fp",
    blockers: [],
    counts: { images: 2, workspaces: 1 },
    images: [
      {
        id: "v1",
        name: "node-dev v1",
        owner_id: "7",
        owner_label: "Ada",
        runner_id: "r",
      },
    ],
    workspaces: [
      {
        id: "w1",
        name: "api",
        owner_id: "8",
        owner_label: "Bob",
        runner_id: "r",
      },
    ],
  };
  const pending = {
    ...target,
    id: "del-1",
    mode: "deferred" as const,
    phase: "waiting_dependency",
    diagnostic: "Waiting for 1 workspace.",
    can_cancel: true,
    approval: graph,
    fingerprint: "",
  };

  it("round-trips names with colons through the route", () => {
    expect(parseDeletionRoute(deletionRoute(target, "a:b").slice(9))).toEqual({
      target,
      name: "a:b",
    });
  });

  it("requests a deferred deletion and cancels it", async () => {
    const request = jest.fn(async () => pending);
    const cancel = jest.fn(async () => ({ ...pending, phase: "cancelled" }));
    renderSettingsPage(<DeletionPage target={target} name="node-dev" />, {
      admin: false,
      client: mockClient({
        deletions: { list: jest.fn(async () => []), request, cancel },
      }),
    });
    fireEvent.press(await screen.findByTestId("request-deletion"));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(target, "deferred"),
    );
    expect(
      await screen.findByText("Deletion: Waiting dependency"),
    ).toBeTruthy();
    expect(screen.queryByText("Force deletion")).toBeNull();
    fireEvent.press(
      screen.getByRole("button", { name: "Cancel pending request" }),
    );
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("del-1"));
  });

  it("lets admins force exactly the previewed deletion after approval", async () => {
    const request = jest.fn(async () => ({
      ...pending,
      mode: "force" as const,
    }));
    renderSettingsPage(<DeletionPage target={target} name="node-dev" />, {
      client: mockClient({
        deletions: {
          list: jest.fn(async () => []),
          preview: jest.fn(async () => graph),
          request,
        },
      }),
    });
    expect(
      await screen.findByText("Removes: 2 images · 1 workspaces"),
    ).toBeTruthy();
    const force = screen.getByRole("button", { name: "Delete permanently" });
    expect(force).toBeDisabled();
    fireEvent.press(
      screen.getByRole("switch", {
        name: "Approve permanent deletion of exactly this preview",
      }),
    );
    const alert = jest.spyOn(Alert, "alert");
    fireEvent.press(screen.getByRole("button", { name: "Delete permanently" }));
    const buttons = alert.mock.calls[0]![2] as AlertButton[];
    await act(async () =>
      buttons.find((b) => b.style === "destructive")!.onPress!(),
    );
    expect(request).toHaveBeenCalledWith(target, "force", "fp");
    alert.mockRestore();
  });
});
