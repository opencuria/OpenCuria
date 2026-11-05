import React from "react";
import { Alert, Linking, type AlertButton } from "react-native";
import { act, fireEvent, screen, waitFor } from "@testing-library/react-native";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";
import type { AgentConfig, ProviderAccount, Skill } from "@opencuria/client";
import type { SettingsChoice } from "../../SettingsContext";
import { mockClient, renderSettingsPage } from "../../testUtils";
import { AgentsPage } from "./AgentsPage";
import { ProviderAccountPage } from "./ProviderAccountPage";
import { ProviderAccountsPage } from "./ProviderAccountsPage";
import { ProvidersPage } from "./ProvidersPage";
import { SkillPage } from "./SkillPage";
import { SkillsPage } from "./SkillsPage";
import { providerAccountInfo } from "@opencuria/chat-core/providerAccounts";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));
jest.mock("expo-clipboard", () => ({
  setStringAsync: jest.fn(async () => true),
}));

const model = (
  id: string,
  provider: string,
  efforts: string[] = [],
): ProviderModel => ({
  id,
  name: id.split("/").pop()!,
  provider,
  reasoning_efforts: efforts,
  default_effort: efforts[1] ?? "",
  supports_tools: true,
  context_length: 128000,
  max_output_tokens: 8000,
});
const catalog = [
  model("openrouter/gpt-mini", "openrouter", ["low", "medium", "high"]),
  model("openrouter/claude-sonnet", "openrouter", ["low", "high"]),
  model("chatgpt/gpt-5", "chatgpt"),
];
const account = (
  overrides: Partial<ProviderAccount> = {},
): ProviderAccount => ({
  id: "acc-1",
  provider: "openrouter",
  label: "Work",
  scope: "personal",
  connected: true,
  is_active: true,
  can_manage: true,
  api_key_hint: "••••abcd",
  base_url: "https://openrouter.ai/api/v1",
  ...overrides,
});

function confirmAlert(button: string) {
  const alert = jest.spyOn(Alert, "alert");
  return {
    async press() {
      const buttons = alert.mock.calls.at(-1)![2] as AlertButton[];
      await act(async () =>
        buttons.find((entry) => entry.text === button)!.onPress!(),
      );
      alert.mockRestore();
    },
  };
}

