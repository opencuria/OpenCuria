import { describe, expect, it } from "vitest";
import { isNewChatModelMissing } from "../src/harnessModels";
import {
  currentWorkspaceSetupStep,
  isModelProviderConnected,
  workspaceSetupSteps,
  type WorkspaceSetupInput,
} from "../src/workspaceSetup";

const ubuntu = {
  id: "def-1",
  name: "Ubuntu 24.04",
  is_active: true,
  status: "active",
};
const readyBuilds = {
  "def-1": [
    { runner_id: "runner-1", status: "active", image_artifact_id: "a1" },
  ],
};

function input(overrides: Partial<WorkspaceSetupInput> = {}): WorkspaceSetupInput {
  return {
    isAdmin: true,
    runners: [],
    definitions: [ubuntu],
    buildsByDefinition: {},
    capturedImages: [],
    providerConnected: false,
    ...overrides,
  };
}

function byId(steps: ReturnType<typeof workspaceSetupSteps>, id: string) {
  return steps.find((step) => step.id === id)!;
}

describe("workspaceSetupSteps", () => {
  it("leads an admin through runner, image, provider and workspace", () => {
    const fresh = workspaceSetupSteps(input());
    expect(fresh.map((step) => step.id)).toEqual([
      "runner",
      "image",
      "provider",
      "workspace",
    ]);
    expect(currentWorkspaceSetupStep(fresh)?.id).toBe("runner");
    expect(byId(fresh, "runner").detail).toBe("Register a runner on a KVM host.");
    // A provider can be connected at any time, e.g. while the image builds.
    expect(byId(fresh, "provider").available).toBe(true);
    expect(byId(fresh, "image").available).toBe(false);
    expect(byId(fresh, "workspace").available).toBe(false);

    const online = [{ id: "runner-1", name: "host-1", status: "online" }];
    const building = workspaceSetupSteps(input({ runners: online }));
    expect(byId(building, "runner")).toMatchObject({
      done: true,
      detail: "host-1 is online.",
    });
    expect(currentWorkspaceSetupStep(building)?.id).toBe("image");
    expect(byId(building, "image").detail).toBe("Build Ubuntu 24.04 on your runner.");

    const ready = workspaceSetupSteps(
      input({ runners: online, buildsByDefinition: readyBuilds }),
    );
    expect(byId(ready, "image")).toMatchObject({
      done: true,
      detail: "Ubuntu 24.04 is ready.",
    });
    expect(currentWorkspaceSetupStep(ready)?.id).toBe("provider");
    expect(byId(ready, "workspace").available).toBe(true);

    const connected = workspaceSetupSteps(
      input({
        runners: online,
        buildsByDefinition: readyBuilds,
        providerConnected: true,
      }),
    );
    expect(currentWorkspaceSetupStep(connected)?.id).toBe("workspace");
    expect(connected.filter((step) => step.done)).toHaveLength(3);
  });

  it("names offline and unnamed runners without an empty label", () => {
    const offline = workspaceSetupSteps(
      input({ runners: [{ id: "r", name: "", status: "offline" }] }),
    );
    expect(byId(offline, "runner").detail).toBe(
      "Your runner is offline. Start it on its host.",
    );
  });

  it("shows members only their own steps and no admin details", () => {
    const steps = workspaceSetupSteps(input({ isAdmin: false }));
    expect(steps.map((step) => step.id)).toEqual(["provider", "workspace"]);
    expect(byId(steps, "workspace")).toMatchObject({
      available: false,
      detail: "Available as soon as a workspace image is ready.",
    });
    const text = steps.map((step) => `${step.title} ${step.detail}`).join(" ");
    expect(text).not.toMatch(/runner|admin|settings/i);
  });

  it("does not block on images when their availability is unknown", () => {
    const steps = workspaceSetupSteps(
      input({ isAdmin: false, imagesUnknown: true }),
    );
    expect(byId(steps, "workspace").available).toBe(true);
  });
});

describe("isModelProviderConnected", () => {
  it("accepts native models or a connected external engine", () => {
    const native = { id: "native" as const, connected: true };
    expect(isModelProviderConnected([], [native])).toBe(false);
    expect(isModelProviderConnected([{ id: "m" }], [native])).toBe(true);
    expect(
      isModelProviderConnected([], [native, { id: "claude", connected: true }]),
    ).toBe(true);
    expect(
      isModelProviderConnected([], [native, { id: "codex", connected: false }]),
    ).toBe(false);
  });
});

describe("isNewChatModelMissing", () => {
  it("requires a model only on the built-in engine", () => {
    expect(isNewChatModelMissing("native", "")).toBe(true);
    expect(isNewChatModelMissing("native", "  ")).toBe(true);
    expect(isNewChatModelMissing("native", "openrouter/x")).toBe(false);
    expect(isNewChatModelMissing("claude", "")).toBe(false);
    expect(isNewChatModelMissing("codex", "")).toBe(false);
  });
});
