import React from "react";
import {
  definitionNeedsPolling,
  getDefinitionStatus,
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
import { nativeTone } from "./images";

export const IMAGE_DEFINITIONS = "image-definitions";
export const RUNNERS = "runners";

/** "Ready · Standard · ubuntu:22.04": one status across all runners. */
export function definitionSubtitle(definition: ImageDefinition): string {
  return [
    getDefinitionStatus(definition).label,
    definition.is_standard ? "Standard" : "",
    definition.base_distro,
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
      definitionNeedsPolling(definition),
    ),
  );
  const sections: NativeFormSection[] = definitions.data?.length
    ? [
        {
          id: "definitions",
          footer: "Build a recipe on a runner to start new workspaces from it.",
          rows: definitions.data.map((definition) => ({
            kind: "item" as const,
            id: `definition-${definition.id}`,
            testID: `definition-${definition.id}`,
            title: definition.name,
            subtitle: definitionSubtitle(definition),
            subtitleTone: nativeTone(getDefinitionStatus(definition).tone),
            busy: definitionNeedsPolling(definition),
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
        title: "No image definitions yet",
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