describe("provider & models", () => {
  function providersClient(
    saveDefaultConfig = jest.fn(async (input: object) => input),
  ) {
    return mockClient({
      providerAccounts: {
        list: jest.fn(async () => [
          account(),
          account({
            id: "acc-2",
            provider: "chatgpt",
            label: "Plus",
            is_active: false,
          }),
        ]),
      },
      harness: {
        listEngineModels: jest.fn(async (engine: string) =>
          engine === "native" ? catalog : [model("claude/opus", "claude")],
        ),
        getDefaultConfig: jest.fn(async () => {
          throw Object.assign(new Error("Provider config not found"), {
            status: 404,
          });
        }),
        saveDefaultConfig,
      },
      agentSettings: {
        getVoiceSettings: jest.fn(async () => {
          throw new Error("Voice unavailable");
        }),
      },
    });
  }

  it("summarizes every provider's accounts and catalog models", async () => {
    renderSettingsPage(<ProvidersPage />, { client: providersClient() });
    expect(
      await screen.findByRole("link", {
        name: "OpenRouter, Work · Personal, 2 models",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", {
        name: "OpenAI ChatGPT, 1 account · None selected, 1 models",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", {
        name: /^Claude Agent, Run the separate Claude Agent/,
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Speech models could not be loaded. You can enter a model ID manually.",
      ),
    ).toBeTruthy();
  });

  it("chooses the small model, snaps its effort and saves automatically", async () => {
    const saveDefaultConfig = jest.fn(async (input: object) => input);
    const { navigation } = renderSettingsPage(<ProvidersPage />, {
      client: providersClient(saveDefaultConfig),
    });
    fireEvent.press(await screen.findByRole("button", { name: "Model, None" }));
    const choice = navigation.choose.mock.calls[0]![0] as SettingsChoice;
    expect(choice.groups.map((group) => group.title)).toEqual([
      undefined,
      "OpenRouter",
      "ChatGPT",
    ]);
    act(() => choice.onSelect("openrouter/gpt-mini"));
    expect(
      screen.getByRole("button", { name: "Model, gpt-mini" }),
    ).toBeTruthy();
    await waitFor(() =>
      expect(saveDefaultConfig).toHaveBeenCalledWith({
        small_model: "openrouter/gpt-mini",
        small_effort: "medium",
        speech_model: "",
        voice_cleanup_model: "",
      }),
    );
    fireEvent.press(screen.getByRole("button", { name: "High" }));
    await waitFor(() =>
      expect(saveDefaultConfig).toHaveBeenLastCalledWith(
        expect.objectContaining({ small_effort: "high" }),
      ),
    );
  });
});

describe("provider accounts", () => {
  const openrouter = providerAccountInfo("openrouter")!;

  it("switches the active account, clears it and opens the editor", async () => {
    const select = jest.fn(async () => undefined);
    const accounts = [
      account(),
      account({ id: "acc-2", label: "Team", is_active: false }),
    ];
    const { navigation, environment } = renderSettingsPage(
      <ProviderAccountsPage provider={openrouter} />,
      {
        client: mockClient({
          providerAccounts: { list: jest.fn(async () => accounts), select },
        }),
      },
    );
    fireEvent.press(await screen.findByTestId("account-acc-2"));
    await waitFor(() =>
      expect(select).toHaveBeenCalledWith("openrouter", "acc-2"),
    );
    expect(environment.onCatalogChanged).toHaveBeenCalled();
    fireEvent.press(screen.getByRole("button", { name: "Use no account" }));
    await waitFor(() =>
      expect(select).toHaveBeenCalledWith("openrouter", null),
    );
    fireEvent.press(screen.getByRole("button", { name: "Edit Team" }));
    expect(navigation.push).toHaveBeenCalledWith(
      "provider-account:openrouter:acc-2",
    );
  });

  it("adds an account with only the provider's fields and validates the name", async () => {
    const create = jest.fn(async () => account());
    const { navigation } = renderSettingsPage(
      <ProviderAccountPage provider={openrouter} accountId={null} />,
      {
        admin: false,
        client: mockClient({
          providerAccounts: { list: jest.fn(async () => []), create },
        }),
      },
    );
    expect(
      screen.queryByRole("switch", { name: "Share with this organization" }),
    ).toBeNull();
    expect(screen.getByTestId("save-provider-account")).toBeDisabled();
    fireEvent.changeText(screen.getByLabelText("Account name"), " Work ");
    fireEvent.changeText(screen.getByLabelText("API key"), "sk-or-secret");
    fireEvent.press(screen.getByTestId("save-provider-account"));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith("openrouter", {
        label: "Work",
        shared: false,
        api_key: "sk-or-secret",
        base_url: "https://openrouter.ai/api/v1",
      }),
    );
    await waitFor(() => expect(navigation.pop).toHaveBeenCalled());
  });

  it("requires a token for a new Claude Agent account", async () => {
    const create = jest.fn();
    renderSettingsPage(
      <ProviderAccountPage
        provider={providerAccountInfo("claude-agent")!}
        accountId={null}
      />,
      {
        client: mockClient({
          providerAccounts: { list: jest.fn(async () => []), create },
        }),
      },
    );
    fireEvent.changeText(screen.getByLabelText("Account name"), "Claude");
    fireEvent.press(screen.getByRole("radio", { name: "Subscription token" }));
    expect(screen.getByLabelText("Anthropic subscription token")).toBeTruthy();
    fireEvent.press(screen.getByTestId("save-provider-account"));
    expect(create).not.toHaveBeenCalled();
  });

  it("confirms unsharing an organization account and keeps its secrets", async () => {
    const update = jest.fn(async () => account());
    renderSettingsPage(
      <ProviderAccountPage provider={openrouter} accountId="acc-1" />,
      {
        client: mockClient({
          providerAccounts: {
            list: jest.fn(async () => [account({ scope: "organization" })]),
            update,
          },
        }),
      },
    );
    const toggle = await screen.findByRole("switch", {
      name: "Share with this organization",
    });
    fireEvent.press(toggle);
    const alert = confirmAlert("Unshare");
    fireEvent.press(screen.getByTestId("save-provider-account"));
    await alert.press();
    expect(update).toHaveBeenCalledWith("openrouter", "acc-1", {
      label: "Work",
      shared: false,
      api_key: "",
      base_url: "https://openrouter.ai/api/v1",
    });
  });

  it("deletes an account after confirmation", async () => {
    const remove = jest.fn(async () => undefined);
    renderSettingsPage(
      <ProviderAccountPage provider={openrouter} accountId="acc-1" />,
      {
        client: mockClient({
          providerAccounts: {
            list: jest.fn(async () => [account()]),
            delete: remove,
          },
        }),
      },
    );
    const alert = confirmAlert("Delete");
    fireEvent.press(
      await screen.findByRole("button", { name: "Delete account" }),
    );
    await alert.press();
    expect(remove).toHaveBeenCalledWith("openrouter", "acc-1");
  });

  it("connects ChatGPT with the device code and reports a denied login", async () => {
    jest.useFakeTimers();
    const statuses = [{ status: "pending" }, { status: "denied" }];
    const startChatGptOAuth = jest.fn(async () => ({
      flow_id: "flow-1",
      user_code: "ABCD-1234",
      verification_url: "https://auth.openai.com/device",
      interval: 2,
      expires_in: 600,
    }));
    const chatGptOAuthStatus = jest.fn(async () => statuses.shift()!);
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    renderSettingsPage(
      <ProviderAccountPage
        provider={providerAccountInfo("chatgpt")!}
        accountId={null}
      />,
      {
        client: mockClient({
          providerAccounts: {
            list: jest.fn(async () => []),
            startChatGptOAuth,
            chatGptOAuthStatus,
            cancelChatGptOAuth: jest.fn(async () => undefined),
          },
        }),
      },
    );
    expect(screen.queryByTestId("save-provider-account")).toBeNull();
    fireEvent.changeText(screen.getByLabelText("Account name"), "Plus");
    await act(async () =>
      fireEvent.press(screen.getByTestId("connect-chatgpt")),
    );
    expect(startChatGptOAuth).toHaveBeenCalledWith({
      label: "Plus",
      shared: false,
    });
    expect(screen.getByText("Code: ABCD-1234")).toBeTruthy();
    fireEvent.press(
      screen.getByRole("button", { name: "Open authorization page" }),
    );
    expect(openURL).toHaveBeenCalledWith("https://auth.openai.com/device");
    await act(async () => jest.advanceTimersByTime(2000));
    await act(async () => jest.advanceTimersByTime(2000));
    expect(chatGptOAuthStatus).toHaveBeenCalledTimes(2);
    expect(
      screen.getByText("Authorization was denied. Try again when ready."),
    ).toBeTruthy();
    expect(screen.getByTestId("connect-chatgpt")).toBeTruthy();
    openURL.mockRestore();
    jest.useRealTimers();
  });
});

