import React, { useCallback, useEffect, useState } from "react";
import { Alert } from "react-native";
import type {
  DeletionGraph,
  DeletionRequest,
  DeletionTarget,
  DeletionTargetType,
} from "@opencuria/client";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../ui/NativeForm.types";
import { showToast } from "../../ui/toast";
import { errorMessage } from "../resources";
import { route } from "../routes";
import { useSettings } from "../SettingsContext";
import { SettingsPage } from "../SettingsPage";

const KIND: Record<DeletionTargetType, string> = {
  image: "image version",
  captured_image: "image with all versions",
  assignment: "runner build",
  definition: "image definition",
};
const DONE = new Set(["completed", "cancelled"]);

/**
 * Route of the deletion page for an image version, captured image, runner
 * build or image definition; `name` labels it.
 */
export function deletionRoute(target: DeletionTarget, name = ""): string {
  return route(
    "deletion",
    target.target_type,
    target.target_id,
    encodeURIComponent(name),
  );
}

/** Parses the argument of a `deletion:` route. */
export function parseDeletionRoute(
  argument: string,
): { target: DeletionTarget; name: string } | null {
  const [type, id, ...name] = argument.split(":");
  if (!type || !id || !(type in KIND)) return null;
  return {
    target: { target_type: type as DeletionTargetType, target_id: id },
    name: decodeURIComponent(name.join(":")),
  };
}

