import React from "react";
import {
  definitionNeedsPolling,
  formatRunnerBuildSummary,
  visibleImageDefinitions,
} from "@opencuria/chat-core/imageDefinitionLifecycle";
import type { ImageDefinition } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";

export const IMAGE_DEFINITIONS = "image-definitions";
export const RUNNERS = "runners";

const STATUS_LABELS: Record<string, string> = {
  deactivated: "Deactivated",
  pending_deletion: "Removing",
  deleting: "Removing",
  delete_failed: "Remove failed",
};

/** "Custom · Base ubuntu:22.04 · 1 active, 0 building, 0 failed". */
export function definitionSubtitle(definition: ImageDefinition): string {
  return [
    STATUS_LABELS[definition.status],
    definition.is_standard ? "Standard" : "Custom",
    `Base ${definition.base_distro || "unknown"}`,
    formatRunnerBuildSummary(definition.runner_build_summary),
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Image recipes; build them on a runner to use them for new workspaces. */
export function ImageDefinitionsPage(): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const definitions = useResource(resources, IMAGE_DEFINITIONS, async () =>
    visibleImageDefinitions(await client.images.listDefinitions()),
  );
  usePolling(
    definitions.reload,
    3000,
    (definitions.data ?? []).some((definition) =>
      definitionNeedsPolling(definition.status),
    ),
  );
  const sections: NativeFormSection[] = definitions.data?.length
    ? [
        {
          id: "definitions",
          footer:
            "An image definition is a recipe. Build it on a runner to make it available for new workspaces. Rebuilding or removing an image does not delete existing workspaces.",
          rows: definitions.data.map((definition) => ({
            kind: "item" as const,
            id: `definition-${definition.id}`,
            testID: `definition-${definition.id}`,
            title: definition.name,
            subtitle: definitionSubtitle(definition),
            subtitleTone: STATUS_LABELS[definition.status]
              ? "warning"
              : "muted",
            busy: definitionNeedsPolling(definition.status),
            onPress: () =>
              navigation.push(route("image-definition", definition.id)),
          })),
        },
      ]
    : [];
  return (
    <SettingsPage
      title="Image definitions"
      sections={
        definitions.error && !definitions.data
          ? loadErrorSections(
              definitions.error,
              () => void definitions.reload(),
            )
          : sections
      }
      loading={!definitions.data && !definitions.error}
      onRefresh={definitions.reload}
      empty={{
        title: "No image definitions",
        description:
          "Create a recipe, then build it on a runner to use it for new workspaces.",
        symbol: "square.stack.3d.up",
      }}
      primaryAction={
        isAdmin
          ? {
              title: "New definition",
              symbol: "plus",
              onPress: () =>
                navigation.push(route("image-definition-editor", "new")),
              testID: "new-image-definition",
            }
          : undefined
      }
      testID="settings-image-definitions"
    />
  );
}