describe("agents", () => {
  const configs: AgentConfig[] = [
    {
      agent: "build",
      mode: "primary",
      description: "",
      model: "openrouter/claude-sonnet",
      effort: "high",
      inherit_model: false,
      effort_strategy: "fixed",
    },
    {
      agent: "plan",
      mode: "primary",
      description: "",
      model: "openrouter/gpt-mini",
      effort: "",
      inherit_model: false,
      effort_strategy: "fixed",
    },
  ];

  function agentsClient(overrides: Record<string, jest.Mock> = {}) {
    return mockClient({
      harness: {
        listAgentConfigs: jest.fn(async () => configs),
        listEngineModels: jest.fn(async () => catalog),
        saveAgentConfigs:
          overrides.saveAgentConfigs ?? jest.fn(async () => configs),
      },
      agentSettings: {
        getSubagentConfig: jest.fn(async () => ({ max_depth: 2 })),
        saveSubagentConfig:
          overrides.saveSubagentConfig ??
          jest.fn(async (input: object) => input),
        getComputerUseConfig: jest.fn(async () => ({
          max_steps: 15,
          enable_recording: false,
          screenshot_max_dimension: 2400,
        })),
        saveComputerUseConfig:
          overrides.saveComputerUseConfig ??
          jest.fn(async (input: object) => input),
      },
    });
  }

  it("saves only the changed agent with a snapped effort", async () => {
    const saveAgentConfigs = jest.fn(async () => configs);
    const { navigation, environment } = renderSettingsPage(<AgentsPage />, {
      client: agentsClient({ saveAgentConfigs }),
    });
    fireEvent.press(await screen.findByTestId("build-model"));
    const choice = navigation.choose.mock.calls[0]![0] as SettingsChoice;
    expect(choice.groups[0]!.title).toBe("OpenRouter");
    act(() => choice.onSelect("openrouter/gpt-mini"));
    await waitFor(() =>
      expect(saveAgentConfigs).toHaveBeenCalledWith([
        {
          agent: "build",
          model: "openrouter/gpt-mini",
          effort: "high",
          inherit_model: false,
          effort_strategy: "fixed",
        },
      ]),
    );
    expect(environment.onCatalogChanged).toHaveBeenCalled();
  });

  it("lets subagents inherit with an effort strategy and saves the depth", async () => {
    const saveAgentConfigs = jest.fn(async () => configs);
    const saveSubagentConfig = jest.fn(async (input: object) => input);
    renderSettingsPage(<AgentsPage />, {
      client: agentsClient({ saveAgentConfigs, saveSubagentConfig }),
    });
    expect(
      (await screen.findByTestId("general-model")).props.accessibilityLabel,
    ).toBe("Model, Inherit");
    fireEvent.press(screen.getAllByRole("button", { name: "Highest" })[0]!);
    await waitFor(() =>
      expect(saveAgentConfigs).toHaveBeenCalledWith([
        expect.objectContaining({
          agent: "general",
          inherit_model: true,
          effort_strategy: "highest",
        }),
      ]),
    );
    fireEvent.press(
      screen.getByRole("button", { name: "Increase Maximum nesting depth" }),
    );
    await waitFor(() =>
      expect(saveSubagentConfig).toHaveBeenCalledWith({ max_depth: 3 }),
    );
  });

  it("rejects an out-of-range Computer Use limit and saves valid changes only", async () => {
    const saveComputerUseConfig = jest.fn(async (input: object) => ({
      max_steps: 15,
      enable_recording: true,
      screenshot_max_dimension: 2400,
      ...input,
    }));
    renderSettingsPage(<AgentsPage />, {
      client: agentsClient({ saveComputerUseConfig }),
    });
    fireEvent.changeText(await screen.findByLabelText("Step limit"), "5000");
    expect(screen.getByText("Enter a step limit from 1 to 1000.")).toBeTruthy();
    fireEvent.changeText(screen.getByLabelText("Step limit"), "40");
    fireEvent.press(
      screen.getByRole("switch", { name: "Record desktop sessions" }),
    );
    await waitFor(() =>
      expect(saveComputerUseConfig).toHaveBeenCalledWith({
        max_steps: 40,
        enable_recording: true,
      }),
    );
  });
});

