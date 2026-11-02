import React from "react";
import { Alert, type AlertButton } from "react-native";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react-native";
import type { OpenCuriaClient, Workspace } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { WorkspacesSheet } from "./WorkspacesSheet";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

const runner = {
  id: "runner-1",
  name: "Office runner",
  status: "online",
  qemu_min_vcpus: 1,
  qemu_max_vcpus: 8,
  qemu_default_vcpus: 2,
  qemu_min_memory_mb: 1024,
  qemu_max_memory_mb: 16384,
  qemu_default_memory_mb: 4096,
  qemu_min_disk_size_gb: 20,
  qemu_max_disk_size_gb: 200,
  qemu_default_disk_size_gb: 50,
};

const baseImage = {
  id: "version-3",
  line_kind: "captured" as const,
  line_id: "image-1",
  name: "node-dev",
  version: 3,
  message: "",
  status: "ready",
  latest_id: "version-4",
  latest_version: 4,
  update_available: true,
};

function workspace(overrides: Partial<Workspace>): Workspace {
  return {
    id: "ws",
    name: "ws",
    status: "running",
    runner_id: runner.id,
    active_operation: null,
    runner_online: true,
    has_active_session: false,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    credential_ids: [],
    plugin_ids: [],
    qemu_vcpus: 2,
    qemu_memory_mb: 4096,
    qemu_disk_size_gb: 50,
    desktop_width: 1920,
    desktop_height: 1080,
    ...overrides,
  };
}

const api = workspace({
  id: "ws-api",
  name: "api",
  base_image: baseImage,
  repos: ["https://github.com/acme/api"],
});
const docs = workspace({ id: "ws-docs", name: "docs", status: "stopped" });

const plugin = {
  id: "plugin-github",
  name: "GitHub",
  slug: "github",
  description: "Issues and pull requests",
  enabled: true,
  published: true,
  organization_id: null,
  is_global: true,
  org_enabled: true,
  credential_requirements: [
    {
      id: "requirement",
      key: "GITHUB_TOKEN",
      description: "",
      required: true,
      service_id: "service-github",
      service_name: "GitHub token",
      service_slug: "github",
      credential_type: "env",
    },
  ],
};
const token = {
  id: "credential-github",
  name: "Personal GitHub",
  scope: "personal" as const,
  service_id: "service-github",
  service_name: "GitHub token",
  service_slug: "github",
  credential_type: "env",
  created_by_id: 1,
  oauth_connected: false,
};

function mockClient() {
  return {
    workspaces: {
      create: jest.fn().mockResolvedValue({ workspace_id: "ws-new" }),
      update: jest.fn().mockResolvedValue({
        name: "api-renamed",
        credential_sync_status: "not_required",
        active_operation: null,
      }),
      stop: jest.fn().mockResolvedValue({}),
      resume: jest.fn().mockResolvedValue({}),
      delete: jest.fn().mockResolvedValue({}),
      recreate: jest.fn().mockResolvedValue({}),
      captureImage: jest.fn().mockResolvedValue({}),
    },
    runners: { list: jest.fn().mockResolvedValue([runner]) },
    plugins: { list: jest.fn().mockResolvedValue([plugin]) },
    credentials: { list: jest.fn().mockResolvedValue([token]) },
    images: {
      listCaptured: jest.fn().mockResolvedValue([
        {
          id: "image-1",
          name: "node-dev",
          status: "active",
          runner_id: runner.id,
          runner_online: true,
          latest_id: "version-4",
          latest_version: 4,
          workspace_count: 1,
          versions: [
            {
              id: "version-4",
              status: "ready",
              version: 4,
              source_runner_id: runner.id,
            },
          ],
        },
      ]),
      listDefinitions: jest.fn().mockResolvedValue([
        {
          id: "def-ubuntu",
          name: "Ubuntu",
          is_active: true,
          status: "active",
        },
      ]),
      listRunnerBuilds: jest.fn().mockResolvedValue([
        {
          id: "build",
          image_definition_id: "def-ubuntu",
          runner_id: runner.id,
          image_artifact_id: "artifact-ubuntu",
          status: "active",
        },
      ]),
      createWorkspace: jest
        .fn()
        .mockResolvedValue({ workspace_id: "ws-clone" }),
    },
  };
}

