/**
 * First-run guide shared by web and mobile: the steps between a new user and
 * a first chat, in order, with their state. Clients render the steps and map
 * each step id to their own action (a settings page, the create sheet).
 */

import type { HarnessEngine } from "./harnessModels";
import {
  isDefinitionSelectableForWorkspace,
  workspaceImageOptions,
  type CapturedImageOptionInput,
  type ImageDefinitionOptionInput,
  type RunnerImageBuildOptionInput,
} from "./workspaceCreation";

export type WorkspaceSetupStepId =
  | "runner"
  | "image"
  | "provider"
  | "workspace";

export interface WorkspaceSetupStep {
  id: WorkspaceSetupStepId;
  title: string;
  detail: string;
  done: boolean;
  /** Its prerequisites are met, so its action can run now. */
  available: boolean;
  /** Label of the step's action button. */
  action: string;
}

export interface WorkspaceSetupInput {
  /** Admins set up runners and images; members never see those steps. */
  isAdmin: boolean;
  runners: Array<{ id: string; name: string; status: string }>;
  definitions: ImageDefinitionOptionInput[];
  buildsByDefinition: Record<string, RunnerImageBuildOptionInput[] | undefined>;
  capturedImages: CapturedImageOptionInput[];
  /** Runner or image lists failed to load: unknown readiness never blocks. */
  imagesUnknown?: boolean;
  /** See {@link isModelProviderConnected}. */
  providerConnected: boolean;
}

/**
 * Whether an agent can answer: the built-in engine lists models (a provider
 * account is connected) or an external engine such as Claude is connected.
 */
export function isModelProviderConnected(
  nativeModels: readonly unknown[],
  engines: ReadonlyArray<Pick<HarnessEngine, "id" | "connected">>,
): boolean {
  return (
    nativeModels.length > 0 ||
    engines.some((engine) => engine.id !== "native" && engine.connected)
  );
}

/** The setup steps for this user, in the order they are usually done. */
export function workspaceSetupSteps(
  input: WorkspaceSetupInput,
): WorkspaceSetupStep[] {
  const options = workspaceImageOptions({
    definitions: input.definitions,
    buildsByDefinition: input.buildsByDefinition,
    capturedImages: input.capturedImages,
    runners: input.runners,
  });
  const readyImage = options.definitions[0] ?? options.captured[0];
  const imageReady = readyImage !== undefined;
  const steps: WorkspaceSetupStep[] = [];

  if (input.isAdmin) {
    const online = input.runners.find((runner) => runner.status === "online");
    const registered = input.runners[0];
    const buildable = input.definitions.find(isDefinitionSelectableForWorkspace);
    steps.push(
      {
        id: "runner",
        title: "Connect a runner",
        detail: online
          ? `${runnerLabel(online.name)} is online.`
          : registered
            ? `${runnerLabel(registered.name)} is offline. Start it on its host.`
            : "Register a runner on a KVM host.",
        done: online !== undefined,
        available: true,
        action: "Runners",
      },
      {
        id: "image",
        title: "Build an image",
        detail: readyImage
          ? `${readyImage.label} is ready.`
          : buildable
            ? `Build ${buildable.name} on your runner.`
            : "Add an image definition and build it.",
        done: imageReady,
        available: online !== undefined,
        action: "Images",
      },
    );
  }

  steps.push(
    {
      id: "provider",
      title: "Connect a model provider",
      detail: input.providerConnected
        ? "Models are ready for your agents."
        : "Add an account, e.g. OpenRouter or ChatGPT.",
      done: input.providerConnected,
      available: true,
      action: "Providers",
    },
    {
      id: "workspace",
      title: "Create a workspace",
      detail:
        imageReady || input.imagesUnknown || input.isAdmin
          ? "Name it and choose the repositories to clone."
          : "Available as soon as a workspace image is ready.",
      done: false,
      available: imageReady || Boolean(input.imagesUnknown),
      action: "Create",
    },
  );
  return steps;
}

/** The step to do next: the first one not done yet. */
export function currentWorkspaceSetupStep(
  steps: readonly WorkspaceSetupStep[],
): WorkspaceSetupStep | undefined {
  return steps.find((step) => !step.done);
}

function runnerLabel(name: string): string {
  return name.trim() || "Your runner";
}
