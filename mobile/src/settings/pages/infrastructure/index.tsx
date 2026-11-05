import React from "react";
import { BuildLogPage } from "./BuildLogPage";
import {
  CapturedImagePage,
  RenameCapturedImagePage,
} from "./CapturedImagePage";
import { CapturedImagesPage } from "./CapturedImagesPage";
import { ImageDefinitionEditorPage } from "./ImageDefinitionEditorPage";
import { ImageDefinitionPage } from "./ImageDefinitionPage";
import { ImageDefinitionsPage } from "./ImageDefinitionsPage";
import { NewRunnerPage, RunnerTokenPage } from "./NewRunnerPage";
import { RebuildPage } from "./RebuildPage";
import { RunnerLimitsPage } from "./RunnerLimitsPage";
import { RunnerOperationPage } from "./RunnerOperationPage";
import { RunnerPage } from "./RunnerPage";
import { RunnersPage } from "./RunnersPage";

/** "<first>:<rest>" of a route argument. */
function pair(argument: string): [string, string] {
  const index = argument.indexOf(":");
  return index < 0
    ? [argument, ""]
    : [argument.slice(0, index), argument.slice(index + 1)];
}

/**
 * Pages for Captured images, Image definitions and Runners.
 * Returns `undefined` for routes this group does not own.
 */
export function renderInfrastructureRoute(
  page: string,
  argument: string,
): React.ReactNode | undefined {
  const [first, rest] = pair(argument);
  switch (page) {
    case "captured-images":
      return <CapturedImagesPage />;
    case "captured-image":
      return argument ? <CapturedImagePage imageId={argument} /> : undefined;
    case "captured-image-rename":
      return argument ? (
        <RenameCapturedImagePage imageId={argument} />
      ) : undefined;
    case "image-definitions":
      return <ImageDefinitionsPage />;
    case "image-definition":
      return argument ? (
        <ImageDefinitionPage definitionId={argument} />
      ) : undefined;
    case "image-definition-editor":
      return argument ? (
        <ImageDefinitionEditorPage
          definitionId={argument === "new" ? null : argument}
        />
      ) : undefined;
    case "image-definition-rebuild":
      return first && rest ? (
        <RebuildPage definitionId={first} runnerId={rest} />
      ) : undefined;
    case "build-log":
      return first && rest ? (
        <BuildLogPage definitionId={first} runnerId={rest} />
      ) : undefined;
    case "runners":
      return <RunnersPage />;
    case "runner":
      return argument ? <RunnerPage runnerId={argument} /> : undefined;
    case "runner-new":
      return <NewRunnerPage />;
    case "runner-token":
      return argument ? <RunnerTokenPage runnerId={argument} /> : undefined;
    case "runner-limits":
      return argument ? <RunnerLimitsPage runnerId={argument} /> : undefined;
    case "runner-operation":
      return argument ? (
        <RunnerOperationPage operationId={argument} />
      ) : undefined;
    default:
      return undefined;
  }
}
