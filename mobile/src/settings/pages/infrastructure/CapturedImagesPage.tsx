import React from "react";
import { getWorkspaceActionAvailability } from "@opencuria/chat-core/workspaceLifecycle";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";
import { capturedImageSubtitle } from "./images";
import { CaptureOverlay } from "./sheets";

export const CAPTURED_IMAGES = "captured-images";

/** Versioned images captured from workspaces; new workspaces use the latest version. */
export function CapturedImagesPage(): React.JSX.Element {
  const { client, resources, workspaces, refreshWorkspaces } = useSettings();
  const navigation = useSettingsNavigation();
  const images = useResource(resources, CAPTURED_IMAGES, () =>
    client.images.listCaptured(),
  );
  const capturing = (images.data ?? []).some((image) =>
    image.versions.some((version) => version.status === "capturing"),
  );
  usePolling(images.reload, capturing ? 3000 : 15000, Boolean(images.data));

  const capturable = workspaces.filter(
    (workspace) => getWorkspaceActionAvailability(workspace).capture,
  );
  const capture = () =>
    navigation.choose({
      title: "Capture workspace",
      value: "",
      footer:
        "The workspace briefly stops and continues on the captured version. Files, chats and settings are kept; running processes stop.",
      emptyText:
        "No workspace can be captured right now. Running workspaces and stopped ones without credentials can be captured.",
      groups: [
        {
          options: capturable.map((workspace) => ({
            value: workspace.id,
            label: workspace.name,
            subtitle: workspace.status === "running" ? "Running" : "Stopped",
          })),
        },
      ],
      onSelect: (id) => {
        const workspace = capturable.find((entry) => entry.id === id);
        if (!workspace) return;
        navigation.present(
          <CaptureOverlay
            workspace={workspace}
            onClose={() => navigation.present(null)}
            onCaptured={async () => {
              navigation.present(null);
              showToast({
                title: "Image capturing",
                message: "Image is being captured.",
                tone: "success",
              });
              await Promise.allSettled([images.reload(), refreshWorkspaces()]);
            }}
          />,
        );
      },
    });

  const sections: NativeFormSection[] = images.data?.length
    ? [
        {
          id: "images",
          footer:
            "Versioned images captured from your workspaces. New workspaces always use the latest version.",
          rows: images.data.map((image) => ({
            kind: "item" as const,
            id: `image-${image.id}`,
            testID: `image-${image.id}`,
            title: image.name,
            subtitle: capturedImageSubtitle(image),
            subtitleTone:
              image.status === "active" && image.runner_online
                ? "muted"
                : "warning",
            busy: image.versions.some(
              (version) => version.status === "capturing",
            ),
            onPress: () => navigation.push(route("captured-image", image.id)),
          })),
        },
      ]
    : [];

  return (
    <SettingsPage
      title="Captured images"
      sections={
        images.error && !images.data
          ? loadErrorSections(images.error, () => void images.reload())
          : sections
      }
      loading={!images.data && !images.error}
      onRefresh={images.reload}
      empty={{
        title: "No captured images yet",
        description:
          "Capture a workspace to reuse its filesystem for new workspaces.",
        symbol: "camera",
      }}
      primaryAction={{
        title: "Capture image",
        symbol: "camera",
        onPress: capture,
        testID: "capture-image",
      }}
      testID="settings-captured-images"
    />
  );
}
