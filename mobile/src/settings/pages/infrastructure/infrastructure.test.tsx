import React from "react";
import { Alert, type AlertButton } from "react-native";
import { act, fireEvent, screen, waitFor } from "@testing-library/react-native";
import type {
  CapturedImage,
  ImageDefinition,
  Runner,
  RunnerImageBuild,
  RunnerStorage,
  Workspace,
} from "@opencuria/client";
import { ResourceStore } from "../../resources";
import type { SettingsChoice } from "../../SettingsContext";
import { mockClient, renderSettingsPage } from "../../testUtils";
import { CapturedImagePage } from "./CapturedImagePage";
import { CapturedImagesPage } from "./CapturedImagesPage";
import { ImageDefinitionEditorPage } from "./ImageDefinitionEditorPage";
import { ImageDefinitionPage } from "./ImageDefinitionPage";
import { NewRunnerPage, RunnerTokenPage } from "./NewRunnerPage";
import { RunnerLimitsPage } from "./RunnerLimitsPage";
import { RunnerOperationPage } from "./RunnerOperationPage";
import { RunnerPage } from "./RunnerPage";
import { setQemuBound } from "./runners";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));
jest.mock("expo-clipboard", () => ({
  setStringAsync: jest.fn(async () => true),
}));

const runner: Runner = {
  id: "r1",
  name: "Office",
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
  qemu_max_active_vcpus: null,
  qemu_max_active_memory_mb: null,
  qemu_max_active_disk_size_gb: 500,
  connected_at: new Date().toISOString(),
};

const image: CapturedImage = {
  id: "img",
  name: "node-dev",
  status: "active",
  runner_id: "r1",
  runner_online: true,
  latest_id: "v2",
  latest_version: 2,
  workspace_count: 1,
  total_size_bytes: 3 * 1024 ** 3,
  versions: [
    {
      id: "v2",
      name: "node-dev",
      status: "ready",
      artifact_kind: "captured",
      version: 2,
      is_latest: true,
      retention: "latest",
      message: "Installed Playwright",
      created_at: "2026-11-01T00:00:00Z",
      source_runner_id: "r1",
    },
    {
      id: "v1",
      name: "node-dev",
      status: "ready",
      artifact_kind: "captured",
      version: 1,
      is_latest: false,
      retention: "expires_when_unused",
      message: "",
      created_at: "2026-10-01T00:00:00Z",
      workspace_count: 1,
    },
  ],
};

