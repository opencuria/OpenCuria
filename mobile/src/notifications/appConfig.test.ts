import { afterEach, describe, expect, it, vi } from "vitest";
import appConfig from "../../app.config";
import type { ConfigContext } from "expo/config";

afterEach(() => vi.unstubAllEnvs());

describe("native push signing configuration", () => {
  it.each([
    ["development", "", "development", "com.opencuria.mobile.dev"],
    ["production", "", "production", "com.opencuria.mobile"],
    ["development", "production", "production", "com.opencuria.mobile.dev"],
  ])(
    "uses valid Apple entitlements for %s builds with override %s",
    (variant, override, environment, bundleId) => {
      vi.stubEnv("APP_VARIANT", variant);
      vi.stubEnv("APNS_ENVIRONMENT", override);
      const config = appConfig({ config: {} } as ConfigContext);
      expect(config.ios?.bundleIdentifier).toBe(bundleId);
      expect(config.ios?.entitlements?.["aps-environment"]).toBe(environment);
      expect(config.plugins).toContainEqual([
        "expo-widgets",
        expect.objectContaining({ enablePushNotifications: true }),
      ]);
    },
  );
});

describe("home screen widget configuration", () => {
  it("offers the Apps widget in every iPhone and iPad home screen size", () => {
    const config = appConfig({ config: {} } as ConfigContext);
    const widgets = config.plugins?.find(
      (plugin) => Array.isArray(plugin) && plugin[0] === "expo-widgets",
    )?.[1] as { widgets: Array<Record<string, unknown>> };
    expect(widgets.widgets).toEqual([
      expect.objectContaining({
        name: "OpenCuriaApps",
        ios: {
          supportedFamilies: [
            "systemSmall",
            "systemMedium",
            "systemLarge",
            "systemExtraLarge",
          ],
          contentMarginsDisabled: true,
        },
        android: null,
      }),
    ]);
  });
});

describe("push proxy configuration", () => {
  it.each(["development", "production"])(
    "builds the %s app for the public OpenCuria push proxy",
    (variant) => {
      vi.stubEnv("APP_VARIANT", variant);
      const config = appConfig({ config: {} } as ConfigContext);
      const group = `group.${config.ios?.bundleIdentifier}`;
      expect(config.extra?.pushProxyUrl).toBe("https://push.opencuria.dev");
      expect(config.plugins).toContainEqual([
        "./plugins/withNotificationServiceExtension",
        { groupIdentifier: group },
      ]);
      expect(config.plugins).toContainEqual([
        "expo-widgets",
        expect.objectContaining({ groupIdentifier: group }),
      ]);
    },
  );
});
