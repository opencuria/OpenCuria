import { describe, expect, it } from "vitest";
import {
  getWorkspaceActionAvailability,
  workspaceOperationLabel,
  workspaceStatusLabel,
  workspaceStatusTone,
} from "../src/workspaceLifecycle";
import {
  evaluateWorkspacePluginSelection,
  toggleWorkspaceCredentialSelection,
} from "../src/workspaceConfiguration";
import {
  NEW_IMAGE,
  captureTargetCandidates,
  defaultCaptureTarget,
  nextImageVersion,
  sharedWorkspaceLabel,
  workspaceRecreateOptions,
} from "../src/imageVersions";
import {
  DEFAULT_QEMU_LIMITS,
  compatibleRunners,
  defaultWorkspaceImageOption,
  definitionArtifactForRunner,
  fitQemuResources,
  isDesktopSizeValid,
  isWorkspaceSettingsChanged,
  qemuResourceLimits,
  workspaceImageOptions,
  workspaceSettingsForm,
  workspaceUpdatePayload,
} from "../src/workspaceCreation";
import { formatStorageBytes } from "../src/workspaceDisk";

const workspace = {
  status: "running",
  runner_online: true,
  active_operation: null,
  intervention_required: false,
  has_active_session: false,
};

const runner = (id: string, status = "online") => ({
  id,
  name: id,
  status,
  qemu_min_vcpus: 1,
  qemu_max_vcpus: 16,
  qemu_default_vcpus: 4,
  qemu_min_memory_mb: 2048,
  qemu_max_memory_mb: 32768,
  qemu_default_memory_mb: 8192,
  qemu_min_disk_size_gb: 20,
  qemu_max_disk_size_gb: 500,
  qemu_default_disk_size_gb: 80,
});

describe("workspace lifecycle", () => {
  it("labels persisted transitions before resting states", () => {
    expect(workspaceOperationLabel(workspace)).toBeNull();
    expect(
      workspaceOperationLabel({ ...workspace, active_operation: "stopping" }),
    ).toBe("Stopping…");
    expect(
      workspaceOperationLabel({ ...workspace, status: "pending_deletion" }),
    ).toBe("Removal queued");
    expect(
      workspaceOperationLabel({ ...workspace, intervention_required: true }),
    ).toBe("Checking state");
  });

  it("reports offline runners, busy chats and readable statuses", () => {
    expect(workspaceStatusLabel({ ...workspace, runner_online: false })).toBe(
      "Runner offline",
    );
    expect(
      workspaceStatusLabel({ ...workspace, has_active_session: true }),
    ).toBe("Busy");
    expect(
      workspaceStatusLabel({ ...workspace, status: "delete_failed" }),
    ).toBe("Delete failed");
    expect(workspaceStatusTone({ ...workspace, status: "failed" })).toBe(
      "error",
    );
    expect(workspaceStatusTone({ ...workspace, status: "stopped" })).toBe(
      "stopped",
    );
    expect(
      workspaceStatusTone({ ...workspace, active_operation: "starting" }),
    ).toBe("busy");
  });

  it("only offers actions the workspace can take", () => {
    expect(getWorkspaceActionAvailability(workspace)).toMatchObject({
      start: false,
      stop: true,
      edit: true,
      reset: false,
      remove: true,
    });
    expect(
      getWorkspaceActionAvailability({
        ...workspace,
        status: "stopped",
        runner_online: false,
      }),
    ).toMatchObject({ start: false, edit: false, remove: true });
    expect(
      getWorkspaceActionAvailability({
        ...workspace,
        active_operation: "starting",
      }),
    ).toMatchObject({ stop: false, edit: false, remove: false });
  });
});

describe("workspace plugin and credential selection", () => {
  const plugin = {
    id: "plugin-1",
    enabled: true,
    published: true,
    org_enabled: true,
    organization_id: null,
    credential_requirements: [
      { id: "req", key: "GITHUB_TOKEN", required: true, service_id: "github" },
    ],
  };
  const token = {
    id: "token",
    service_id: "github",
    credential_type: "env",
    oauth_connected: false,
  };

  it("blocks saving until required credentials are selected", () => {
    const missing = evaluateWorkspacePluginSelection({
      plugins: [plugin],
      credentials: [token],
      organizationId: "org",
      pluginIds: ["plugin-1"],
      credentialIds: [],
    });
    expect(missing.valid).toBe(false);
    expect(missing.missingRequired).toHaveLength(1);
    const ready = evaluateWorkspacePluginSelection({
      plugins: [plugin],
      credentials: [token],
      organizationId: "org",
      pluginIds: ["plugin-1"],
      credentialIds: ["token"],
    });
    expect(ready.valid).toBe(true);
  });

  it("flags unavailable plugins, removed credentials and duplicate services", () => {
    const result = evaluateWorkspacePluginSelection({
      plugins: [{ ...plugin, organization_id: "other-org" }],
      credentials: [token, { ...token, id: "token-2" }],
      organizationId: "org",
      pluginIds: ["plugin-1"],
      credentialIds: ["token", "token-2", "gone"],
    });
    expect(result.catalog).toHaveLength(0);
    expect(result.unavailablePluginIds).toEqual(["plugin-1"]);
    expect(result.unavailableCredentialIds).toEqual(["gone"]);
    expect(result.duplicateServiceIds).toEqual(["github"]);
    expect(result.valid).toBe(false);
  });

  it("keeps one credential per service when toggling", () => {
    expect(
      toggleWorkspaceCredentialSelection(
        ["token"],
        { id: "token-2", service_id: "github" },
        [token, { ...token, id: "token-2" }],
      ),
    ).toEqual(["token-2"]);
  });
});

