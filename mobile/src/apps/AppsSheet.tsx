import React, { useEffect } from "react";
import { Alert } from "react-native";
import { NativeForm } from "../ui/NativeForm";
import type {
  NativeFormItemRow,
  NativeFormSection,
  NativeFormSwipeAction,
} from "../ui/NativeForm.types";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { NativeModal } from "../ui/primitives";
import { tileColor, type TileColor } from "../ui/tileColors";
import { useTheme } from "../ui/theme";
import {
  appSections,
  appSubtitle,
  type AppRow,
  type AppState,
} from "./appPresentation";
import { runPrimaryAction, type AppsState } from "./useApps";

export interface AppsSheetProps {
  visible: boolean;
  onClose: () => void;
  apps: AppsState;
}

const TILES: Record<AppState, TileColor> = {
  installed: "indigo",
  update: "orange",
  available: "green",
  incompatible: "gray",
  unsupported: "gray",
};

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
  const { theme } = useTheme();
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
    const runnable =
      apps.supported &&
      (row.state === "installed" ||
        row.state === "update" ||
        row.state === "available");
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
      subtitle: busy
        ? `Downloading… ${Math.round(progress * 100)}%`
        : appSubtitle(row, apps.platform),
      subtitleTone: row.state === "update" ? "accent" : undefined,
      symbol: "square.grid.2x2.fill",
      tint: tileColor(TILES[row.state], theme),
      busy,
      badge:
        row.state === "update"
          ? "Update"
          : row.state === "available"
            ? "Install"
            : undefined,
      disabled: busy,
      onPress: runnable ? () => void runPrimaryAction(apps, row) : undefined,
      actions: busy ? [] : actions,
      trailingSwipe: busy ? [] : swipe,
      onAction: (id) => act(row, id),
    };
  };

  const { installed, available } = appSections(apps.rows);
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
        },
      ],
    });
  if (apps.error)
    notes.push({
      id: "error",
      rows: [
        installed.length
          ? {
              kind: "note",
              id: "apps-error",
              text: "Can’t reach OpenCuria right now. Installed apps still open; new apps and updates show up once you are back online.",
            }
          : {
              kind: "note",
              id: "apps-error",
              text: `Could not load apps: ${apps.error}`,
              tone: "danger",
            },
        {
          kind: "button",
          id: "apps-retry",
          title: "Retry",
          symbol: "arrow.counterclockwise",
          busy: apps.loading,
          onPress: () => void apps.refresh(),
        },
      ],
    });
  const sections: NativeFormSection[] = [
    ...notes,
    ...(installed.length
      ? [{ id: "installed", title: "Installed", rows: installed.map(item) }]
      : []),
    ...(available.length
      ? [{ id: "available", title: "Ready to install", rows: available.map(item) }]
      : []),
  ];
  const waiting = !apps.loaded && !apps.error && !installed.length;

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Apps"
      scrollable={false}
      testID="apps-sheet"
    >
      <NativeForm
        sections={
          waiting
            ? [
                {
                  id: "loading",
                  rows: [
                    { kind: "note", id: "apps-loading", text: "Loading apps…" },
                  ],
                },
              ]
            : sections
        }
        onRefresh={apps.refresh}
        empty={{
          title: "No apps yet",
          description:
            "Ask an agent to build an Expo app and send it to you. It shows up here to install.",
          symbol: "square.grid.2x2.fill",
        }}
        testID="app-list"
      />
    </NativeModal>
  );
}
