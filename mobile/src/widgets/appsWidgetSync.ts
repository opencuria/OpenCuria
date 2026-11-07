import type { AppInstaller } from "../apps/appInstaller";
import type { AppsWidgetTarget } from "./appsWidget";
import { appsWidgetSnapshot, widgetApps } from "./appsWidgetSnapshot";
import { prepareWidgetIcons } from "./widgetIcons";

/**
 * Keeps the home screen widget in step with the apps installed for the
 * signed-in account: after installs, updates, removals and opens, and when
 * the account changes. One update runs at a time; unchanged content is not
 * handed to the system again.
 */
export class AppsWidgetSync {
  /** `undefined` until the session is known, `null` when signed out. */
  private scope: string | null | undefined = undefined;
  private loop: Promise<void> | null = null;
  private dirty = false;
  private published: string | null = null;

  constructor(
    private readonly installer: AppInstaller,
    private readonly widget: AppsWidgetTarget,
    private readonly icons: typeof prepareWidgetIcons = prepareWidgetIcons,
  ) {}

  /** Follow the installed apps; returns the stop function. */
  start(): () => void {
    const stop = this.installer.subscribe(() => void this.request());
    void this.request();
    return stop;
  }

  setScope(scope: string | null | undefined): void {
    if (scope === this.scope) return;
    this.scope = scope;
    void this.request();
  }

  /** Resolves once the widget shows the latest state. */
  request(): Promise<void> {
    this.dirty = true;
    this.loop ??= this.run();
    return this.loop;
  }

  private async run(): Promise<void> {
    try {
      while (this.dirty) {
        this.dirty = false;
        try {
          await this.update();
        } catch {
          /* The next change tries again. */
        }
      }
    } finally {
      this.loop = null;
    }
  }

  private async update(): Promise<void> {
    const scope = this.scope;
    if (scope === undefined || !this.widget.available) return;
    const apps =
      scope === null ? [] : widgetApps(await this.installer.installed(), scope);
    // Signed out, this also removes the account's icons from shared storage.
    const directory = await this.widget.iconDirectory();
    const icons = directory
      ? await this.icons(apps, this.installer, directory)
      : {};
    // The account changed meanwhile; the next round shows the new one.
    if (scope !== this.scope) return;
    const snapshot = appsWidgetSnapshot(apps, scope, icons);
    const content = JSON.stringify(snapshot);
    if (content === this.published) return;
    await this.widget.publish(snapshot);
    this.published = content;
  }
}