/** "Waiting dependency" from "waiting_dependency". */
export function phaseLabel(phase: string): string {
  const text = phase.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Requests deletion as the webapp does: deferred by default, which waits
 * until nothing uses the target; admins can review a forced deletion of
 * exactly the previewed images and workspaces. Pending requests can be
 * cancelled. Pages listing the target reload afterwards.
 */
export function DeletionPage({
  target,
  name,
}: {
  target: DeletionTarget;
  name: string;
}): React.JSX.Element {
  const { client, isAdmin, resources } = useSettings();
  const [request, setRequest] = useState<DeletionRequest | null>(null);
  const [graph, setGraph] = useState<DeletionGraph | null>(null);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const preview = useCallback(async () => {
    if (!isAdmin) return;
    setApproved(false);
    try {
      setGraph(await client.deletions.preview(target));
    } catch (cause) {
      setGraph(null);
      setError(errorMessage(cause, "Could not preview this deletion."));
    }
  }, [client, isAdmin, target]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const requests = await client.deletions.list();
        if (!active) return;
        setRequest(
          requests.find(
            (entry) =>
              entry.target_type === target.target_type &&
              entry.target_id === target.target_id &&
              !DONE.has(entry.phase),
          ) ?? null,
        );
      } catch (cause) {
        if (active)
          setError(errorMessage(cause, "Could not load deletion requests."));
      } finally {
        if (active) setLoading(false);
      }
    })();
    void preview();
    return () => {
      active = false;
    };
    // The target is fixed for this page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const changed = () => {
    // Lists of images, definitions and runners show the new state.
    resources.invalidate("");
  };

  const run = async (
    id: string,
    action: () => Promise<DeletionRequest>,
    success: string,
  ) => {
    setBusy(id);
    setError(null);
    try {
      const result = await action();
      setRequest(DONE.has(result.phase) ? null : result);
      changed();
      showToast({ title: success, tone: "success" });
      if (result.phase === "reconfirmation_required") await preview();
    } catch (cause) {
      setError(errorMessage(cause, "The deletion request failed."));
      if (id === "force") await preview();
    } finally {
      setBusy(null);
    }
  };

  const requestDeferred = () =>
    void run(
      "deferred",
      () => client.deletions.request(target, "deferred"),
      "Deletion requested",
    );
  const requestForce = () =>
    Alert.alert(
      "Delete permanently?",
      `${graph?.counts.images ?? 0} images and ${graph?.counts.workspaces ?? 0} workspaces will be removed, including other owners' data. This cannot be undone.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete permanently",
          style: "destructive",
          onPress: () =>
            void run(
              "force",
              () =>
                client.deletions.request(target, "force", graph!.fingerprint),
              "Permanent deletion requested",
            ),
        },
      ],
    );
  const cancel = (pending: DeletionRequest) =>
    void run(
      "cancel",
      () => client.deletions.cancel(pending.id),
      "Deletion request cancelled",
    );

  const blocked = Boolean(graph?.blockers.length);
  const sections: NativeFormSection[] = [
    {
      id: "target",
      footer:
        "Ordinary deletion stores intent now and waits until no workspace uses it. It does not delete dependent workspaces.",
      rows: [
        {
          kind: "value",
          id: "target-name",
          label: name || "Target",
          value: KIND[target.target_type],
        },
      ],
    },
    ...(request
      ? [
          {
            id: "pending",
            title: "Pending request",
            footer: request.diagnostic || undefined,
            rows: [
              {
                kind: "value" as const,
                id: "phase",
                label:
                  request.mode === "force" ? "Permanent deletion" : "Deletion",
                value: phaseLabel(request.phase),
              },
              ...(request.can_cancel
                ? [
                    {
                      kind: "button" as const,
                      id: "cancel-request",
                      title: "Cancel pending request",
                      symbol: "xmark.circle.fill" as const,
                      busy: busy === "cancel",
                      disabled: Boolean(busy),
                      onPress: () => cancel(request),
                    },
                  ]
                : []),
            ],
          },
        ]
      : []),
    ...(error
      ? [
          {
            id: "error",
            footer: error,
            footerTone: "danger" as const,
            rows: [],
          },
        ]
      : []),
    ...(isAdmin && graph
      ? [
          {
            id: "force",
            title: "Force deletion",
            footer:
              "All listed images and workspaces will be permanently removed, including other owners' data.",
            footerTone: "danger" as const,
            rows: [
              {
                kind: "value" as const,
                id: "counts",
                label: "Removes",
                value: `${graph.counts.images} images · ${graph.counts.workspaces} workspaces`,
              },
              ...graph.images.map(
                (image): NativeFormRow => ({
                  kind: "value",
                  id: `image-${image.id}`,
                  label: image.name,
                  value: `Image · ${image.owner_label || image.owner_id || "unknown owner"}`,
                }),
              ),
              // Only the owner gets a workspace name; others show by owner.
              ...graph.workspaces.map(
                (workspace): NativeFormRow => ({
                  kind: "value",
                  id: `workspace-${workspace.id}`,
                  label: workspace.name || "Workspace",
                  value: `Workspace · ${workspace.owner_label || workspace.owner_id || "unknown owner"}`,
                }),
              ),
              ...graph.blockers.map(
                (blocker, index): NativeFormRow => ({
                  kind: "note",
                  id: `blocker-${index}`,
                  text: blocker,
                  tone: "danger",
                }),
              ),
              {
                kind: "toggle" as const,
                id: "approve",
                label: "Approve permanent deletion of exactly this preview",
                value: approved,
                disabled: blocked || Boolean(busy),
                onChange: setApproved,
              },
              {
                kind: "button" as const,
                id: "revalidate",
                title: "Revalidate preview",
                symbol: "arrow.clockwise" as const,
                disabled: Boolean(busy),
                onPress: () => void preview(),
              },
              {
                kind: "button" as const,
                id: "force-delete",
                title: "Delete permanently",
                symbol: "trash" as const,
                destructive: true,
                busy: busy === "force",
                disabled: !approved || blocked || Boolean(busy),
                onPress: requestForce,
              },
            ],
          },
        ]
      : []),
  ];

  return (
    <SettingsPage
      title="Request deletion"
      sections={sections}
      loading={loading}
      primaryAction={
        request
          ? undefined
          : {
              title: "Request",
              onPress: requestDeferred,
              busy: busy === "deferred",
              disabled: Boolean(busy),
              testID: "request-deletion",
            }
      }
      testID="settings-deletion"
    />
  );
}
