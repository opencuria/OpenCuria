import React from "react";
import { StyleSheet, Text, View } from "react-native";
import { Image } from "expo-image";
import { LayoutGrid } from "lucide-react-native";
import type { PublishedAppReference } from "@opencuria/chat-core/publishedApps";
import { Button } from "../ui/primitives";
import { tileColor } from "../ui/tileColors";
import { useTheme } from "../ui/theme";
import { appSubtitle } from "./appPresentation";
import { runPrimaryAction, type AppsState } from "./useApps";

const ACTIONS = {
  available: "Install",
  update: "Update",
  installed: "Open",
} as const;

/**
 * An app the agent sent to the phone, in the transcript: its name and
 * version with the one action that fits (install, update or open).
 */
export function PublishedAppCard({
  reference,
  apps,
}: {
  reference: PublishedAppReference;
  apps: AppsState;
}): React.JSX.Element {
  const { theme } = useTheme();
  const row = apps.rows.find((item) => item.appId === reference.appId);
  const progress = apps.progress[reference.appId];
  const icon = apps.icons[reference.appId];
  const action =
    row && apps.supported && row.state in ACTIONS
      ? ACTIONS[row.state as keyof typeof ACTIONS]
      : null;
  const subtitle =
    progress !== undefined
      ? `Downloading… ${Math.round(progress * 100)}%`
      : row
        ? appSubtitle(row, apps.platform)
        : apps.loaded
          ? "No longer available"
          : reference.versionName
            ? `Version ${reference.versionName}`
            : "";
  return (
    <View
      testID={`published-app-${reference.appId}`}
      style={[
        styles.card,
        { borderColor: theme.border, backgroundColor: theme.surfaceMuted },
      ]}
    >
      <View
        style={[styles.tile, { backgroundColor: tileColor("indigo", theme) }]}
      >
        {icon ? (
          <Image
            source={{ uri: icon }}
            style={styles.icon}
            accessibilityIgnoresInvertColors
          />
        ) : (
          <LayoutGrid size={22} color="#FFFFFF" />
        )}
      </View>
      <View style={styles.text}>
        <Text numberOfLines={1} style={[styles.name, { color: theme.text }]}>
          {row?.name ?? reference.name}
        </Text>
        {subtitle ? (
          <Text
            numberOfLines={2}
            style={[styles.subtitle, { color: theme.mutedText }]}
          >
            {subtitle}
          </Text>
        ) : null}
      </View>
      {action && row ? (
        <Button
          title={action}
          variant={action === "Open" ? "secondary" : "primary"}
          compact
          shape="capsule"
          busy={progress !== undefined}
          // Sized to its label beside the name (see hugsContent).
          style={{ alignSelf: "center" }}
          onPress={() => void runPrimaryAction(apps, row)}
          accessibilityLabel={`${action} ${row.name}`}
          testID={`published-app-action-${reference.appId}`}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 18,
    padding: 12,
  },
  tile: {
    width: 44,
    height: 44,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  icon: { width: 44, height: 44 },
  text: { flex: 1, gap: 2 },
  name: { fontSize: 15, fontWeight: "600" },
  subtitle: { fontSize: 13, lineHeight: 18 },
});
