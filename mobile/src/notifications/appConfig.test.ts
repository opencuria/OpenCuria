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
        { enablePushNotifications: true },
      ]);
    },
  );
});
