import React from "react";
import type { Workspace } from "@opencuria/client";
import { CaptureImageSheet } from "../../../workspaces/CaptureImageSheet";
import { CreateWorkspaceSheet } from "../../../workspaces/CreateWorkspaceSheet";
import { useWorkspaceCatalog } from "../../../workspaces/useWorkspaceCatalog";
import { useSettings } from "../../SettingsContext";

/**
 * The workspace capture sheet above Settings; it loads its own catalog so
 * that its image choices are current.
 */
export function CaptureOverlay({
  workspace,
  onClose,
  onCaptured,
}: {
  workspace: Workspace;
  onClose: () => void;
  onCaptured: () => Promise<void>;
}): React.JSX.Element {
  const { client } = useSettings();
  const catalog = useWorkspaceCatalog(client, true);
  return (
    <CaptureImageSheet
      workspace={workspace}
      client={client}
      catalog={catalog}
      onClose={onClose}
      onCaptured={onCaptured}
    />
  );
}

/** The New workspace sheet above Settings, starting from a given image. */
export function CreateWorkspaceOverlay({
  imageValue,
  onClose,
  onCreated,
}: {
  /** Image option such as `captured:<latest version id>`. */
  imageValue: string;
  onClose: () => void;
  onCreated: () => Promise<void>;
}): React.JSX.Element {
  const { client, organizationId } = useSettings();
  const catalog = useWorkspaceCatalog(client, true);
  return (
    <CreateWorkspaceSheet
      visible
      client={client}
      catalog={catalog}
      organizationId={organizationId}
      initialImageValue={imageValue}
      onClose={onClose}
      onCreated={onCreated}
    />
  );
}
