import { Directory, File } from "expo-file-system";
import * as ImageManipulator from "expo-image-manipulator";
import type { AppInstaller, InstalledApp } from "../apps/appInstaller";

/**
 * Edge of a widget icon in pixels: sharp at home screen icon size, and
 * small enough for a widget's memory (about 30 MB on iOS; app icons are
 * usually 1024 px).
 */
export const WIDGET_ICON_PIXELS = 256;

/**
 * Small copies of the apps' icons in `directory`, a place the widget can
 * read (iOS: the App Group container). A copy is made once per app version;
 * files of other apps and versions are removed. Maps app ids to file URLs.
 */
export async function prepareWidgetIcons(
  apps: InstalledApp[],
  installer: AppInstaller,
  directory: Directory,
): Promise<Record<string, string>> {
  if (!directory.exists)
    directory.create({ intermediates: true, idempotent: true });
  const icons: Record<string, string> = {};
  const keep = new Set<string>();
  for (const app of apps) {
    const source = installer.iconUri(app);
    if (!source) continue;
    const name = `${app.appId}-${app.versionId}.png`;
    const target = new File(directory, name);
    keep.add(name);
    if (target.exists || (await shrink(source, target)))
      icons[app.appId] = target.uri;
  }
  for (const entry of directory.list())
    if (entry instanceof File && !keep.has(entry.name)) entry.delete();
  return icons;
}

async function shrink(source: string, target: File): Promise<boolean> {
  try {
    const context = ImageManipulator.ImageManipulator.manipulate(source);
    context.resize({ width: WIDGET_ICON_PIXELS });
    const image = await context.renderAsync();
    const result = await image.saveAsync({
      format: ImageManipulator.SaveFormat.PNG,
    });
    new File(result.uri).moveSync(target, { overwrite: true });
    return true;
  } catch {
    // The tile shows the generic app symbol instead.
    return false;
  }
}
