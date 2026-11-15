import { describe, expect, it } from "vitest";
import { canTogglePlugin, pluginAvailability } from "../src/plugins";
import { formatMinutesAsDuration } from "../src/workspaceLifecycle";
import {
  agentConfigsValid,
  agentStatesFromConfigs,
  changedAgentConfigs,
  parseMaxDepth,
  type AgentConfig,
} from "../src/harnessAgents";
import {
  chatGptOAuthOutcome,
  providerAccountForm,
  providerAccountPayload,
  providerAccountsSummary,
  PROVIDER_ACCOUNT_PROVIDERS,
  unsharePayload,
} from "../src/providerAccounts";
import {
  imageDefinitionForm,
  imageDefinitionPayload,
  isImageDefinitionFormValid,
} from "../src/imageDefinitionLifecycle";

describe("workspace policy durations", () => {
  it("formats the inactivity timeout compactly", () => {
    expect(formatMinutesAsDuration(null)).toBe("Disabled");
    expect(formatMinutesAsDuration(30)).toBe("30 min");
    expect(formatMinutesAsDuration(240)).toBe("4h");
    expect(formatMinutesAsDuration(2880)).toBe("2d");
    expect(formatMinutesAsDuration(90)).toBe("1h 30m");
  });
});

describe("plugin availability", () => {
  const plugin = { enabled: true, published: true, org_enabled: false };

  it("labels unpublished, active and inactive plugins", () => {
    expect(pluginAvailability(plugin)).toBe("Inactive");
    expect(pluginAvailability({ ...plugin, org_enabled: true })).toBe("Active");
    expect(pluginAvailability({ ...plugin, published: false })).toBe(
      "Unpublished",
    );
  });

  it("only activates published, enabled plugins but always allows turning off", () => {
    expect(canTogglePlugin(plugin)).toBe(true);
    expect(canTogglePlugin({ ...plugin, enabled: false })).toBe(false);
    expect(
      canTogglePlugin({ ...plugin, enabled: false, org_enabled: true }),
    ).toBe(true);
  });
});

describe("agent defaults", () => {
  const stored: AgentConfig[] = [
    {
      agent: "build",
      mode: "primary",
      description: "",
      model: "m1",
      effort: "high",
      inherit_model: false,
      effort_strategy: "fixed",
    },
  ];

  it("sends only rows that differ from the stored or default configuration", () => {
    const states = agentStatesFromConfigs(stored);
    expect(states.general).toMatchObject({
      inherit: true,
      strategy: "inherit",
    });
    expect(changedAgentConfigs(stored, states)).toEqual([
      // Plan has no stored row: an empty model is the default, so nothing changes.
    ]);
    states.general = { ...states.general, strategy: "highest" };
    states.build = { ...states.build, model: "" };
    const changed = changedAgentConfigs(stored, states);
    expect(changed).toEqual([
      {
        agent: "build",
        model: "",
        effort: "high",
        inherit_model: false,
        effort_strategy: "fixed",
      },
      {
        agent: "general",
        model: "",
        effort: "",
        inherit_model: true,
        effort_strategy: "highest",
      },
    ]);
    expect(agentConfigsValid(changed)).toBe(false);
  });

  it("parses the nesting depth strictly", () => {
    expect(parseMaxDepth("3")).toBe(3);
    expect(parseMaxDepth("0")).toBeNull();
    expect(parseMaxDepth("1.5")).toBeNull();
    expect(parseMaxDepth("2147483648")).toBeNull();
  });
});

describe("provider accounts", () => {
  it("summarizes a provider's accounts", () => {
    const openrouter = PROVIDER_ACCOUNT_PROVIDERS[0]!;
    expect(providerAccountsSummary(openrouter, [])).toBe(
      openrouter.description,
    );
    expect(
      providerAccountsSummary(openrouter, [
        { label: "A", scope: "personal", is_active: false },
        { label: "B", scope: "organization", is_active: false },
      ]),
    ).toBe("2 accounts · None selected");
    expect(
      providerAccountsSummary(openrouter, [
        { label: "Team", scope: "organization", is_active: true },
      ]),
    ).toBe("Team · Shared");
  });

  it("builds provider payloads, shares only for admins and keeps secrets when unsharing", () => {
    const form = {
      ...providerAccountForm("amazon-bedrock"),
      label: " AWS ",
      shared: true,
      bedrockAuthMethod: "bearer" as const,
      bearerToken: "secret",
      region: "",
    };
    const member = providerAccountPayload("amazon-bedrock", form, {
      isAdmin: false,
    });
    expect(member).toEqual({
      payload: {
        label: "AWS",
        shared: false,
        auth_method: "bearer",
        region: "us-east-1",
        bearer_token: "secret",
      },
    });
    const admin = providerAccountPayload("amazon-bedrock", form, {
      isAdmin: true,
    });
    expect(
      "payload" in admin && unsharePayload("amazon-bedrock", admin.payload),
    ).toEqual({
      label: "AWS",
      shared: false,
      auth_method: "bearer",
      region: "us-east-1",
    });
    expect(
      providerAccountPayload(
        "claude-agent",
        {
          ...providerAccountForm("claude-agent"),
          label: "C",
          claudeAuthType: "subscription_token",
        },
        {
          isAdmin: false,
          account: { connected: true, auth_type: "api_token" },
        },
      ),
    ).toEqual({
      error: "Enter a token when changing the authentication type.",
    });
    expect(
      providerAccountPayload(
        "openai-compatible",
        providerAccountForm("openai-compatible"),
        {
          isAdmin: false,
        },
      ),
    ).toEqual({ error: "Account name is required." });
  });

  it("maps device login statuses", () => {
    expect(chatGptOAuthOutcome("pending")).toBe("pending");
    expect(chatGptOAuthOutcome("no_flow")).toEqual({
      error: "The authorization code expired. Start a new connection.",
    });
  });
});

describe("image definition form", () => {
  it("round-trips a definition and keeps unknown distros as custom", () => {
    const form = imageDefinitionForm({
      name: "Dev",
      base_distro: "debian:12",
      packages: ["git", "curl"],
      env_vars: { A: "1" },
    });
    expect(form).toMatchObject({
      baseDistroPreset: "custom",
      customBaseDistro: "debian:12",
      packages: "git, curl",
      envVars: "A=1",
    });
    expect(isImageDefinitionFormValid({ ...form, envVars: "=x" })).toBe(false);
    // QEMU builds need an Ubuntu cloud image.
    expect(isImageDefinitionFormValid(form)).toBe(false);
    expect(
      isImageDefinitionFormValid({ ...form, customBaseDistro: "ubuntu:25.04" }),
    ).toBe(true);
    expect(imageDefinitionPayload(form)).toEqual({
      name: "Dev",
      description: "",
      base_distro: "debian:12",
      packages: ["git", "curl"],
      env_vars: { A: "1" },
      custom_init_script: "",
    });
  });
});