describe("workspace images and resources", () => {
  const options = workspaceImageOptions({
    definitions: [
      { id: "def", name: "Ubuntu", is_active: true, status: "active" },
      { id: "old", name: "Old", is_active: true, status: "pending_deletion" },
    ],
    buildsByDefinition: {
      def: [
        { runner_id: "r1", status: "active", image_artifact_id: "art-1" },
        { runner_id: "r2", status: "active", image_artifact_id: "art-2" },
        { runner_id: "r3", status: "active", image_artifact_id: "art-3" },
      ],
      old: [{ runner_id: "r1", status: "active", image_artifact_id: "art-x" }],
    },
    capturedImages: [
      {
        id: "cap",
        name: "node-dev",
        status: "active",
        runner_online: true,
        latest_id: "v2",
        latest_version: 2,
        versions: [{ id: "v2", status: "ready", source_runner_id: "r2" }],
      },
    ],
    runners: [runner("r1"), runner("r2"), runner("r3", "offline")],
  });

  it("offers active definitions on online runners and ready captured images", () => {
    expect(options.definitions.map((option) => option.label)).toEqual([
      "Ubuntu",
    ]);
    expect(options.definitions[0]!.runnerBuilds).toHaveLength(2);
    expect(options.captured.map((option) => option.label)).toEqual([
      "node-dev · v2",
    ]);
    expect(defaultWorkspaceImageOption(options)?.value).toBe("definition:def");
  });

  it("places workspaces only on compatible online runners", () => {
    const definition = options.definitions[0]!;
    expect(
      compatibleRunners(definition, [runner("r1"), runner("r2")]).map(
        (entry) => entry.id,
      ),
    ).toEqual(["r1", "r2"]);
    expect(definitionArtifactForRunner(definition, "r2")).toBe("art-2");
    expect(definitionArtifactForRunner(definition, "")).toBe("art-1");
    expect(
      compatibleRunners(options.captured[0], [runner("r1"), runner("r2")]).map(
        (entry) => entry.id,
      ),
    ).toEqual(["r2"]);
  });

  it("carries customized resources to another runner within its limits", () => {
    const limits = qemuResourceLimits(runner("r1"));
    expect(
      fitQemuResources(
        { vcpus: 2, memoryMb: 1024, diskSizeGb: 100 },
        DEFAULT_QEMU_LIMITS,
        limits,
      ),
    ).toEqual({ vcpus: 4, memoryMb: 8192, diskSizeGb: 100 });
  });

  it("patches only changed resources", () => {
    const current = {
      name: "demo",
      credential_ids: ["a"],
      plugin_ids: [],
      qemu_vcpus: null,
      qemu_memory_mb: 4096,
      qemu_disk_size_gb: 50,
      desktop_width: 1920,
      desktop_height: 1080,
    };
    const limits = qemuResourceLimits(runner("r1"));
    const form = workspaceSettingsForm(current, limits);
    expect(form.vcpus).toBe(4);
    expect(isWorkspaceSettingsChanged(current, form, limits)).toBe(false);
    const changed = { ...form, name: " renamed ", memoryMb: 8192 };
    expect(isWorkspaceSettingsChanged(current, changed, limits)).toBe(true);
    expect(workspaceUpdatePayload(current, changed, limits)).toEqual({
      name: "renamed",
      credential_ids: ["a"],
      plugin_ids: [],
      qemu_memory_mb: 8192,
    });
    expect(isDesktopSizeValid(1440, 900)).toBe(true);
    expect(isDesktopSizeValid(1441, 900)).toBe(false);
  });

  it("formats storage in binary units", () => {
    expect(formatStorageBytes(12.4 * 1024 ** 3)).toBe("12.4 GiB");
    expect(formatStorageBytes(null)).toBe("Unknown");
  });
});

describe("image versions", () => {
  const base = {
    id: "v3",
    line_kind: "captured" as const,
    line_id: "cap",
    name: "node-dev",
    version: 3,
    status: "ready",
    latest_id: "v4",
    latest_version: 4,
    update_available: true,
  };

  it("offers reset and update targets", () => {
    expect(
      workspaceRecreateOptions({ base_image: base }).map(
        (option) => option.kind,
      ),
    ).toEqual(["reset", "update"]);
    expect(workspaceRecreateOptions({ base_image: null })).toEqual([]);
  });

  it("captures into the workspace's own image by default", () => {
    const images = [
      {
        id: "cap",
        name: "node-dev",
        status: "active",
        runner_id: "r1",
        versions: [{ status: "ready", version: 3 }],
      },
      {
        id: "busy",
        name: "busy",
        status: "active",
        runner_id: "r1",
        versions: [{ status: "capturing", version: 1 }],
      },
    ];
    const candidates = captureTargetCandidates(images, { runner_id: "r1" });
    expect(candidates.map((image) => image.id)).toEqual(["cap"]);
    expect(defaultCaptureTarget(candidates, { base_image: base })).toBe("cap");
    expect(defaultCaptureTarget(candidates, { base_image: null })).toBe(
      NEW_IMAGE,
    );
    expect(nextImageVersion(images[0])).toBe(4);
  });

  it("labels other users' workspaces by owner only", () => {
    expect(sharedWorkspaceLabel({ name: "api", owner_label: "Ada" })).toBe(
      "api",
    );
    expect(sharedWorkspaceLabel({ name: "", owner_label: "Ada" })).toBe(
      "Workspace · Ada",
    );
    expect(sharedWorkspaceLabel({ name: "" })).toBe("Workspace");
  });
});
