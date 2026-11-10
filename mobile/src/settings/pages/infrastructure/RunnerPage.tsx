import React, { useState } from "react";
import { formatStorageBytes } from "@opencuria/chat-core/workspaceDisk";
import type {
  RunnerActivity,
  RunnerStorage,
  RunnerStorageGeneration,
} from "@opencuria/client";
import type {
  NativeFormRow,
  NativeFormSection,
} from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { count, formatRelative } from "../../format";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";
import { deletionRoute } from "../DeletionPage";
import { RUNNERS } from "./ImageDefinitionsPage";
import {
  ACTIVITY_GROUPS,
  percent,
  runnerConnection,
  usageTone,
} from "./runners";

const HISTORY_LIMIT = 10;
const DELETING = new Set(["pending_deletion", "deleting", "deleted"]);

/** "node-dev · v3" for a generation. */
function generationTitle(generation: RunnerStorageGeneration): string {
  const name = generation.line_name || generation.name;
  return generation.generation ? `${name} · v${generation.generation}` : name;
}

/** Whether the latest complete inventory arrived after a scan was requested. */
export function scanFinished(
  storage: RunnerStorage | undefined,
  requestedAt: string,
): boolean {
  if (!storage?.latest_complete) return false;
  return storage.runtimes.every(
    (runtime) =>
      runtime.fresh &&
      runtime.received_at !== null &&
      runtime.received_at > requestedAt,
  );
}

/**
 * One runner (admins): connection and load, QEMU limits, the storage
 * inventory with its images, and activities that need attention.
 */
