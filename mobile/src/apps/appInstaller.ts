import type { AppFileRequest, AppManifest } from "@opencuria/client";
import type { AppLaunchRequest } from "./appRuntime";
import { filePath, joinUri, type AppFileSystem } from "./appStorage";

/**
 * An app installed on this device. Installed apps stay usable offline; the
 * registry keeps one entry per app with the version that is installed.
 */
export interface InstalledApp {
  appId: string;
  /** Server, organization and user the app was published to. */
  scope: string;
  name: string;
  slug: string;
  versionId: string;
  versionNumber: number;
  versionName: string;
  sdkVersion: string;
  /** When the installed version was published. */
  publishedAt: string;
  installedAt: string;
  hasIcon: boolean;
}

/** Progress of an install, in bytes of files still to download. */
export interface InstallProgress {
  done: number;
  total: number;
}

export interface InstallOptions {
  scope: string;
  /** Authenticated request for a file of this version. */
  fileRequest: (sha256: string) => AppFileRequest;
  onProgress?: (progress: InstallProgress) => void;
}

interface Registry {
  version: 1;
  apps: InstalledApp[];
}

const REGISTRY = "registry.json";
const BUNDLE = "index.bundle";
const MANIFEST = "manifest.json";
const ICON = "icon.png";
const PARALLEL_DOWNLOADS = 4;
const ORIENTATIONS = new Set(["portrait", "landscape"]);

/** Where an app's files live below the installer root. */
export function appLayout(root: string, appId: string) {
  const app = joinUri(root, appId);
  return {
    app,
    data: joinUri(app, "data"),
    assets: joinUri(app, "assets"),
    icon: joinUri(app, ICON),
    versions: joinUri(app, "versions"),
    version: (versionId: string) => joinUri(app, "versions", versionId),
    bundle: (versionId: string) => joinUri(app, "versions", versionId, BUNDLE),
    manifest: (versionId: string) =>
      joinUri(app, "versions", versionId, MANIFEST),
    asset: (key: string, extension: string) =>
      joinUri(app, "assets", extension ? `${key}.${extension}` : key),
  };
}

/** Only server-issued identifiers may become path segments. */
function assertSegment(value: string, label: string): void {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(value))
    throw new Error(`Invalid ${label} in the app manifest.`);
}

function assertManifest(manifest: AppManifest): void {
  assertSegment(manifest.app_id, "app id");
  assertSegment(manifest.version_id, "version id");
  for (const asset of manifest.assets) {
    assertSegment(asset.key, "asset");
    if (!/^[A-Za-z0-9]{0,16}$/.test(asset.extension))
      throw new Error("Invalid asset type in the app manifest.");
  }
}

interface Download {
  sha256: string;
  md5: string;
  size: number;
  target: string;
}

/**
 * The manifest `expo-updates` would have launched: `expo-constants` reads the
 * app's Expo config from `extra.expoClient`.
 */
export function updatesManifest(
  manifest: AppManifest,
  bundleUri: string,
): Record<string, unknown> {
  return {
    id: manifest.version_id,
    createdAt: manifest.created_at,
    runtimeVersion: manifest.sdk_version,
    launchAsset: {
      key: "bundle",
      contentType: "application/javascript",
      url: bundleUri,
    },
    assets: [],
    metadata: {},
    extra: {
      expoClient: manifest.expo_config,
      scopeKey: `@opencuria/${manifest.slug}`,
    },
  };
}

/**
 * Installs, updates and removes apps agents published, and prepares their
 * launch. Files are downloaded to a temporary name, verified by size and MD5
 * and only then moved into place; the registry switches to a new version
 * once all of its files are present, so a failed update keeps the old one.
 */