function workspace(overrides: Partial<Workspace>): Workspace {
  return {
    id: "w1",
    name: "api",
    status: "running",
    runner_id: "r1",
    active_operation: null,
    runner_online: true,
    has_active_session: false,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

describe("captured images", () => {
  it("captures a capturable workspace in the existing sheet", async () => {
    const { navigation } = renderSettingsPage(<CapturedImagesPage />, {
      client: mockClient({
        images: { listCaptured: jest.fn(async () => [image]) },
      }),
      environment: {
        workspaces: [
          workspace({}),
          workspace({
            id: "w2",
            name: "busy",
            active_operation: "capturing_image",
          }),
        ],
      },
    });
    expect(
      await screen.findByRole("button", {
        name: "node-dev, v2 · 2 versions · 3.0 GiB · used by 1 workspace",
      }),
    ).toBeTruthy();
    fireEvent.press(screen.getByTestId("capture-image"));
    const choice = navigation.choose.mock.calls[0]![0] as SettingsChoice;
    expect(choice.groups[0]!.options.map((option) => option.label)).toEqual([
      "api",
    ]);
    act(() => choice.onSelect("w1"));
    expect(navigation.present).toHaveBeenCalledTimes(1);
  });

  it("deletes older versions and the image through the deletion page", async () => {
    const { navigation } = renderSettingsPage(
      <CapturedImagePage imageId="img" />,
      {
        client: mockClient({
          images: { listCaptured: jest.fn(async () => [image]) },
        }),
      },
    );
    expect(await screen.findByText("v2 · Latest")).toBeTruthy();
    // Usage is a count; which workspaces use a version stays private.
    expect(screen.getByText(/Used by 1 workspace/)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Actions for v2 · Latest" }),
    ).toBeNull();
    fireEvent.press(
      screen.getByRole("button", {
        name: "Actions for v1 · Removed when unused",
      }),
    );
    fireEvent.press(screen.getByRole("button", { name: "Delete v1" }));
    expect(navigation.push).toHaveBeenCalledWith(
      "deletion:image:v1:node-dev%20%C2%B7%20v1",
    );
    fireEvent.press(screen.getByRole("button", { name: "Delete image" }));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "deletion:captured_image:img:node-dev",
    );
    fireEvent.press(screen.getByRole("button", { name: "New workspace" }));
    expect(navigation.present).toHaveBeenCalled();
  });
});

describe("image definitions", () => {
  const definition: ImageDefinition = {
    id: "d1",
    name: "Python",
    description: "",
    is_active: true,
    status: "active",
    is_standard: false,
    base_distro: "ubuntu:22.04",
    packages: ["python3"],
    env_vars: { LANG: "C.UTF-8" },
    custom_init_script: "",
  };
  const build: RunnerImageBuild = {
    id: "b1",
    image_definition_id: "d1",
    runner_id: "r1",
    status: "active",
    current_generation_id: "g2",
    versions: [],
  };

  function definitionClient(overrides: Record<string, jest.Mock> = {}) {
    return mockClient({
      images: {
        listDefinitions: jest.fn(async () => [definition]),
        listRunnerBuilds: jest.fn(async () => [build]),
        updateRunnerBuild:
          overrides.updateRunnerBuild ?? jest.fn(async () => build),
        createRunnerBuild:
          overrides.createRunnerBuild ?? jest.fn(async () => build),
        duplicateDefinition:
          overrides.duplicateDefinition ??
          jest.fn(async () => ({ ...definition, id: "d2" })),
      },
      runners: {
        list: jest.fn(async () => [
          runner,
          { ...runner, id: "r2", name: "Lab", status: "offline" },
        ]),
      },
    });
  }

  it("offers lifecycle actions per runner to admins", async () => {
    const updateRunnerBuild = jest.fn(async () => build);
    const createRunnerBuild = jest.fn(async () => build);
    const { navigation } = renderSettingsPage(
      <ImageDefinitionPage definitionId="d1" />,
      {
        client: definitionClient({ updateRunnerBuild, createRunnerBuild }),
      },
    );
    fireEvent.press(await screen.findByTestId("r1-deactivate"));
    await waitFor(() =>
      expect(updateRunnerBuild).toHaveBeenCalledWith("d1", "r1", {
        action: "deactivate",
      }),
    );
    fireEvent.press(screen.getByTestId("r1-rebuild"));
    expect(navigation.push).toHaveBeenCalledWith(
      "image-definition-rebuild:d1:r1",
    );
    fireEvent.press(screen.getByTestId("r2-build"));
    await waitFor(() =>
      expect(createRunnerBuild).toHaveBeenCalledWith("d1", {
        runner_id: "r2",
        activate: true,
      }),
    );
    fireEvent.press(screen.getByTestId("r1-remove"));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "deletion:assignment:b1:Python%20on%20Office",
    );
  });

  it("hides definition mutations from members", async () => {
    renderSettingsPage(<ImageDefinitionPage definitionId="d1" />, {
      admin: false,
      client: definitionClient(),
    });
    expect(await screen.findByText("Status: Ready")).toBeTruthy();
    expect(screen.queryByTestId("r1-deactivate")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "More for Python" }),
    ).toBeNull();
  });

  it("validates environment lines and creates a definition", async () => {
    const createDefinition = jest.fn(async () => ({ ...definition, id: "d3" }));
    const { navigation } = renderSettingsPage(
      <ImageDefinitionEditorPage definitionId={null} />,
      {
        client: mockClient({
          images: {
            listDefinitions: jest.fn(async () => []),
            createDefinition,
          },
        }),
      },
    );
    fireEvent.changeText(screen.getByLabelText("Name"), "Node");
    fireEvent.changeText(
      screen.getByLabelText("Baked environment variables"),
      "=broken",
    );
    expect(
      screen.getByText("Each environment line needs a KEY before its “=”."),
    ).toBeTruthy();
    expect(screen.getByTestId("save-image-definition")).toBeDisabled();
    fireEvent.changeText(
      screen.getByLabelText("Baked environment variables"),
      "NODE_ENV=production",
    );
    fireEvent.changeText(
      screen.getByLabelText("Pre-installed packages"),
      "nodejs, , git",
    );
    fireEvent.press(screen.getByTestId("save-image-definition"));
    await waitFor(() =>
      expect(createDefinition).toHaveBeenCalledWith({
        name: "Node",
        description: "",
        base_distro: "ubuntu:22.04",
        packages: ["nodejs", "git"],
        env_vars: { NODE_ENV: "production" },
        custom_init_script: "",
      }),
    );
    expect(navigation.replace).toHaveBeenCalledWith("image-definition:d3");
  });
});

