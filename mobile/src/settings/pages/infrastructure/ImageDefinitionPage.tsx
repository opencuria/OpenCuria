import React, { useState } from "react";
import {
  buildNeedsPolling,
  definitionNeedsPolling,
  getDefinitionActions,
  getDefinitionStatus,
  getRunnerBuildActions,
  getRunnerBuildStatus,
  isDefinitionLocked,
  visibleImageDefinitions,
  type RunnerBuildActionId,
} from "@opencuria/chat-core/imageDefinitionLifecycle";
import type { Runner, RunnerImageBuild } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import type { NativeMenuAction } from "../../../ui/NativeMenu";
import type { NativeSymbol } from "../../../ui/controls.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { usePolling } from "../../usePolling";
import { deletionRoute } from "../DeletionPage";
import { IMAGE_DEFINITIONS, RUNNERS } from "./ImageDefinitionsPage";
import {
  canDeleteVersion,
  isVersionBusy,
  nativeTone,
  versionSubtitle,
  versionTitle,
} from "./images";

const ACTION_SYMBOLS: Record<RunnerBuildActionId, NativeSymbol> = {
  build: "hammer",
  retry: "arrow.clockwise",
  rebuild: "arrow.clockwise",
  activate: "play.fill",
  deactivate: "pause.fill",
  remove: "trash",
  retry_remove: "trash",
  view_log: "doc.text",
};

/**
 * One image definition: its recipe and, per runner, the build status,
 * lifecycle actions (admins), build log and versions.
 */
