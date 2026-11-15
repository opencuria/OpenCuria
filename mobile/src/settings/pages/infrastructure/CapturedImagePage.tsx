import React from "react";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { deletionRoute } from "../DeletionPage";
import { CAPTURED_IMAGES } from "./CapturedImagesPage";
import {
  canDeleteVersion,
  capturedImageSubtitle,
  isVersionBusy,
  versionSubtitle,
  versionTitle,
} from "./images";
import { CreateWorkspaceOverlay } from "./sheets";

/** One captured image: new workspaces from it, renaming, its versions and deletion. */
export function CapturedImagePage({
  imageId,
}: {
  imageId: string;
}): React.JSX.Element {
  const { client, resources, refreshWorkspaces } = useSettings();
  const navigation = useSettingsNavigation();
  const images = useResource(resources, CAPTURED_IMAGES, () =>
    client.images.listCaptured(),
  );
  const image = images.data?.find((entry) => entry.id === imageId);
  if (!image)
    return (
      <SettingsPage
        title="Captured image"
        sections={[]}
        loading={!images.data && !images.error}
        empty={{
          title: "Image unavailable",
          description: images.error ?? "This image no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-captured-image"
      />
    );

  const active = image.status === "active";
  const latest = image.versions.find(
    (version) => version.id === image.latest_id,
  );
  const newWorkspace = () =>
    navigation.present(
      <CreateWorkspaceOverlay
        imageValue={`captured:${latest!.id}`}
        onClose={() => navigation.present(null)}
        onCreated={async () => {
          navigation.present(null);
          showToast({
            title: "Creating workspace",
            message: "New workspace is being created from image.",
            tone: "success",
          });
          await refreshWorkspaces();
        }}
      />,
    );

  const sections: NativeFormSection[] = [
    {
      id: "image",
      footer: capturedImageSubtitle(image),
      rows: [
        ...(latest
          ? [
              {
                kind: "button" as const,
                id: "new-workspace",
                title: "New workspace",
                symbol: "plus" as const,
                disabled: !image.runner_online,
                onPress: newWorkspace,
              },
            ]
          : []),
        ...(active
          ? [
              {
                kind: "link" as const,
                id: "rename",
                title: "Rename",
                symbol: "pencil" as const,
                route: route("captured-image-rename", image.id),
              },
            ]
          : []),
      ],
    },
    {
      id: "versions",
      title: "Versions",
      footer: active
        ? "Swipe or long-press an older version to delete it. The latest version is deleted with the image."
        : undefined,
      rows: image.versions.map((version) => {
        const deletable = canDeleteVersion(version, active);
        const target = deletionRoute(
          { target_type: "image", target_id: version.id },
          `${image.name} · v${version.version ?? "?"}`,
        );
        return {
          kind: "item" as const,
          id: `version-${version.id}`,
          testID: `version-${version.id}`,
          title: versionTitle(version),
          subtitle: versionSubtitle(version),
          busy: isVersionBusy(version),
          actions: deletable
            ? [
                {
                  id: "delete",
                  title: `Delete v${version.version ?? "?"}`,
                  symbol: "trash",
                  destructive: true,
                },
              ]
            : undefined,
          trailingSwipe: deletable
            ? [
                {
                  id: "delete",
                  title: "Delete",
                  symbol: "trash" as const,
                  destructive: true,
                },
              ]
            : undefined,
          onAction: () => navigation.push(target),
        };
      }),
    },
    {
      id: "delete",
      rows: [
        {
          kind: "button",
          id: "delete-image",
          title: "Delete image",
          symbol: "trash",
          destructive: true,
          onPress: () =>
            navigation.push(
              deletionRoute(
                { target_type: "captured_image", target_id: image.id },
                image.name,
              ),
            ),
        },
      ],
    },
  ];

  return (
    <SettingsPage
      title={image.name}
      sections={sections}
      onRefresh={images.reload}
      testID="settings-captured-image"
    />
  );
}

/** Renames a captured image; an empty or unchanged name changes nothing. */
export function RenameCapturedImagePage({
  imageId,
}: {
  imageId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const images = useResource(resources, CAPTURED_IMAGES, () =>
    client.images.listCaptured(),
  );
  const image = images.data?.find((entry) => entry.id === imageId);
  const [name, setName] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const value = name ?? image?.name ?? "";
  const changed = Boolean(image && value.trim() && value.trim() !== image.name);

  const save = async () => {
    if (!image || !changed) {
      navigation.pop();
      return;
    }
    setBusy(true);
    try {
      const saved = await client.images.renameCaptured(image.id, value.trim());
      images.set((current) =>
        (current ?? []).map((entry) =>
          entry.id === saved.id ? { ...entry, ...saved } : entry,
        ),
      );
      showToast({
        title: "Image renamed",
        message: `Image renamed to "${saved.name}".`,
        tone: "success",
      });
      navigation.pop();
    } catch (cause) {
      showToast({
        title: "Rename failed",
        message: cause instanceof Error ? cause.message : "Please try again.",
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsPage
      title="Rename image"
      sections={[
        {
          id: "name",
          rows: [
            {
              kind: "text",
              id: "image-name",
              label: "Name",
              placeholder: "e.g. node-dev",
              value,
              autoFocus: true,
              onChange: setName,
              onSubmit: () => void save(),
            },
          ],
        },
      ]}
      primaryAction={{
        title: "Save",
        onPress: () => void save(),
        busy,
        disabled: !changed || busy,
        testID: "save-image-name",
      }}
      testID="settings-rename-image"
    />
  );
}
