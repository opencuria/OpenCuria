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
  "chevron.right": require("../../assets/native-icons/chevron-right.xml"),
  xmark: require("../../assets/native-icons/close.xml"),
  "calendar.badge.clock": require("../../assets/native-icons/schedule.xml"),
  clock: require("../../assets/native-icons/schedule.xml"),
  "pause.fill": require("../../assets/native-icons/pause.xml"),
  "play.circle": require("../../assets/native-icons/play-circle.xml"),
  "checkmark.circle.fill": require("../../assets/native-icons/check-circle.xml"),
  "xmark.circle.fill": require("../../assets/native-icons/cancel.xml"),
  "minus.circle.fill": require("../../assets/native-icons/remove-circle.xml"),
  // Apps agents publish.
  "square.grid.2x2.fill": require("../../assets/native-icons/apps.xml"),
  "arrow.down.circle": require("../../assets/native-icons/download.xml"),
  // Settings (Material Symbols, rounded).
  cpu: require("../../assets/native-icons/memory.xml"),
  sparkles: require("../../assets/native-icons/auto-awesome.xml"),
  "book.closed": require("../../assets/native-icons/menu-book.xml"),
  "puzzlepiece.extension": require("../../assets/native-icons/extension.xml"),
  key: require("../../assets/native-icons/key.xml"),
  "key.horizontal": require("../../assets/native-icons/vpn-key.xml"),
  "slider.horizontal.3": require("../../assets/native-icons/tune.xml"),
  "square.stack.3d.up": require("../../assets/native-icons/stacks.xml"),
  "checkmark.shield": require("../../assets/native-icons/verified-user.xml"),
  "circle.lefthalf.filled": require("../../assets/native-icons/contrast.xml"),
  bell: require("../../assets/native-icons/notifications.xml"),
  envelope: require("../../assets/native-icons/mail.xml"),
  "building.2": require("../../assets/native-icons/corporate-fare.xml"),
  globe: require("../../assets/native-icons/language.xml"),
  person: require("../../assets/native-icons/person.xml"),
  "person.badge.key": require("../../assets/native-icons/badge.xml"),
  "rectangle.portrait.and.arrow.right": require("../../assets/native-icons/logout.xml"),
  "doc.on.doc": require("../../assets/native-icons/content-copy.xml"),
  "square.and.arrow.up": require("../../assets/native-icons/share.xml"),
  "arrow.clockwise": require("../../assets/native-icons/refresh.xml"),
  "arrow.up.right.square": require("../../assets/native-icons/open-in-new.xml"),
  lock: require("../../assets/native-icons/lock.xml"),
  "doc.text": require("../../assets/native-icons/description.xml"),
  hammer: require("../../assets/native-icons/build.xml"),
  "plus.square.on.square": require("../../assets/native-icons/library-add.xml"),
  power: require("../../assets/native-icons/power.xml"),
  gearshape: require("../../assets/native-icons/settings.xml"),
  "square.and.pencil": require("../../assets/native-icons/edit-square.xml"),
  "clock.arrow.circlepath": require("../../assets/native-icons/history.xml"),
  eye: require("../../assets/native-icons/visibility.xml"),
  powerplug: require("../../assets/native-icons/link-off.xml"),
  "arrow.triangle.2.circlepath.circle": require("../../assets/native-icons/sync.xml"),
  mic: require("../../assets/native-icons/mic.xml"),
  desktopcomputer: require("../../assets/native-icons/computer.xml"),
  "point.3.connected.trianglepath.dotted": require("../../assets/native-icons/account-tree.xml"),
  shippingbox: require("../../assets/native-icons/deployed-code.xml"),
  archivebox: require("../../assets/native-icons/inventory.xml"),
};

/** The Material icon for an SF Symbol name, if one is bundled. */
export function androidIcon(
  symbol: string | undefined,
): ImageSourcePropType | null {
  return symbol ? (ICONS[symbol] ?? null) : null;
}

/** Icon for workspace tiles when a row names no bundled symbol. */
export const WORKSPACE_ICON: ImageSourcePropType = ICONS["server.rack"]!;
