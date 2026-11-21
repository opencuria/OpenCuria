import React, { useEffect } from "react";
import { Alert } from "react-native";
import { errorRows, loadErrorState } from "../ui/formStates";
import type {
  NativeFormEmptyState,
  NativeFormItemRow,
  NativeFormSection,
  NativeFormSwipeAction,
} from "../ui/NativeForm.types";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { NativeModal } from "../ui/primitives";
import {
  appActionTitle,
  appSections,
  appSubtitle,
  type AppRow,
} from "./appPresentation";
import { runPrimaryAction, type AppsState } from "./useApps";

export interface AppsSheetProps {
  visible: boolean;
  onClose: () => void;
  apps: AppsState;
}

function confirm(
  title: string,
  message: string,
  action: string,
  onConfirm: () => void,
): void {
  Alert.alert(title, message, [
    { text: "Cancel", style: "cancel" },
    { text: action, style: "destructive", onPress: onConfirm },
  ]);
}

/**
 * Apps agents built and sent to the user. Installed apps open inside
 * OpenCuria and keep working offline; newer versions show as updates.
 */
export function AppsSheet({
  visible,
  onClose,
  apps,
}: AppsSheetProps): React.JSX.Element | null {
  const { refresh } = apps;
  // Opening the list checks for apps and updates published since.
  useEffect(() => {
    if (visible) void refresh();
  }, [visible, refresh]);
  if (!visible) return null;

  const act = (row: AppRow, id: string) => {
    if (id === "open") void apps.open(row.appId);
    else if (id === "install" || id === "update") void apps.install(row.appId);
    else if (id === "uninstall")
      confirm(
        `Remove ${row.name}?`,
        "The app and its data leave this device.",
        "Remove",
        () => void apps.uninstall(row.appId),
      );
    else if (id === "delete")
      confirm(
        `Delete ${row.name}?`,
        row.installed
          ? "Deletes the app from OpenCuria. The copy on this device stays until you remove it."
          : "Deletes the app from OpenCuria.",
        "Delete",
        () => void apps.remove(row.appId),
      );
  };

  const item = (row: AppRow): NativeFormItemRow => {
    const progress = apps.progress[row.appId];
    const busy = progress !== undefined;
    const action = apps.supported ? appActionTitle(row.state) : null;
    const run = () => void runPrimaryAction(apps, row);
    const actions: NativeMenuAction[] = [];
    const swipe: NativeFormSwipeAction[] = [];
    if (row.state === "update")
      actions.push({
        id: "update",
        title: "Update",
        symbol: "arrow.triangle.2.circlepath",
      });
    if (row.installed)
      actions.push({ id: "open", title: "Open", symbol: "play.fill" });
    if (row.state === "available")
      actions.push({
        id: "install",
        title: "Install",
        symbol: "arrow.down.circle",
      });
    if (row.installed) {
      const uninstall = {
        id: "uninstall",
        title: "Remove from device",
        symbol: "minus.circle.fill",
        destructive: true,
      } as const;
      actions.push(uninstall);
      swipe.push(uninstall);
    }
    if (row.published) {
      const remove = {
        id: "delete",
        title: "Delete app",
        symbol: "trash",
        destructive: true,
      } as const;
      actions.push(remove);
      if (!row.installed) swipe.push(remove);
    }
    return {
      kind: "item",
      id: row.appId,
      testID: `app-${row.appId}`,
      title: row.name,
      subtitle: appSubtitle(row, apps.platform, progress),
      symbol: "square.grid.2x2.fill",
      tile: true,
      image: apps.icons[row.appId],
      // The capsule shows the download's spinner in its place.
      busy: busy && !action,
      accessory: action
        ? {
            title: action,
            prominent: action !== "Open",
            busy,
            onPress: run,
            accessibilityLabel: `${action} ${row.name}`,
            testID: `app-action-${row.appId}`,
          }
        : undefined,
      disabled: busy,
      onPress: action ? run : undefined,
      actions: busy ? [] : actions,
      trailingSwipe: busy ? [] : swipe,
      onAction: (id) => act(row, id),
    };
  };

  const { installed, available } = appSections(apps.rows);
  const hasRows = installed.length > 0 || available.length > 0;
  const retry = () => void apps.refresh();
  const notes: NativeFormSection[] = [];
  if (!apps.supported)
    notes.push({
      id: "unsupported",
      rows: [
        {
          kind: "note",
          id: "apps-unsupported",
          text: "This build of OpenCuria cannot open apps.",
          tone: "warning",
          symbol: "exclamationmark.triangle",
        },
      ],
    });
  if (apps.offline)
    notes.push({
      id: "offline",
      rows: [
        {
          kind: "note",
          id: "apps-offline",
          text: installed.length
            ? "You’re offline. Installed apps still open; new apps and updates show up once you are back online."
            : "You’re offline. New apps show up here once you are back online.",
          tone: "muted",
          symbol: "wifi.slash",
        },
      ],
    });
  // Without anything to list, a failed load is the centered empty state.
  else if (apps.error && (hasRows || notes.length))
    notes.push({
      id: "error",
      // With installed apps the failure reassures (muted, like offline).
      rows: installed.length
        ? [
            {
              kind: "note",
              id: "apps-error",
              text: "Can’t reach OpenCuria right now. Installed apps still open; new apps and updates show up once you are back online.",
              tone: "muted",
              symbol: "wifi.slash",
            },
            {
              kind: "button",
              id: "apps-retry",
              title: "Retry",
              symbol: "arrow.clockwise",
              busy: apps.loading,
              onPress: retry,
            },
          ]
        : errorRows("apps", apps.error, retry, apps.loading),
    });
  const sections: NativeFormSection[] = [
    ...notes,
    ...(installed.length
      ? [{ id: "installed", title: "Installed", rows: installed.map(item) }]
      : []),
    ...(available.length
      ? [
          {
            id: "available",
            title: "Ready to install",
            rows: available.map(item),
          },
        ]
      : []),
  ];
  const waiting =
    !apps.offline && !apps.loaded && !apps.error && !installed.length;
  const empty: NativeFormEmptyState =
    apps.error && !apps.offline
      ? loadErrorState("apps", apps.error, retry, apps.loading)
      : {
          title: "No apps yet",
          description:
            "Ask an agent to build an Expo app and send it to you. It shows up here to install.",
          symbol: "square.grid.2x2.fill",
        };

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Apps"
      largeTitle
      testID="apps-sheet"
      form={{
        sections: waiting ? [] : sections,
        loading: waiting,
        onRefresh: apps.refresh,
        empty,
        testID: "app-list",
      }}
    />
  );
}