describe("skills", () => {
  const skills: Skill[] = [
    {
      id: "s1",
      name: "TypeScript",
      body: "# TypeScript\nUse strict types.",
      scope: "organization",
      created_by_email: "admin@example.com",
      created_at: "",
      updated_at: "",
    },
    {
      id: "s2",
      name: "Reviews",
      body: "Review carefully.",
      scope: "personal",
      created_by_email: "ada@example.com",
      created_at: "",
      updated_at: "",
    },
  ];

  it("groups skills by scope and opens one", async () => {
    const { navigation } = renderSettingsPage(<SkillsPage />, {
      client: mockClient({ skills: { list: jest.fn(async () => skills) } }),
    });
    fireEvent.press(
      await screen.findByRole("button", { name: "TypeScript, TypeScript" }),
    );
    expect(navigation.push).toHaveBeenCalledWith("skill:s1");
    expect(screen.getByText("Organization")).toBeTruthy();
    expect(screen.getByText("Personal")).toBeTruthy();
  });

  it("creates a shared skill as admin and refreshes the composer", async () => {
    const create = jest.fn(async () => skills[0]!);
    const { environment, navigation } = renderSettingsPage(
      <SkillPage skillId={null} />,
      {
        client: mockClient({
          skills: { list: jest.fn(async () => skills), create },
        }),
      },
    );
    fireEvent.changeText(screen.getByLabelText("Name"), " Go ");
    fireEvent.changeText(screen.getByLabelText("Body"), "Write idiomatic Go.");
    fireEvent.press(
      screen.getByRole("switch", { name: "Share with entire organization" }),
    );
    fireEvent.press(screen.getByTestId("save-skill"));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: "Go",
        body: "Write idiomatic Go.",
        organization_skill: true,
      }),
    );
    expect(environment.refreshWorkspaces).toHaveBeenCalled();
    expect(navigation.pop).toHaveBeenCalled();
  });

  it("keeps organization skills read-only for members and reports failures", async () => {
    renderSettingsPage(<SkillPage skillId="s1" />, {
      admin: false,
      client: mockClient({ skills: { list: jest.fn(async () => skills) } }),
    });
    expect(
      await screen.findByText(
        "Only organization admins can edit organization skills.",
      ),
    ).toBeTruthy();
    expect(screen.queryByTestId("save-skill")).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete skill" })).toBeNull();
  });

  it("deletes a personal skill after confirmation", async () => {
    const remove = jest.fn(async () => {
      throw new Error("Skill in use");
    });
    renderSettingsPage(<SkillPage skillId="s2" />, {
      admin: false,
      client: mockClient({
        skills: { list: jest.fn(async () => skills), delete: remove },
      }),
    });
    const alert = confirmAlert("Delete");
    fireEvent.press(
      await screen.findByRole("button", { name: "Delete skill" }),
    );
    await alert.press();
    expect(remove).toHaveBeenCalledWith("s2");
  });
});
