import type { ImageSourcePropType } from "react-native";

/**
 * Material vector icons for the SF Symbols used by shared row models, so
 * one row description renders with native icons on both platforms.
 */
const ICONS: Record<string, ImageSourcePropType> = {
  plus: require("../../assets/native-icons/add.xml"),
  minus: require("../../assets/native-icons/remove.xml"),
  "play.fill": require("../../assets/native-icons/play.xml"),
  "stop.fill": require("../../assets/native-icons/stop.xml"),
  trash: require("../../assets/native-icons/delete.xml"),
  pencil: require("../../assets/native-icons/edit.xml"),
  "bubble.left": require("../../assets/native-icons/chat.xml"),
  "bubble.left.fill": require("../../assets/native-icons/chat.xml"),
  camera: require("../../assets/native-icons/camera.xml"),
  "arrow.counterclockwise": require("../../assets/native-icons/restore.xml"),
  "arrow.triangle.2.circlepath": require("../../assets/native-icons/update.xml"),
  magnifyingglass: require("../../assets/native-icons/search.xml"),
  "info.circle": require("../../assets/native-icons/info.xml"),
  checkmark: require("../../assets/native-icons/check.xml"),
  "exclamationmark.triangle.fill": require("../../assets/native-icons/warning.xml"),
  "server.rack": require("../../assets/native-icons/workspace.xml"),
  ellipsis: require("../../assets/native-icons/more-vert.xml"),
};

/** The Material icon for an SF Symbol name, if one is bundled. */
export function androidIcon(
  symbol: string | undefined,
): ImageSourcePropType | null {
  return symbol ? (ICONS[symbol] ?? null) : null;
}

/** Icon for workspace tiles when a row names no bundled symbol. */
export const WORKSPACE_ICON: ImageSourcePropType = ICONS["server.rack"]!;