export function RunnerPage({
  runnerId,
}: {
  runnerId: string;
}): React.JSX.Element {
  const { client, resources, workspaces, onOpenWorkspace } = useSettings();
  const navigation = useSettingsNavigation();
  const runners = useResource(resources, RUNNERS, () => client.runners.list());
  const metrics = useResource(resources, `runner-metric:${runnerId}`, () =>
    client.runners.latestMetrics(runnerId),
  );
  const storage = useResource(resources, `runner-storage:${runnerId}`, () =>
    client.runners.storage(runnerId),
  );
  const [scan, setScan] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  usePolling(storage.reload, 10000);
  usePolling(metrics.reload, 60000);
  const runner = runners.data?.find((entry) => entry.id === runnerId);
  const scanning = scan !== null && !scanFinished(storage.data, scan);

  if (!runner)
    return (
      <SettingsPage
        title="Runner"
        sections={[]}
        loading={!runners.data && !runners.error}
        empty={{
          title: "Runner unavailable",
          description: runners.error ?? "This runner no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-runner"
      />
    );

  const run = async (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => {
    setBusy(key);
    try {
      await action();
      await storage.reload();
      showToast({ title: success, tone: "success" });
    } catch (cause) {
      showToast({
        title: "The action failed",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const refresh = async () => {
    setBusy("scan");
    try {
      const { requested_at } = await client.runners.refreshStorage(runner.id);
      setScan(requested_at);
    } catch (cause) {
      showToast({
        title: "Inventory refresh refused",
        message: errorMessage(cause, "The runner must be online."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const sample = metrics.data;
  const metricRows: NativeFormRow[] = sample
    ? [
        {
          kind: "gauge",
          id: "cpu",
          label: "CPU",
          value: sample.cpu_usage_percent / 100,
          valueLabel: `${Math.round(sample.cpu_usage_percent)}%`,
          tone: usageTone(sample.cpu_usage_percent),
        },
        {
          kind: "gauge",
          id: "ram",
          label: "Memory",
          value: sample.ram_total_bytes
            ? sample.ram_used_bytes / sample.ram_total_bytes
            : 0,
          valueLabel: `${formatStorageBytes(sample.ram_used_bytes)} of ${formatStorageBytes(sample.ram_total_bytes)}`,
          tone: usageTone(
            percent(sample.ram_used_bytes, sample.ram_total_bytes),
          ),
        },
        {
          kind: "gauge",
          id: "disk",
          label: "Storage",
          value: sample.disk_total_bytes
            ? sample.disk_used_bytes / sample.disk_total_bytes
            : 0,
          valueLabel: `${formatStorageBytes(sample.disk_used_bytes)} of ${formatStorageBytes(sample.disk_total_bytes)}`,
          tone: usageTone(
            percent(sample.disk_used_bytes, sample.disk_total_bytes),
          ),
        },
      ]
    : [];

  const activityRows = (activity: RunnerActivity): NativeFormRow[] => {
    const deletion = activity.kind === "deletion";
    return [
      {
        kind: "item",
        id: `activity-${activity.id}`,
        title: `${activity.target_name} · ${activity.title}`,
        subtitle: [activity.summary, formatRelative(activity.created_at)]
          .filter(Boolean)
          .join(" · "),
        subtitleTone: activity.state === "attention" ? "warning" : "muted",
        busy: activity.state === "active",
        actions: [
          ...(deletion && activity.state !== "history"
            ? [
                {
                  id: "cancel",
                  title: "Cancel request",
                  symbol: "xmark.circle.fill",
                },
              ]
            : []),
          ...activity.operation_ids.map((operationId, index) => ({
            id: `operation:${operationId}`,
            title:
              activity.operation_ids.length > 1
                ? `Check operation ${index + 1}`
                : "Check status",
            symbol: "info.circle",
          })),
        ],
        onAction: (id) => {
          if (id === "cancel")
            void run(
              `cancel:${activity.id}`,
              () => client.deletions.cancel(activity.id),
              "Deletion request cancelled",
            );
          else if (id.startsWith("operation:"))
            navigation.push(
              route("runner-operation", id.slice("operation:".length)),
            );
        },
      },
    ];
  };

  const activities = storage.data?.activities ?? [];
  const activitySections: NativeFormSection[] = ACTIVITY_GROUPS.map(
    ({ state, title }) => {
      const entries = activities.filter((activity) => activity.state === state);
      return {
        id: `activities-${state}`,
        title: `${title} · ${entries.length}`,
        footer:
          state === "history" && entries.length > HISTORY_LIMIT
            ? `Showing the latest ${HISTORY_LIMIT} entries.`
            : undefined,
        rows: (state === "history"
          ? entries.slice(0, HISTORY_LIMIT)
          : entries
        ).flatMap(activityRows),
      };
    },
  ).filter((section) => section.rows.length);

  const generationRow = (
    generation: RunnerStorageGeneration,
  ): NativeFormRow => {
    const deleting = DELETING.has(generation.status);
    const dependents = generation.dependencies;
    return {
      kind: "item",
      id: `generation-${generation.id}`,
      title: generationTitle(generation),
      subtitle: [
        generation.captured_image_id
          ? generation.owner_label || "Capture"
          : generation.definition_name || "Legacy definition",
        generation.is_current
          ? "Current default"
          : generation.is_pending
            ? "Pending"
            : "",
        formatStorageBytes(generation.size_bytes),
        count(dependents.length, "dependent"),
        deleting ? generation.status.replaceAll("_", " ") : "",
      ]
        .filter(Boolean)
        .join(" · "),
      actions: [
        ...(generation.is_current && generation.definition_id && !deleting
          ? [{ id: "rebuild", title: "Build new generation", symbol: "hammer" }]
          : []),
        // Only your own workspaces can be opened; their names come from your list.
        ...workspaces
          .filter((workspace) =>
            dependents.some((dependent) => dependent.id === workspace.id),
          )
          .map((workspace) => ({
            id: `open:${workspace.id}`,
            title: `Open ${workspace.name}`,
            symbol: "bubble.left",
          })),
        ...(!deleting
          ? [
              {
                id: "delete",
                title: "Delete this generation",
                symbol: "trash",
                destructive: true,
              },
            ]
          : []),
        ...(generation.build_job_id && generation.is_current && !deleting
          ? [
              {
                id: "delete-assignment",
                title: "Delete runner assignment",
                symbol: "trash",
                destructive: true,
              },
            ]
          : []),
      ],
      onAction: (id) => {
        if (id === "rebuild")
          void run(
            `rebuild:${generation.id}`,
            () =>
              client.images.updateRunnerBuild(
                generation.definition_id!,
                runner.id,
                {
                  action: "rebuild",
                },
              ),
            "Build started",
          );
        else if (id === "delete")
          navigation.push(
            deletionRoute(
              { target_type: "image", target_id: generation.id },
              generationTitle(generation),
            ),
          );
        else if (id === "delete-assignment")
          navigation.push(
            deletionRoute(
              {
                target_type: "assignment",
                target_id: generation.build_job_id!,
              },
              `${generation.definition_name ?? generation.name} on ${runner.name}`,
            ),
          );
        else if (id.startsWith("open:")) {
          const workspace = workspaces.find(
            (entry) => entry.id === id.slice(5),
          );
          if (workspace) onOpenWorkspace(workspace);
        }
      },
    };
  };

  const generations = storage.data?.generations ?? [];
  const groups: Array<[string, RunnerStorageGeneration[]]> = [
    [
      "Base and build images",
      generations.filter((entry) => !entry.captured_image_id),
    ],
    ["Captured images", generations.filter((entry) => entry.captured_image_id)],
  ];
  const filesystems =
    storage.data?.runtimes.flatMap((runtime) => runtime.filesystems) ?? [];
  const collected = storage.data?.runtimes
    .map((runtime) => runtime.collected_at)
    .filter((value): value is string => Boolean(value))
    .sort()
    .at(-1);

  const sections: NativeFormSection[] = [
    {
      id: "runner",
      rows: [
        {
          kind: "value",
          id: "connection",
          label: "Connection",
          value: runnerConnection(runner),
          tone: runner.status === "online" ? "success" : "muted",
        },
        {
          kind: "link",
          id: "limits",
          title: "QEMU limits",
          symbol: "slider.horizontal.3",
          route: route("runner-limits", runner.id),
        },
      ],
    },
    ...(metricRows.length
      ? [
          {
            id: "resources",
            title: "Resources",
            footer: sample
              ? `Host sample · ${formatRelative(sample.timestamp)}`
              : undefined,
            rows: metricRows,
          },
        ]
      : []),
    {
      id: "inventory",
      title: "Inventory",
      footer: storage.error
        ? storage.error
        : scanning
          ? "Scan queued · waiting for a new complete inventory."
          : storage.data
            ? `${storage.data.latest_complete ? "Complete" : "Partial or unavailable"} inventory${collected ? ` · collected ${formatRelative(collected).toLowerCase()}` : ""}. Image sizes are not filesystem usage.`
            : undefined,
      footerTone: storage.error ? "danger" : "muted",
      rows: [
        ...filesystems.map(
          (filesystem, index): NativeFormRow => ({
            kind: "gauge",
            id: `filesystem-${index}`,
            label: filesystem.path,
            value:
              filesystem.capacity_bytes && filesystem.used_bytes !== null
                ? filesystem.used_bytes / filesystem.capacity_bytes
                : 0,
            valueLabel: `${formatStorageBytes(filesystem.used_bytes)} of ${formatStorageBytes(filesystem.capacity_bytes)}`,
            tone: usageTone(
              filesystem.capacity_bytes && filesystem.used_bytes !== null
                ? percent(filesystem.used_bytes, filesystem.capacity_bytes)
                : 0,
            ),
          }),
        ),
        {
          kind: "button",
          id: "refresh-inventory",
          title: scanning ? "Scan queued" : "Refresh inventory",
          symbol: "arrow.clockwise",
          busy: busy === "scan" || scanning,
          disabled: Boolean(busy) || scanning || runner.status !== "online",
          onPress: () => void refresh(),
        },
      ],
    },
    ...activitySections,
    ...groups
      .filter(([, entries]) => entries.length)
      .map(([title, entries]) => ({
        id: `images-${title}`,
        title,
        footer: "Long-press an image for its actions.",
        rows: entries.map(generationRow),
      })),
  ];

  return (
    <SettingsPage
      title={runner.name || runner.id.slice(0, 8)}
      sections={sections}
      onRefresh={async () => {
        await Promise.allSettled([
          storage.reload(),
          metrics.reload(),
          runners.reload(),
        ]);
      }}
      testID="settings-runner"
    />
  );
}
