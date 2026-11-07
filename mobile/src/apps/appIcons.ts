import { Directory, File, Paths } from "expo-file-system";
import * as ImageManipulator from "expo-image-manipulator";
import type { AppFileRequest } from "@opencuria/client";
import type { AppInstaller } from "./appInstaller";
import type { AppRow } from "./appPresentation";

/**
 * Edge of the icons lists show, in pixels: sharp at 44 points on 3× screens
 * and only a few KB, so native lists can read them while they draw.
 */
export const LIST_ICON_PIXELS = 144;

/** Where an app's icon comes from: its installed copy or the server. */
export type AppIconSource =
  | { appId: string; token: string; file: string }
  | { appId: string; token: string; request: () => AppFileRequest };

/**
 * The icon a row shows: the installed copy's own icon, or for an app that
 * is not installed the icon of its newest version.
 */
export function appIconSource(
  row: AppRow,
  installer: Pick<AppInstaller, "iconUri">,
  fileRequest?: (
    appId: string,
    versionId: string,
    sha256: string,
  ) => AppFileRequest,
): AppIconSource | null {
  if (row.installed) {
    const file = installer.iconUri(row.installed);
    return file
      ? { appId: row.appId, token: row.installed.versionId, file }
      : null;
  }
  const version = row.published?.latest_version;
  const sha256 = version?.icon_sha256;
  if (!version || !sha256 || !fileRequest) return null;
  return {
    appId: row.appId,
    token: sha256.slice(0, 16),
    request: () => fileRequest(row.appId, version.id, sha256),
  };
}

/** Write a copy of the image at `source` to `target`, `pixels` wide. */
export async function shrinkIcon(
  source: string,
  target: File,
  pixels: number,
): Promise<boolean> {
  try {
    const context = ImageManipulator.ImageManipulator.manipulate(source);
    context.resize({ width: pixels });
    const image = await context.renderAsync();
    const result = await image.saveAsync({
      format: ImageManipulator.SaveFormat.PNG,
    });
    new File(result.uri).moveSync(target, { overwrite: true });
    return true;
  } catch {
    // The row shows the generic app symbol instead.
    return false;
  }
}

async function downloadIcon(request: AppFileRequest, target: File) {
  await File.downloadFileAsync(request.url, target, {
    headers: request.headers,
    idempotent: true,
  });
}

/**
 * Small copies of app icons for lists, in the app's cache. Each app keeps
 * one copy, named after where it came from (`<app>.<token>.png`), so a new
 * icon gets a new name and image views never show an outdated one.
 */
export class AppIconCache {
  private readonly pending = new Map<string, Promise<string | null>>();

  constructor(
    private readonly directory: Directory = new Directory(
      Paths.cache,
      "opencuria-app-icons",
    ),
    private readonly shrink: (
      source: string,
      target: File,
    ) => Promise<boolean> = (source, target) =>
      shrinkIcon(source, target, LIST_ICON_PIXELS),
    private readonly download: (
      request: AppFileRequest,
      target: File,
    ) => Promise<void> = downloadIcon,
  ) {}

  /** `file://` URIs of the icons of these sources by app id; makes missing ones. */
  async icons(sources: AppIconSource[]): Promise<Record<string, string>> {
    const icons: Record<string, string> = {};
    // One at a time: icons are small, and lists rarely hold many apps.
    for (const source of sources) {
      const uri = await this.icon(source);
      if (uri) icons[source.appId] = uri;
    }
    return icons;
  }

  private icon(source: AppIconSource): Promise<string | null> {
    const name = `${source.appId}.${source.token}.png`;
    const target = new File(this.directory, name);
    if (target.exists) return Promise.resolve(target.uri);
    let flight = this.pending.get(name);
    if (!flight) {
      flight = this.make(source, target, name).finally(() =>
        this.pending.delete(name),
      );
      this.pending.set(name, flight);
    }
    return flight;
  }

  private async make(
    source: AppIconSource,
    target: File,
    name: string,
  ): Promise<string | null> {
    if (!this.directory.exists)
      this.directory.create({ intermediates: true, idempotent: true });
    let original = "file" in source ? source.file : null;
    const download = new File(this.directory, `${name}.download`);
    try {
      if (!original) {
        if (!("request" in source)) return null;
        await this.download(source.request(), download);
        original = download.uri;
      }
      if (!(await this.shrink(original, target))) return null;
    } catch {
      return null;
    } finally {
      if (download.exists) download.delete();
    }
    // The app's previous icon is no longer shown anywhere.
    for (const entry of this.directory.list())
      if (
        entry instanceof File &&
        entry.name !== name &&
        entry.name.startsWith(`${source.appId}.`)
      )
        entry.delete();
    return target.uri;
  }
}