describe("runners", () => {
  const storage: RunnerStorage = {
    runner_id: "r1",
    runner_online: true,
    latest_snapshot_id: 3,
    latest_complete: true,
    runtimes: [],
    generations: [
      {
        id: "g2",
        name: "Python",
        owner_label: "",
        definition_id: "d1",
        definition_name: "Python",
        build_job_id: "b1",
        generation: 2,
        status: "ready",
        assignment_status: "active",
        size_bytes: 1024 ** 3,
        size_source: "measured",
        is_legacy: false,
        is_current: true,
        is_pending: false,
        observed_state: "present",
        dependencies: [],
      },
    ],
    activities: [
      {
        id: "del-1",
        kind: "deletion",
        title: "Deletion waiting",
        target_name: "node-dev v1",
        state: "attention",
        outcome: "",
        summary: "Waiting for 1 workspace",
        diagnostic: "",
        operation_ids: ["op-1"],
        created_at: new Date().toISOString(),
      },
    ],
  };

  it("rebuilds the current generation and cancels a pending deletion", async () => {
    const updateRunnerBuild = jest.fn(async () => ({}));
    const cancel = jest.fn(async () => ({}));
    const { navigation } = renderSettingsPage(<RunnerPage runnerId="r1" />, {
      client: mockClient({
        runners: {
          list: jest.fn(async () => [runner]),
          latestMetrics: jest.fn(async () => ({
            runner_id: "r1",
            timestamp: new Date().toISOString(),
            cpu_usage_percent: 95,
            ram_used_bytes: 1,
            ram_total_bytes: 4,
            disk_used_bytes: 1,
            disk_total_bytes: 2,
          })),
          storage: jest.fn(async () => storage),
        },
        images: { updateRunnerBuild },
        deletions: { cancel },
      }),
    });
    fireEvent.press(
      await screen.findByRole("button", {
        name: "Actions for Python · v2",
      }),
    );
    fireEvent.press(
      screen.getByRole("button", { name: "Build new generation" }),
    );
    await waitFor(() =>
      expect(updateRunnerBuild).toHaveBeenCalledWith("d1", "r1", {
        action: "rebuild",
      }),
    );
    fireEvent.press(
      screen.getByRole("button", {
        name: "Actions for node-dev v1 · Deletion waiting",
      }),
    );
    fireEvent.press(screen.getByRole("button", { name: "Cancel request" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("del-1"));
    fireEvent.press(
      screen.getByRole("button", {
        name: "Actions for node-dev v1 · Deletion waiting",
      }),
    );
    fireEvent.press(screen.getByRole("button", { name: "Check status" }));
    expect(navigation.push).toHaveBeenCalledWith("runner-operation:op-1");
  });

  it("couples QEMU bounds and saves all limits with unlimited caps", async () => {
    expect(setQemuBound({ min: 1, default: 2, max: 8 }, "min", 4)).toEqual({
      min: 4,
      default: 4,
      max: 8,
    });
    expect(setQemuBound({ min: 2, default: 4, max: 8 }, "max", 1)).toEqual({
      min: 1,
      default: 1,
      max: 1,
    });
    const update = jest.fn(async () => runner);
    renderSettingsPage(<RunnerLimitsPage runnerId="r1" />, {
      client: mockClient({
        runners: {
          list: jest.fn(async () => [runner]),
          latestMetrics: jest.fn(async () => null),
          update,
        },
      }),
    });
    const [vcpuMinimum] = await screen.findAllByRole("button", {
      name: "Increase Minimum",
    });
    fireEvent.press(vcpuMinimum!);
    fireEvent.press(screen.getAllByRole("switch", { name: "Unlimited" })[2]!);
    fireEvent.press(screen.getByTestId("save-runner-limits"));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        "r1",
        expect.objectContaining({
          qemu_min_vcpus: 2,
          qemu_default_vcpus: 2,
          qemu_max_active_vcpus: null,
          qemu_max_active_disk_size_gb: null,
        }),
      ),
    );
  });

  it("registers a runner and shows its token once", async () => {
    const resources = new ResourceStore();
    const create = jest.fn(async () => ({
      id: "r9",
      name: "lab",
      api_token: "tok_123",
    }));
    const client = mockClient({
      runners: { list: jest.fn(async () => []), create },
    });
    const { navigation, unmount } = renderSettingsPage(<NewRunnerPage />, {
      client,
      environment: { resources },
    });
    fireEvent.changeText(screen.getByLabelText("Name (optional)"), "lab");
    fireEvent.press(screen.getByTestId("create-runner"));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: "lab" }));
    expect(navigation.replace).toHaveBeenCalledWith("runner-token:r9");
    unmount();
    renderSettingsPage(<RunnerTokenPage runnerId="r9" />, {
      client,
      environment: { resources },
    });
    expect(screen.getByText("API token: tok_123")).toBeTruthy();
  });

  it("confirms closing an interrupted operation", async () => {
    const disposeOperation = jest.fn(async () => ({}));
    renderSettingsPage(<RunnerOperationPage operationId="op-1" />, {
      client: mockClient({
        runners: {
          inspectOperation: jest.fn(async () => ({
            operation_id: "op-1",
            status: "interrupted",
            diagnostic: "Runner restarted",
            phase: "applying",
            permitted_actions: ["acknowledge_interrupted"],
            evidence: { attempts: 2 },
          })),
          disposeOperation,
        },
      }),
    });
    const alert = jest.spyOn(Alert, "alert");
    fireEvent.press(
      await screen.findByTestId("dispose-acknowledge_interrupted"),
    );
    const buttons = alert.mock.calls[0]![2] as AlertButton[];
    await act(async () =>
      buttons.find((button) => button.style === "destructive")!.onPress!(),
    );
    expect(disposeOperation).toHaveBeenCalledWith(
      "op-1",
      "acknowledge_interrupted",
    );
    alert.mockRestore();
  });
});
