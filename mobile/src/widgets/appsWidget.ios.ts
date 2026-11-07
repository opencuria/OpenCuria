import { requireOptionalNativeModule } from "expo";
import { Directory } from "expo-file-system";
import type { AppsWidgetTarget } from "./appsWidget";

/** iOS draws the widget with expo-widgets (`AppsWidget.tsx`). */
export const appsWidget: AppsWidgetTarget = {
  available: requireOptionalNativeModule("ExpoWidgets") !== null,
  async iconDirectory() {
    // The App Group container: the widget extension cannot read app files.
    const { widgetsDirectory } = await import("expo-widgets");
    return widgetsDirectory
      ? new Directory(widgetsDirectory, "opencuria-apps")
      : null;
  },
  async publish(snapshot) {
    // Loading the layout stores it for the widget extension.
    const { AppsWidget } = await import("./AppsWidget");
    AppsWidget.updateSnapshot(snapshot);
  },
};