export class AppInstaller {
  private registryCache: Promise<Registry> | null = null;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly fs: AppFileSystem,
    private readonly now: () => Date = () => new Date(),
  ) {}

  /** Installed apps of every account, most recently installed first. */
  async installed(): Promise<InstalledApp[]> {
    const registry = await this.registry();
    return [...registry.apps].sort((a, b) =>
      b.installedAt.localeCompare(a.installedAt),
    );
  }

  /** Install a version, or update an installed app to it. */
  install(
    manifest: AppManifest,
    options: InstallOptions,
  ): Promise<InstalledApp> {
    return this.serialized(() => this.installNow(manifest, options));
  }

  /** Remove an app with its data from this device. */
  uninstall(appId: string): Promise<void> {
    return this.serialized(async () => {
      assertSegment(appId, "app id");
      const registry = await this.registry();
      this.fs.remove(appLayout(this.fs.root, appId).app);
      await this.saveRegistry({
        ...registry,
        apps: registry.apps.filter((app) => app.appId !== appId),
      });
    });
  }

  /** URI of an installed app's icon, if it has one. */
  iconUri(app: InstalledApp): string | null {
    return app.hasIcon ? appLayout(this.fs.root, app.appId).icon : null;
  }

  /** What the native runtime needs to open an installed app. */
  async launchRequest(app: InstalledApp): Promise<AppLaunchRequest> {
    const layout = appLayout(this.fs.root, app.appId);
    const manifest = JSON.parse(
      await this.fs.readText(layout.manifest(app.versionId)),
    ) as AppManifest;
    const bundle = layout.bundle(app.versionId);
    if (!this.fs.exists(bundle))
      throw new Error(`${app.name} is incomplete; install it again.`);
    this.fs.makeDirectory(layout.data);
    const config = manifest.expo_config;
    const orientation = String(config.orientation ?? "");
    const background = config.backgroundColor;
    return {
      appId: app.appId,
      versionId: app.versionId,
      name: app.name,
      bundlePath: filePath(bundle),
      installDir: filePath(layout.app),
      dataDir: filePath(layout.data),
      manifest: updatesManifest(manifest, bundle),
      localAssets: Object.fromEntries(
        manifest.assets.map((asset) => [
          asset.key,
          layout.asset(asset.key, asset.extension),
        ]),
      ),
      orientation: ORIENTATIONS.has(orientation)
        ? (orientation as "portrait" | "landscape")
        : "default",
      ...(typeof background === "string" && background
        ? { backgroundColor: background }
        : {}),
      ...(app.hasIcon && this.fs.exists(layout.icon)
        ? { iconPath: filePath(layout.icon) }
        : {}),
    };
  }

  private serialized<T>(task: () => Promise<T>): Promise<T> {
    const next = this.queue.then(task, task);
    this.queue = next.catch(() => undefined);
    return next;
  }

  private registry(): Promise<Registry> {
    this.registryCache ??= this.loadRegistry();
    return this.registryCache;
  }

  private async loadRegistry(): Promise<Registry> {
    const uri = joinUri(this.fs.root, REGISTRY);
    if (!this.fs.exists(uri)) return { version: 1, apps: [] };
    try {
      const parsed = JSON.parse(await this.fs.readText(uri)) as Registry;
      return Array.isArray(parsed.apps)
        ? { version: 1, apps: parsed.apps }
        : { version: 1, apps: [] };
    } catch {
      return { version: 1, apps: [] };
    }
  }

  private async saveRegistry(registry: Registry): Promise<void> {
    this.fs.makeDirectory(this.fs.root);
    this.fs.writeText(joinUri(this.fs.root, REGISTRY), JSON.stringify(registry));
    this.registryCache = Promise.resolve(registry);
  }

  private async installNow(
    manifest: AppManifest,
    options: InstallOptions,
  ): Promise<InstalledApp> {
    assertManifest(manifest);
    const layout = appLayout(this.fs.root, manifest.app_id);
    const versionDir = layout.version(manifest.version_id);
    for (const dir of [layout.app, layout.assets, layout.versions, versionDir])
      this.fs.makeDirectory(dir);

    const downloads: Download[] = [
      { ...manifest.bundle, target: layout.bundle(manifest.version_id) },
      ...manifest.assets.map((asset) => ({
        ...asset,
        target: layout.asset(asset.key, asset.extension),
      })),
      ...(manifest.icon ? [{ ...manifest.icon, target: layout.icon }] : []),
    ].filter((file) => !this.isComplete(file));
    const total = downloads.reduce((sum, file) => sum + file.size, 0);
    let done = 0;
    options.onProgress?.({ done, total });
    await this.downloadAll(downloads, options, (bytes) => {
      done += bytes;
      options.onProgress?.({ done, total });
    });
    this.fs.writeText(layout.manifest(manifest.version_id), JSON.stringify(manifest));

    const registry = await this.registry();
    const previous = registry.apps.find((app) => app.appId === manifest.app_id);
    const installed: InstalledApp = {
      appId: manifest.app_id,
      scope: options.scope,
      name: manifest.name,
      slug: manifest.slug,
      versionId: manifest.version_id,
      versionNumber: manifest.number,
      versionName: manifest.version_name,
      sdkVersion: manifest.sdk_version,
      publishedAt: manifest.created_at,
      installedAt: previous?.installedAt ?? this.now().toISOString(),
      hasIcon: Boolean(manifest.icon) && this.fs.exists(layout.icon),
    };
    await this.saveRegistry({
      ...registry,
      apps: [
        installed,
        ...registry.apps.filter((app) => app.appId !== manifest.app_id),
      ],
    });
    this.removeStaleFiles(manifest);
    return installed;
  }

  /** A file already in place with the expected size and hash. */
  private isComplete(file: Download): boolean {
    return (
      this.fs.exists(file.target) &&
      this.fs.size(file.target) === file.size &&
      this.fs.md5(file.target) === file.md5
    );
  }

  private async downloadAll(
    files: Download[],
    options: InstallOptions,
    onFile: (bytes: number) => void,
  ): Promise<void> {
    const pending = [...files];
    const worker = async () => {
      for (let file = pending.shift(); file; file = pending.shift()) {
        await this.downloadOne(file, options);
        onFile(file.size);
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(PARALLEL_DOWNLOADS, files.length) }, worker),
    );
  }

  private async downloadOne(file: Download, options: InstallOptions) {
    const partial = `${file.target}.part`;
    const request = options.fileRequest(file.sha256);
    try {
      await this.fs.download(request.url, request.headers, partial);
      if (
        this.fs.size(partial) !== file.size ||
        this.fs.md5(partial) !== file.md5
      )
        throw new Error("A downloaded file was damaged; try again.");
      this.fs.move(partial, file.target);
    } finally {
      this.fs.remove(partial);
    }
  }

  /** Drop other versions and assets the installed version no longer uses. */
  private removeStaleFiles(manifest: AppManifest): void {
    const layout = appLayout(this.fs.root, manifest.app_id);
    for (const name of this.fs.list(layout.versions))
      if (name !== manifest.version_id)
        this.fs.remove(joinUri(layout.versions, name));
    const keep = new Set(
      manifest.assets.map((asset) =>
        asset.extension ? `${asset.key}.${asset.extension}` : asset.key,
      ),
    );
    for (const name of this.fs.list(layout.assets))
      if (!keep.has(name)) this.fs.remove(joinUri(layout.assets, name));
    if (!manifest.icon) this.fs.remove(layout.icon);
  }
}
