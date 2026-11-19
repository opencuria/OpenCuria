import React, { useState } from "react";
import {
  ActivityIndicator,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { Image } from "expo-image";
import { ArrowDown, Play } from "lucide-react-native";
import { formatAttachmentSize } from "@opencuria/chat-core/harnessAttachments";
import {
  FILE_TRANSFER_MAX_BYTES,
  MEBIBYTE,
} from "@opencuria/chat-core/workspaceFiles";
import { NativeAction } from "../ui/controls";
import { useTheme } from "../ui/theme";
import type { MediaPlaceholderState } from "./useWorkspaceMedia";

const TOO_LARGE = `Too large to show (max ${FILE_TRANSFER_MAX_BYTES / MEBIBYTE} MB)`;
const SCRIM = "rgba(0, 0, 0, 0.55)";

/**
 * A workspace image or video that is not loaded, as in messaging apps: its
 * blurred thumbnail (a neutral box without one) with the file size and a
 * download or play button, a spinner once tapped, or why it cannot load.
 */
export function MediaPlaceholder({
  state: { status: mode, preview },
  kind,
  label,
  fit = "contain",
  compact = false,
  onLoad,
  style,
}: {
  /** Waiting for a tap, loading after one, or too large to load. */
  state: MediaPlaceholderState;
  kind: "image" | "video";
  label: string;
  /** How the thumbnail fills the frame, as the loaded media will. */
  fit?: "contain" | "cover";
  /** Small frames such as prompt thumbnails. */
  compact?: boolean;
  /** Makes a waiting placeholder a button that loads the file. */
  onLoad?: () => void;
  /** The frame of the loaded media, so rows keep their size. */
  style: StyleProp<ViewStyle>;
}): React.JSX.Element {
  const { theme } = useTheme();
  // The URL that showed or failed; a renewed link loads again.
  const [shown, setShown] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const thumbnail =
    preview.thumbnailUrl && failed !== preview.thumbnailUrl
      ? preview.thumbnailUrl
      : "";
  const size = formatAttachmentSize(preview.size);
  const icon = compact ? 14 : 20;
  const frame = [
    styles.frame,
    // The thumbnail's own proportions show once it loaded.
    !thumbnail || shown !== thumbnail
      ? { backgroundColor: theme.surfaceMuted }
      : null,
    style,
  ];
  const content = (
    <>
      {thumbnail ? (
        <Image
          source={{ uri: thumbnail }}
          blurRadius={compact ? 4 : 12}
          cachePolicy="memory"
          contentFit={fit}
          style={StyleSheet.absoluteFill}
          onLoad={() => setShown(thumbnail)}
          onError={() => setFailed(thumbnail)}
        />
      ) : null}
      {mode === "too_large" ? (
        <Text
          numberOfLines={3}
          style={[styles.pill, compact && styles.compactText]}
        >
          {TOO_LARGE}
        </Text>
      ) : (
        <>
          <View style={[styles.button, compact && styles.compactButton]}>
            {mode === "loading" ? (
              <ActivityIndicator color="#FFFFFF" />
            ) : kind === "video" ? (
              <Play size={icon} color="#FFFFFF" fill="#FFFFFF" />
            ) : (
              <ArrowDown size={icon} color="#FFFFFF" />
            )}
          </View>
          {size && !compact ? <Text style={styles.pill}>{size}</Text> : null}
        </>
      )}
    </>
  );
  if (mode === "tap" && onLoad)
    return (
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${kind === "video" ? "Play video" : "Load image"} ${label}${size ? `, ${size}` : ""}`}
        onPress={onLoad}
        style={frame}
      >
        {content}
      </NativeAction>
    );
  return (
    <View
      accessibilityLabel={
        mode === "loading"
          ? `Loading ${label}`
          : mode === "too_large"
            ? `${label}: ${TOO_LARGE}`
            : `${label}${size ? `, ${size}` : ""}`
      }
      style={frame}
    >
      {content}
    </View>
  );
}

const styles = StyleSheet.create({
  frame: {
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    overflow: "hidden",
  },
  button: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: SCRIM,
  },
  compactButton: { width: 30, height: 30, borderRadius: 15 },
  pill: {
    color: "#FFFFFF",
    backgroundColor: SCRIM,
    fontSize: 12,
    fontWeight: "500",
    textAlign: "center",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
    overflow: "hidden",
    marginHorizontal: 8,
  },
  compactText: { fontSize: 10, paddingHorizontal: 4, marginHorizontal: 4 },
});
