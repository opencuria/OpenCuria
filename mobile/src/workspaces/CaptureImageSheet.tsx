import React, { useState } from "react";
import {
  NEW_IMAGE,
  captureTargetCandidates,
  defaultCaptureTarget,
  nextImageVersion,
} from "@opencuria/chat-core/imageVersions";
import type { OpenCuriaClient, Workspace } from "@opencuria/client";
import { NativeForm } from "../ui/NativeForm";
import type { NativeFormSection } from "../ui/NativeForm.types";
import { NativeModal } from "../ui/primitives";
import { showToast } from "../ui/toast";
import type { WorkspaceCatalog } from "./useWorkspaceCatalog";
import { errorText } from "./workspaceOperations";
import { workspaceState } from "./workspacePresentation";

export interface CaptureImageSheetProps {
  workspace: Workspace | null;
  client: OpenCuriaClient;
  catalog: WorkspaceCatalog;
  onClose: () => void;
  onCaptured: () => Promise<void>;
}

const MESSAGE_LIMIT = 500;

/** Capture a workspace as a new image or as the next version of its image. */
export function CaptureImageSheet(
  props: CaptureImageSheetProps,
): React.JSX.Element | null {
  if (!props.workspace) return null;
  return (
    <CaptureSheet
      key={props.workspace.id}
      {...props}
      workspace={props.workspace}
    />
  );
}

function CaptureSheet({
  workspace,
  client,
  catalog,
  onClose,
  onCaptured,
}: CaptureImageSheetProps & { workspace: Workspace }): React.JSX.Element {
  const candidates = captureTargetCandidates(catalog.capturedImages, workspace);
  const [chosen, setChosen] = useState<string | null>(null);
  const target = chosen ?? defaultCaptureTarget(candidates, workspace);
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const state = workspaceState(workspace);
  const blockedByCredentials =
    workspace.status === "stopped" && Boolean(workspace.credentials_present);
  const valid =
    state.actions.capture &&
    (target === NEW_IMAGE
      ? Boolean(name.trim())
      : candidates.some((image) => image.id === target));

  const capture = async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    try {
      await client.workspaces.captureImage(
        workspace.id,
        target === NEW_IMAGE
          ? { name: name.trim(), message: message.trim() }
          : { captured_image_id: target, message: message.trim() },
      );
      onClose();
      showToast({
        title: "Capturing image",
        message: `${workspace.name} briefly stops and continues on the new version.`,
      });
      void onCaptured();
    } catch (cause) {
      showToast({
        title: "Capture refused",
        message: errorText(
          cause,
          "Check the runner's operations for the exact diagnostic.",
        ),
        tone: "error",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const sections: NativeFormSection[] = [
    {
      id: "target",
      title: "Save as",
      rows: [
        {
          kind: "picker",
          id: "capture-target",
          label: "Image",
          value: target,
          groups: [
            { options: [{ value: NEW_IMAGE, label: "New image" }] },
            ...(candidates.length
              ? [
                  {
                    title: "New version of",
                    options: candidates.map((image) => ({
                      value: image.id,
                      label: `${image.name} · v${nextImageVersion(image)}`,
                    })),
                  },
                ]
              : []),
          ],
          onChange: setChosen,
          disabled: submitting,
        },
        ...(target === NEW_IMAGE
          ? [
              {
                kind: "text" as const,
                id: "capture-name",
                label: "Image name",
                placeholder: "Image name, e.g. node-dev",
                value: name,
                onChange: setName,
                disabled: submitting,
              },
            ]
          : []),
      ],
    },
    {
      id: "message",
      title: "Notes",
      rows: [
        {
          kind: "text",
          id: "capture-message",
          label: "What changed?",
          placeholder: "Optional, e.g. Installed Playwright browsers",
          value: message,
          multiline: true,
          onChange: (value) => setMessage(value.slice(0, MESSAGE_LIMIT)),
          disabled: submitting,
        },
      ],
      footer: blockedByCredentials
        ? "Credentials are still on disk. Start and stop the workspace to remove them before capturing."
        : "The workspace briefly stops and continues on the captured version. Files, chats and settings are kept; running processes stop.",
      footerTone: blockedByCredentials ? "danger" : "muted",
    },
  ];

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Capture image"
      scrollable={false}
      primaryAction={{
        title: "Capture",
        onPress: () => {
          void capture();
        },
        disabled: !valid,
        busy: submitting,
        testID: "capture-submit",
      }}
      testID="capture-image-sheet"
    >
      <NativeForm sections={sections} />
    </NativeModal>
  );
}
