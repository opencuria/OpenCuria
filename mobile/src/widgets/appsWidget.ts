import { Platform } from "react-native";
import { Directory, Paths } from "expo-file-system";
import { isAppsWidgetAvailable, updateAppsWidget } from "../apps/appRuntime";
import type { AppsWidgetSnapshot } from "./appsWidgetSnapshot";

/** The home screen widget of the platform. */
export interface AppsWidgetTarget {
  /** Whether this build has the widget. */
  available: boolean;
  /** Where widget icons go: a directory the widget can read. */
  iconDirectory(): Promise<Directory | null>;
  publish(snapshot: AppsWidgetSnapshot): Promise<void>;
}

/** Android draws the widget natively (`modules/opencuria-app-runtime`). */
export const appsWidget: AppsWidgetTarget = {
  available: Platform.OS === "android" && isAppsWidgetAvailable(),
  // The widget runs in OpenCuria's process and reads its files directly.
  iconDirectory: async () =>
    new Directory(Paths.document, "opencuria-widget-icons"),
  publish: (snapshot) => updateAppsWidget(JSON.stringify(snapshot)),
};