export function ImageDefinitionPage({
  definitionId,
}: {
  definitionId: string;
}): React.JSX.Element {
  const { client, resources, isAdmin } = useSettings();
  const navigation = useSettingsNavigation();
  const definitions = useResource(resources, IMAGE_DEFINITIONS, async () =>
    visibleImageDefinitions(await client.images.listDefinitions()),
  );
  const runners = useResource(resources, RUNNERS, () => client.runners.list());
  const builds = useResource(resources, `runner-builds:${definitionId}`, () =>
    client.images.listRunnerBuilds(definitionId),
  );
  const definition = definitions.data?.find(
    (entry) => entry.id === definitionId,
  );
  const [busy, setBusy] = useState<string | null>(null);
  const inFlight =
    definitionNeedsPolling(definition) ||
    (builds.data ?? []).some((build) => buildNeedsPolling(build));
  usePolling(
    async () => {
      await Promise.allSettled([builds.reload(), definitions.reload()]);
    },
    3000,
    inFlight,
  );

  if (!definition)
    return (
      <SettingsPage
        title="Image definition"
        sections={[]}
        loading={!definitions.data && !definitions.error}
        empty={{
          title: "Definition unavailable",
          description: definitions.error ?? "This definition no longer exists.",
          symbol: "exclamationmark.triangle.fill",
        }}
        testID="settings-image-definition"
      />
    );

  const locked = isDefinitionLocked(definition.status);
  const definitionStatus = getDefinitionStatus(definition);
  const actions = getDefinitionActions({
    is_standard: Boolean(definition.is_standard),
    status: definition.status,
  });

  const run = async (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => {
    setBusy(key);
    try {
      await action();
      await Promise.allSettled([builds.reload(), definitions.reload()]);
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

  const runnerAction = (
    runner: Runner,
    build: RunnerImageBuild | undefined,
    id: RunnerBuildActionId,
  ) => {
    const key = `${runner.id}:${id}`;
    switch (id) {
      case "build":
      case "retry":
        return void run(
          key,
          () =>
            client.images.createRunnerBuild(definition.id, {
              runner_id: runner.id,
              activate: true,
            }),
          "Build started",
        );
      case "activate":
      case "deactivate":
        return void run(
          key,
          () =>
            client.images.updateRunnerBuild(definition.id, runner.id, {
              action: id,
            }),
          id === "activate" ? "Image activated" : "Image deactivated",
        );
      case "rebuild":
        return navigation.push(
          route("image-definition-rebuild", definition.id, runner.id),
        );
      case "view_log":
        return navigation.push(route("build-log", definition.id, runner.id));
      case "remove":
      case "retry_remove":
        if (build)
          navigation.push(
            deletionRoute(
              { target_type: "assignment", target_id: build.id },
              `${definition.name} on ${runner.name || runner.id.slice(0, 8)}`,
            ),
          );
        return;
    }
  };

  const runnerSection = (runner: Runner): NativeFormSection => {
    const build = builds.data?.find((entry) => entry.runner_id === runner.id);
    const online = runner.status === "online";
    const status = getRunnerBuildStatus(build, online);
    const current = build?.versions?.find(
      (version) => version.id === build.current_generation_id,
    );
    const available = getRunnerBuildActions(build, {
      definitionLocked: locked,
    }).filter((action) => isAdmin || action.id === "view_log");
    return {
      id: `runner-${runner.id}`,
      title: `${runner.name || runner.id.slice(0, 8)}${online ? "" : " · Offline"}`,
      rows: [
        {
          kind: "value",
          id: `status-${runner.id}`,
          label: "Status",
          value: status.label,
          tone: nativeTone(status.tone),
        },
        ...(current
          ? [
              {
                kind: "value" as const,
                id: `latest-${runner.id}`,
                label: "Latest",
                value: `v${current.version ?? "?"}`,
              },
            ]
          : []),
        ...available.map((action) => ({
          kind: "button" as const,
          id: `${runner.id}-${action.id}`,
          testID: `${runner.id}-${action.id}`,
          title: action.label,
          symbol: ACTION_SYMBOLS[action.id],
          destructive: action.kind === "destructive",
          busy: busy === `${runner.id}:${action.id}`,
          disabled: Boolean(busy),
          onPress: () => runnerAction(runner, build, action.id),
        })),
        ...(build?.versions ?? []).map((version) => {
          const deletable = isAdmin && canDeleteVersion(version, !locked);
          const target = deletionRoute(
            { target_type: "image", target_id: version.id },
            `${definition.name} · v${version.version ?? "?"}`,
          );
          return {
            kind: "item" as const,
            id: `version-${version.id}`,
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
      ],
    };
  };

  const menu: NativeMenuAction[] = isAdmin
    ? [
        ...(actions.canDuplicate
          ? [
              {
                id: "duplicate",
                title: "Duplicate",
                symbol: "plus.square.on.square",
              },
            ]
          : []),
        ...(actions.canEdit
          ? [{ id: "edit", title: "Edit", symbol: "pencil" }]
          : []),
        ...(actions.canEnable
          ? [{ id: "enable", title: "Enable", symbol: "play.fill" }]
          : []),
        ...(actions.canRestore
          ? [
              {
                id: "restore",
                title: "Restore",
                symbol: "arrow.counterclockwise",
              },
            ]
          : []),
        ...(actions.canDelete || actions.canRetryDelete
          ? [
              {
                id: "delete",
                title: actions.canRetryDelete ? "Retry delete" : "Delete",
                symbol: "trash",
                destructive: true,
              },
            ]
          : []),
      ]
    : [];

  const onMenu = (id: string) => {
    if (id === "edit")
      navigation.push(route("image-definition-editor", definition.id));
    else if (id === "duplicate")
      void run(
        "duplicate",
        async () => {
          const copy = await client.images.duplicateDefinition(definition.id);
          navigation.push(route("image-definition", copy.id));
        },
        "Definition duplicated",
      );
    else if (id === "restore" || id === "enable")
      void run(
        id,
        () => client.images.activateDefinition(definition.id),
        id === "enable" ? "Definition enabled" : "Definition restored",
      );
    else if (id === "delete")
      navigation.push(
        deletionRoute(
          { target_type: "definition", target_id: definition.id },
          definition.name,
        ),
      );
  };

  const runnerList = runners.data ?? [];
  const sections: NativeFormSection[] = [
    {
      id: "recipe",
      footer:
        definition.delete_last_error || definition.description || undefined,
      footerTone: definition.delete_last_error ? "danger" : "muted",
      rows: [
        {
          kind: "value",
          id: "summary",
          label: "Status",
          value: definitionStatus.label,
          tone: nativeTone(definitionStatus.tone),
        },
        {
          kind: "value",
          id: "base",
          label: "Base distro",
          value: definition.base_distro || "—",
          monospace: true,
        },
        {
          kind: "value",
          id: "packages",
          label: "Packages",
          value: definition.packages?.length
            ? definition.packages.join(", ")
            : "None",
          stacked: true,
        },
      ],
    },
    ...runnerList.map(runnerSection),
    ...(runnerList.length
      ? []
      : [{ id: "runners-footer", footer: "No runners yet.", rows: [] }]),
  ];

  return (
    <SettingsPage
      title={definition.name}
      sections={sections}
      onRefresh={async () => {
        await Promise.allSettled([
          builds.reload(),
          definitions.reload(),
          runners.reload(),
        ]);
      }}
      actions={menu.length ? menu : undefined}
      onAction={onMenu}
      testID="settings-image-definition"
    />
  );
}