function renderSheet(client = mockClient(), workspaces = [api, docs]) {
  const onOpenWorkspace = jest.fn();
  const onRefresh = jest.fn().mockResolvedValue(undefined);
  const tree = render(
    <ThemeProvider>
      <WorkspacesSheet
        visible
        onClose={jest.fn()}
        client={client as unknown as OpenCuriaClient}
        organizationId="org"
        workspaces={workspaces}
        activeWorkspaceId="ws-api"
        onOpenWorkspace={onOpenWorkspace}
        onRefresh={onRefresh}
      />
    </ThemeProvider>,
  );
  return { client, tree, onOpenWorkspace, onRefresh };
}

/** Press the button of the alert currently shown. */
function alertButton(alert: jest.SpyInstance, text: string): AlertButton {
  const buttons = alert.mock.calls.at(-1)?.[2] as AlertButton[];
  return buttons.find((button) => button.text === text)!;
}

afterEach(() => jest.restoreAllMocks());

it("lists workspaces by status, searches and opens a workspace's chats", async () => {
  const { onOpenWorkspace, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  expect(screen.getByText("Stopped · 1")).toBeTruthy();
  const row = screen.getByRole("button", {
    name: /^api, Running · node-dev · v3/,
  });
  expect(row.props.accessibilityState.selected).toBe(true);
  expect(within(row).getByText("Update")).toBeTruthy();

  fireEvent.changeText(screen.getByLabelText("Search workspaces"), "doc");
  expect(screen.queryByText("api")).toBeNull();
  fireEvent.press(screen.getByRole("button", { name: /^docs, Stopped/ }));
  expect(onOpenWorkspace).toHaveBeenCalledWith(docs);

  fireEvent.changeText(screen.getByLabelText("Search workspaces"), "zzz");
  expect(screen.getByText("No results")).toBeTruthy();
  tree.unmount();
});

it("starts, stops and removes workspaces from their row menu", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
  const { client, onRefresh, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());

  fireEvent.press(screen.getByRole("button", { name: "Actions for docs" }));
  fireEvent.press(screen.getByRole("button", { name: "Start" }));
  await waitFor(() =>
    expect(client.workspaces.resume).toHaveBeenCalledWith("ws-docs"),
  );
  await waitFor(() => expect(onRefresh).toHaveBeenCalled());

  fireEvent.press(screen.getByRole("button", { name: "Actions for api" }));
  fireEvent.press(screen.getByRole("button", { name: "Stop" }));
  await waitFor(() =>
    expect(client.workspaces.stop).toHaveBeenCalledWith("ws-api"),
  );

  fireEvent.press(screen.getByRole("button", { name: "Actions for api" }));
  fireEvent.press(screen.getByRole("button", { name: "Remove workspace" }));
  expect(alert.mock.calls.at(-1)?.[0]).toBe("Remove workspace?");
  expect(client.workspaces.delete).not.toHaveBeenCalled();
  await act(async () => alertButton(alert, "Remove").onPress?.());
  expect(client.workspaces.delete).toHaveBeenCalledWith("ws-api");
  tree.unmount();
});

it("resets or updates a workspace only after confirming the data loss", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "Actions for api" }));
  fireEvent.press(screen.getByRole("button", { name: "Update to v4" }));
  expect(alert.mock.calls.at(-1)?.[1]).toContain("permanently deleted");
  expect(alert.mock.calls.at(-1)?.[1]).toContain("https://github.com/acme/api");
  await act(async () => alertButton(alert, "Update to v4").onPress?.());
  expect(client.workspaces.recreate).toHaveBeenCalledWith(
    "ws-api",
    "version-4",
  );
  tree.unmount();
});

it("edits a workspace and saves only what changed", async () => {
  const { client, onRefresh, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "Details for api" }));
  const sheet = await screen.findByTestId("workspace-detail-sheet");
  await waitFor(() =>
    expect(within(sheet).getByText("Runner: Office runner")).toBeTruthy(),
  );
  expect(
    screen.getByTestId("workspace-save").props.accessibilityState.disabled,
  ).toBe(true);

  fireEvent.changeText(
    within(sheet).getByLabelText("Workspace name"),
    "api-renamed",
  );
  fireEvent.press(
    within(sheet).getByRole("button", { name: "Increase Memory" }),
  );
  fireEvent.press(screen.getByTestId("workspace-save"));
  await waitFor(() =>
    expect(client.workspaces.update).toHaveBeenCalledWith("ws-api", {
      name: "api-renamed",
      credential_ids: [],
      plugin_ids: [],
      qemu_memory_mb: 5120,
    }),
  );
  await waitFor(() => expect(onRefresh).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.queryByTestId("workspace-detail-sheet")).toBeNull(),
  );
  tree.unmount();
});

it("blocks creation until required plugin credentials are selected", async () => {
  const { client, onRefresh, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "New workspace" }));
  const sheet = await screen.findByTestId("create-workspace-sheet");
  await waitFor(() =>
    expect(within(sheet).getByRole("switch", { name: "GitHub" })).toBeTruthy(),
  );

  fireEvent.changeText(within(sheet).getByLabelText("Name"), "billing");
  fireEvent.changeText(
    within(sheet).getByLabelText("Repository URL"),
    "https://github.com/acme/billing",
  );
  fireEvent.press(
    within(sheet).getByRole("button", { name: "Add repository" }),
  );
  fireEvent.press(within(sheet).getByRole("switch", { name: "GitHub" }));
  expect(
    within(sheet).getByText("Select a GitHub token credential below"),
  ).toBeTruthy();
  expect(
    screen.getByTestId("workspace-create").props.accessibilityState.disabled,
  ).toBe(true);

  fireEvent.press(
    within(sheet).getByRole("switch", { name: "Personal GitHub" }),
  );
  fireEvent.press(within(sheet).getByRole("button", { name: "Increase CPU" }));
  fireEvent.press(screen.getByTestId("workspace-create"));
  await waitFor(() =>
    expect(client.workspaces.create).toHaveBeenCalledWith({
      name: "billing",
      image_id: "artifact-ubuntu",
      repos: ["https://github.com/acme/billing"],
      credential_ids: ["credential-github"],
      plugin_ids: ["plugin-github"],
      runner_id: "runner-1",
      qemu_vcpus: 3,
      qemu_memory_mb: 4096,
      qemu_disk_size_gb: 50,
    }),
  );
  await waitFor(() => expect(onRefresh).toHaveBeenCalled());
  expect(screen.queryByTestId("create-workspace-sheet")).toBeNull();
  tree.unmount();
});

it("clones captured images without runner or resource choices", async () => {
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "New workspace" }));
  const sheet = await screen.findByTestId("create-workspace-sheet");
  await waitFor(() =>
    expect(
      within(sheet).getByRole("button", { name: "node-dev · v4" }),
    ).toBeTruthy(),
  );
  fireEvent.press(within(sheet).getByRole("button", { name: "node-dev · v4" }));
  expect(within(sheet).queryByText("Resources")).toBeNull();
  fireEvent.changeText(within(sheet).getByLabelText("Name"), "clone");
  fireEvent.press(screen.getByTestId("workspace-create"));
  await waitFor(() =>
    expect(client.images.createWorkspace).toHaveBeenCalledWith("image-1", {
      name: "clone",
      credential_ids: [],
      plugin_ids: [],
    }),
  );
  tree.unmount();
});

it("captures the next version of the workspace's own image", async () => {
  const { client, tree } = renderSheet();
  await waitFor(() => expect(screen.getByText("Running · 1")).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "Actions for api" }));
  fireEvent.press(screen.getByRole("button", { name: "Capture image" }));
  const sheet = await screen.findByTestId("capture-image-sheet");
  await waitFor(() =>
    expect(
      within(sheet).getByRole("button", { name: "node-dev · v5" }).props
        .accessibilityState.selected,
    ).toBe(true),
  );
  fireEvent.changeText(
    within(sheet).getByLabelText("What changed?"),
    "Added Playwright",
  );
  fireEvent.press(screen.getByTestId("capture-submit"));
  await waitFor(() =>
    expect(client.workspaces.captureImage).toHaveBeenCalledWith("ws-api", {
      captured_image_id: "image-1",
      message: "Added Playwright",
    }),
  );
  tree.unmount();
});
