import React from "react";
import { Alert, Linking, type AlertButton } from "react-native";
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react-native";
import {
  emptyMcpForm,
  emptyPluginForm,
  pluginToForm,
} from "@opencuria/chat-core/pluginForms";
import type {
  ApiKey,
  ApiKeyPermission,
  Credential,
  CredentialService,
  OpenCuriaClient,
  Plugin,
  PluginUpdateIn,
} from "@opencuria/client";
import { ResourceStore } from "../../resources";
import type { SettingsChoice } from "../../SettingsContext";
import { mockClient, renderSettingsPage, testUser } from "../../testUtils";
import { ApiKeyCreatedPage } from "./ApiKeyCreatedPage";
import { ApiKeyPage } from "./ApiKeyPage";
import { ApiKeysPage } from "./ApiKeysPage";
import { CredentialPage } from "./CredentialPage";
import { CredentialServicesPage } from "./CredentialServicesPage";
import { CredentialsPage } from "./CredentialsPage";
import { NewApiKeyPage } from "./NewApiKeyPage";
import { NewCredentialPage } from "./NewCredentialPage";
import { NewCredentialServicePage } from "./NewCredentialServicePage";
import { PluginEditorPage } from "./PluginEditorPage";
import {
  PluginMcpImportPage,
  PluginMcpPage,
  PluginRequirementPage,
  PluginRequirementsPage,
  PluginSkillPage,
} from "./PluginEntryPages";
import { PluginPage } from "./PluginPage";
import { PluginSkillViewPage } from "./PluginSkillViewPage";
import { PluginsPage } from "./PluginsPage";
import type { PluginDraft } from "./pluginDraft";

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

const service = (overrides: Partial<CredentialService>): CredentialService => ({
  id: "svc-env",
  name: "GitHub",
  slug: "github",
  description: "",
  credential_type: "env",
  organization_id: null,
  env_var_name: "GITHUB_TOKEN",
  target_path: "",
  label: "",
  oauth_server_url: "",
  is_active: true,
  ...overrides,
});
const credential = (overrides: Partial<Credential>): Credential => ({
  id: "cred-1",
  name: "GitHub token",
  scope: "personal",
  service_id: "svc-env",
  service_name: "GitHub",
  service_slug: "github",
  credential_type: "env",
  created_by_id: testUser.id,
  oauth_connected: false,
  env_var_name: "GITHUB_TOKEN",
  created_at: "2026-10-01T00:00:00Z",
  ...overrides,
});

describe("credentials", () => {
  it("lists credentials with their OAuth state", async () => {
    const { navigation } = renderSettingsPage(<CredentialsPage />, {
      client: mockClient({
        credentials: {
          list: jest.fn(async () => [
            credential({}),
            credential({
              id: "cred-2",
              name: "Notion",
              credential_type: "mcp_oauth",
              service_name: "Notion",
              oauth_reconnect_required: true,
            }),
          ]),
        },
      }),
    });
    fireEvent.press(
      await screen.findByRole("button", {
        name: "Notion, Notion · Personal · OAuth · Reconnect required",
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith("credential:cred-2");
  });

  it("sends only changed fields and deletes after confirmation", async () => {
    const update = jest.fn(async () => credential({}));
    const remove = jest.fn(async () => undefined);
    const { navigation } = renderSettingsPage(
      <CredentialPage credentialId="cred-1" />,
      {
        admin: false,
        client: mockClient({
          credentials: {
            list: jest.fn(async () => [credential({})]),
            update,
            delete: remove,
          },
        }),
      },
    );
    expect(
      await screen.findByText("Environment variable: GITHUB_TOKEN"),
    ).toBeTruthy();
    expect(screen.getByTestId("save-credential")).toBeDisabled();
    fireEvent.changeText(screen.getByLabelText("New value"), "ghp_new");
    fireEvent.press(screen.getByTestId("save-credential"));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("cred-1", { value: "ghp_new" }),
    );
    // Destructive actions stand alone in the last section.
    const sections = screen.getAllByTestId(/^form-section-/);
    expect(sections.at(-1)!.props.testID).toBe("form-section-danger");
    const alert = confirmAlert("Delete");
    fireEvent.press(
      within(sections.at(-1)!).getByRole("button", {
        name: "Delete credential",
      }),
    );
    await alert.press();
    expect(remove).toHaveBeenCalledWith("cred-1");
    expect(navigation.pop).toHaveBeenCalled();
  });

  it("offers Retry when the credential cannot load", async () => {
    const list = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce([credential({})]);
    renderSettingsPage(<CredentialPage credentialId="cred-1" />, {
      client: mockClient({ credentials: { list } }),
    });
    expect(await screen.findByText("Server unavailable")).toBeTruthy();
    expect(screen.getByText("Couldn’t load credential")).toBeTruthy();
    expect(screen.queryByText("Credential unavailable")).toBeNull();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Service: GitHub")).toBeTruthy();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("explains services that cannot load and retries", async () => {
    const listServices = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce([service({})]);
    renderSettingsPage(<NewCredentialPage />, {
      client: mockClient({
        credentials: { list: jest.fn(async () => []), listServices },
      }),
    });
    expect(
      await screen.findByText("Couldn’t load credential services"),
    ).toBeTruthy();
    expect(screen.getByText("Server unavailable")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("credential-service")).toBeTruthy();
    expect(listServices).toHaveBeenCalledTimes(2);
  });

  it("keeps organization credentials read-only for members", async () => {
    renderSettingsPage(<CredentialPage credentialId="cred-1" />, {
      admin: false,
      client: mockClient({
        credentials: {
          list: jest.fn(async () => [
            credential({ scope: "organization", created_by_id: 99 }),
          ]),
        },
      }),
    });
    expect(await screen.findByText("Service: GitHub")).toBeTruthy();
    expect(screen.queryByTestId("save-credential")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Delete credential" }),
    ).toBeNull();
  });

  it("reconnects OAuth in the browser and disconnects after confirmation", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    const disconnectOAuth = jest.fn(async () => undefined);
    renderSettingsPage(<CredentialPage credentialId="cred-2" />, {
      client: mockClient({
        credentials: {
          list: jest.fn(async () => [
            credential({
              id: "cred-2",
              credential_type: "mcp_oauth",
              env_var_name: "",
              oauth_reconnect_required: true,
            }),
          ]),
          disconnectOAuth,
        },
      }),
    });
    fireEvent.press(
      await screen.findByRole("button", { name: "Reconnect in browser" }),
    );
    expect(openURL).toHaveBeenCalledWith(
      "https://curia.example/?settings=credentials&reconnect_credential=cred-2",
    );
    const alert = confirmAlert("Disconnect");
    fireEvent.press(screen.getByRole("button", { name: "Disconnect" }));
    await alert.press();
    expect(disconnectOAuth).toHaveBeenCalledWith("cred-2");
    openURL.mockRestore();
  });

  it("creates a shared environment credential for an active service", async () => {
    const create = jest.fn(async () => credential({}));
    const { navigation } = renderSettingsPage(<NewCredentialPage />, {
      client: mockClient({
        credentials: {
          list: jest.fn(async () => []),
          listServices: jest.fn(async () => [
            service({}),
            service({ id: "svc-off", name: "Old", is_active: false }),
          ]),
          create,
        },
      }),
    });
    fireEvent.press(await screen.findByTestId("credential-service"));
    const choice = navigation.choose.mock.calls[0]![0] as SettingsChoice;
    expect(choice.groups[0]!.options.map((option) => option.label)).toEqual([
      "GitHub",
    ]);
    act(() => choice.onSelect("svc-env"));
    expect(screen.getByTestId("save-new-credential")).toBeDisabled();
    fireEvent.changeText(screen.getByLabelText("Value"), "ghp_secret");
    fireEvent.press(
      screen.getByRole("switch", { name: "Share with entire organization" }),
    );
    fireEvent.press(screen.getByTestId("save-new-credential"));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        service_id: "svc-env",
        name: undefined,
        value: "ghp_secret",
        organization_credential: true,
      }),
    );
  });

  it("hands MCP OAuth connections over to the browser", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    renderSettingsPage(<NewCredentialPage serviceId="svc-oauth" />, {
      client: mockClient({
        credentials: {
          list: jest.fn(async () => []),
          listServices: jest.fn(async () => [
            service({
              id: "svc-oauth",
              name: "Notion",
              credential_type: "mcp_oauth",
            }),
          ]),
        },
      }),
    });
    fireEvent.press(await screen.findByTestId("save-new-credential"));
    await waitFor(() =>
      expect(openURL).toHaveBeenCalledWith(
        "https://curia.example/?settings=credentials&add_credential=svc-oauth",
      ),
    );
    openURL.mockRestore();
  });

  it("explains an inactive preselected service", async () => {
    renderSettingsPage(<NewCredentialPage serviceId="svc-off" />, {
      client: mockClient({
        credentials: {
          list: jest.fn(async () => []),
          listServices: jest.fn(async () => [
            service({ id: "svc-off", name: "Old", is_active: false }),
          ]),
        },
      }),
    });
    expect(
      await screen.findByText(
        "Old is inactive for this organization. Ask an admin to activate it before creating credentials.",
      ),
    ).toBeTruthy();
  });
});

describe("credential services", () => {
  it("activates services and creates an organization service", async () => {
    const setActive = jest.fn(async () => service({ is_active: false }));
    renderSettingsPage(<CredentialServicesPage />, {
      client: mockClient({
        credentialServices: {
          list: jest.fn(async () => [service({})]),
          setActive,
        },
      }),
    });
    fireEvent.press(await screen.findByRole("switch", { name: "GitHub" }));
    await waitFor(() =>
      expect(setActive).toHaveBeenCalledWith("svc-env", false),
    );
  });

  it("validates and creates a file service", async () => {
    const create = jest.fn(async () => service({ credential_type: "file" }));
    renderSettingsPage(<NewCredentialServicePage />, {
      client: mockClient({
        credentialServices: { list: jest.fn(async () => []), create },
      }),
    });
    fireEvent.changeText(screen.getByLabelText("Name"), "Cloud config");
    fireEvent.press(screen.getByRole("button", { name: "Credential file" }));
    fireEvent.changeText(screen.getByLabelText("Target path"), "~/.config/");
    expect(
      screen.getByText("The path must point to a file, not a folder."),
    ).toBeTruthy();
    expect(screen.getByTestId("create-credential-service")).toBeDisabled();
    fireEvent.changeText(
      screen.getByLabelText("Target path"),
      "~/.config/auth.json",
    );
    fireEvent.press(screen.getByTestId("create-credential-service"));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: "Cloud config",
        slug: "",
        description: "",
        credential_type: "file",
        label: "",
        target_path: "~/.config/auth.json",
      }),
    );
  });
});

describe("plugins", () => {
  const plugin = (overrides: Partial<Plugin> = {}): Plugin => ({
    id: "p1",
    name: "Browser",
    slug: "browser",
    description: "Browser automation",
    scope: "organization",
    enabled: true,
    published: true,
    organization_id: "org-1",
    is_global: false,
    org_enabled: false,
    available: false,
    editable: true,
    skills: [
      {
        id: "k1",
        name: "Basics",
        slug: "basics",
        description: "Opens and reads web pages.",
        body: "Use the browser.",
        position: 0,
      },
    ],
    mcp_servers: [
      {
        id: "m1",
        name: "Browser",
        slug: "browser",
        transport: "stdio",
        command: "npx",
        args: ["-y", "browser-mcp"],
        cwd: "/workspace",
        env: { API_TOKEN: "{{credential.api_token}}" },
        url: "",
        headers: {},
        auth_type: "none",
        oauth_requirement_key: "",
      },
    ],
    credential_requirements: [
      {
        id: "r1",
        key: "token",
        description: "",
        required: true,
        service_id: "svc-env",
        service_name: "GitHub",
        service_slug: "github",
        credential_type: "env",
        credential_id: null,
        credential_name: "",
        credential_scope: "",
      },
      {
        id: "r2",
        key: "api_token",
        description: "",
        required: true,
        service_id: "svc-api",
        service_name: "API_TOKEN (Browser)",
        service_slug: "api-token-browser",
        credential_type: "env",
        credential_id: "cred-api",
        credential_name: "Browser: API_TOKEN",
        credential_scope: "organization",
      },
    ],
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...overrides,
  });
  const catalog = [
    plugin(),
    plugin({
      id: "p2",
      name: "Notes",
      scope: "personal",
      organization_id: null,
      available: true,
      skills: [],
      mcp_servers: [],
    }),
    plugin({
      id: "p3",
      name: "OpenCuria",
      scope: "builtin",
      organization_id: null,
      is_global: true,
      org_enabled: true,
      available: true,
      editable: false,
      mcp_servers: [],
    }),
  ];

  /** A draft as the editor pages share it. */
  function draftStore(form: PluginDraft["form"], pluginId: string | null) {
    const resources = new ResourceStore();
    resources.set<PluginDraft>("plugin-draft:d1", { pluginId, form });
    return resources;
  }
  const editorClient = (plugins: Partial<OpenCuriaClient["plugins"]> = {}) =>
    mockClient({
      credentials: {
        listServices: jest.fn(async () => [service({})]),
        list: jest.fn(async () => [
          credential({}),
          credential({
            id: "cred-team",
            name: "Team key",
            scope: "organization",
            service_id: "svc-team",
            service_name: "Team key",
          }),
        ]),
      },
      plugins: { list: jest.fn(async () => []), ...plugins },
    });

  it("groups plugins by owner and lets members create their own", async () => {
    const { navigation } = renderSettingsPage(<PluginsPage />, {
      admin: false,
      client: mockClient({
        plugins: { list: jest.fn(async () => catalog) },
      }),
    });
    expect(await screen.findByText("Your plugins")).toBeTruthy();
    const titles = screen
      .getAllByTestId(/^form-section-/)
      .map((section) => section.props.testID);
    expect(titles).toEqual([
      "form-section-personal",
      "form-section-organization",
      "form-section-builtin",
    ]);
    // Release for the organization is an admin setting.
    expect(
      screen.getByRole("button", {
        name: "Browser, 1 skill · 1 MCP server",
      }),
    ).toBeTruthy();
    expect(screen.queryByText(/Available|Not released/)).toBeNull();
    fireEvent.press(screen.getByTestId("new-plugin"));
    expect(navigation.push).toHaveBeenCalledWith(
      expect.stringMatching(/^plugin-editor:/),
    );
  });

  it("shows admins whether shared plugins are available", async () => {
    renderSettingsPage(<PluginsPage />, {
      client: mockClient({
        plugins: { list: jest.fn(async () => catalog) },
      }),
    });
    expect(
      await screen.findByRole("button", {
        name: "Browser, 1 skill · 1 MCP server · Not released",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "OpenCuria, 1 skill · Available" }),
    ).toBeTruthy();
    // Personal plugins need no release.
    expect(screen.getByRole("button", { name: "Notes" })).toBeTruthy();
  });

  it("offers New plugin in the empty state", async () => {
    renderSettingsPage(<PluginsPage />, {
      admin: false,
      client: mockClient({ plugins: { list: jest.fn(async () => []) } }),
    });
    expect(await screen.findByText("No plugins yet")).toBeTruthy();
    expect(
      screen.getByText(
        "Plugins add skills and tools to your agents. Create one, or ask an agent to build one for you.",
      ),
    ).toBeTruthy();
    expect(screen.getByTestId("empty-new-plugin")).toBeTruthy();
  });

  it("releases a plugin, shows its contents, opens a credential and deletes it", async () => {
    const setActive = jest.fn(async () =>
      plugin({ org_enabled: true, available: true }),
    );
    const remove = jest.fn(async () => undefined);
    const { navigation, environment } = renderSettingsPage(
      <PluginPage pluginId="p1" />,
      {
        client: mockClient({
          plugins: {
            list: jest.fn(async () => [plugin()]),
            setActive,
            delete: remove,
          },
        }),
      },
    );
    fireEvent.press(
      await screen.findByRole("switch", {
        name: "Available in this organization",
      }),
    );
    await waitFor(() => expect(setActive).toHaveBeenCalledWith("p1", true));
    await waitFor(() =>
      expect(environment.refreshWorkspaces).toHaveBeenCalled(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("edit-plugin")).toBeEnabled(),
    );
    expect(screen.getByText("Browser automation")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Basics, Opens and reads web pages." }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: "Browser, Command · npx -y browser-mcp · 1 secret",
      }),
    ).toBeTruthy();
    fireEvent.press(
      screen.getByRole("button", {
        name: "GitHub, Required · ENV · Each workspace adds its own",
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith("credential-new:svc-env");
    // The plugin brings this credential itself: nothing to add.
    expect(screen.getByText("API_TOKEN (Browser)")).toBeTruthy();
    expect(screen.getByText("Uses “Browser: API_TOKEN”")).toBeTruthy();
    fireEvent.press(screen.getByTestId("edit-plugin"));
    expect(navigation.push).toHaveBeenCalledWith(
      expect.stringMatching(/^plugin-editor:/),
    );
    const alert = confirmAlert("Delete");
    fireEvent.press(screen.getByRole("button", { name: "Delete plugin" }));
    await alert.press();
    expect(remove).toHaveBeenCalledWith("p1");
  });

  it("shows members a built-in plugin without release or editing", async () => {
    renderSettingsPage(<PluginPage pluginId="p3" />, {
      admin: false,
      client: mockClient({
        plugins: { list: jest.fn(async () => catalog) },
      }),
    });
    expect(await screen.findByText("Skills")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByTestId("edit-plugin")).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete plugin" })).toBeNull();
  });

  it("shows a skill's description and instructions", async () => {
    renderSettingsPage(<PluginSkillViewPage pluginId="p1" skillId="k1" />, {
      client: mockClient({
        plugins: { list: jest.fn(async () => [plugin()]) },
      }),
    });
    expect(await screen.findByText("Opens and reads web pages.")).toBeTruthy();
    expect(screen.getByText("Instructions: Use the browser.")).toBeTruthy();
  });

  it("explains credential services that cannot load and retries", async () => {
    const listServices = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce([]);
    renderSettingsPage(<PluginEditorPage draftKey="d1" />, {
      client: mockClient({
        credentials: { listServices },
        plugins: { list: jest.fn(async () => []) },
      }),
      environment: {
        resources: draftStore({ ...emptyPluginForm(), name: "Tools" }, null),
      },
    });
    expect(
      await screen.findByText(
        "Couldn’t load credential services: Server unavailable",
      ),
    ).toBeTruthy();
    expect(screen.getByTestId("save-plugin")).toBeDisabled();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByTestId("save-plugin")).toBeEnabled(),
    );
    expect(listServices).toHaveBeenCalledTimes(2);
  });

  it("creates a personal plugin as a member, without the sharing choice", async () => {
    const create = jest.fn(async () => plugin({ id: "p9" }));
    const resources = draftStore(emptyPluginForm(), null);
    const client = editorClient({ create });
    const first = renderSettingsPage(<PluginEditorPage draftKey="d1" />, {
      admin: false,
      client,
      environment: { resources },
    });
    expect(screen.queryByText("Available to")).toBeNull();
    expect(screen.getByText("Enter a plugin name.")).toBeTruthy();
    fireEvent.changeText(screen.getByLabelText("Name"), "Notes");
    fireEvent.press(screen.getByRole("button", { name: "Add skill" }));
    const uid =
      resources.get<PluginDraft>("plugin-draft:d1").data!.form.skills[0]!.uid;
    expect(first.navigation.push).toHaveBeenCalledWith(
      `plugin-skill:d1:${uid}`,
    );
    first.unmount();

    const skill = renderSettingsPage(
      <PluginSkillPage draftKey="d1" uid={uid} />,
      {
        admin: false,
        client,
        environment: { resources },
      },
    );
    expect(
      screen.getByText("Enter a name. Write its instructions."),
    ).toBeTruthy();
    // A pasted SKILL.md fills in the name and description.
    fireEvent.changeText(
      screen.getByLabelText("Instructions"),
      "---\nname: meeting-notes\ndescription: Summarizes meetings.\n---\nWrite short notes.",
    );
    expect(screen.getByDisplayValue("Meeting notes")).toBeTruthy();
    expect(screen.getByDisplayValue("Summarizes meetings.")).toBeTruthy();
    expect(screen.getByDisplayValue("Write short notes.")).toBeTruthy();
    expect(
      screen.getByText(
        "Agents see the description and load the instructions when a task matches it.",
      ),
    ).toBeTruthy();
    skill.unmount();

    const editor = renderSettingsPage(<PluginEditorPage draftKey="d1" />, {
      admin: false,
      client,
      environment: { resources },
    });
    expect(
      screen.getByRole("link", {
        name: "Meeting notes, Summarizes meetings.",
      }),
    ).toBeTruthy();
    const save = screen.getByTestId("save-plugin");
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.press(save);
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "Notes",
        scope: "personal",
        skills: [
          expect.objectContaining({
            name: "Meeting notes",
            description: "Summarizes meetings.",
            body: "Write short notes.",
          }),
        ],
      }),
    );
    await waitFor(() =>
      expect(editor.navigation.replace).toHaveBeenCalledWith("plugin:p9"),
    );
    expect(editor.environment.refreshWorkspaces).toHaveBeenCalled();
  });

  it("lets admins share a new plugin with the organization", async () => {
    const create = jest.fn(async () => plugin({ id: "p9" }));
    renderSettingsPage(<PluginEditorPage draftKey="d1" />, {
      client: editorClient({ create }),
      environment: {
        resources: draftStore({ ...emptyPluginForm(), name: "Team" }, null),
      },
    });
    fireEvent.press(
      screen.getByRole("button", { name: "Everyone in the organization" }),
    );
    expect(
      screen.getByText("Members can use it once you make it available."),
    ).toBeTruthy();
    const save = screen.getByTestId("save-plugin");
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.press(save);
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Team", scope: "organization" }),
      ),
    );
  });

  it("saves an edited plugin and keeps saved secrets", async () => {
    const update = jest.fn(async () => plugin());
    const resources = draftStore(pluginToForm(plugin()), "p1");
    const client = editorClient({ update });
    const uid =
      resources.get<PluginDraft>("plugin-draft:d1").data!.form.mcps[0]!.uid;
    const server = renderSettingsPage(
      <PluginMcpPage draftKey="d1" uid={uid} />,
      { client, environment: { resources } },
    );
    // One command line; no timeouts, working directory or desktop.
    expect(screen.getByLabelText("Command line").props.value).toBe(
      "npx -y browser-mcp",
    );
    expect(screen.queryByText(/timeout/i)).toBeNull();
    expect(screen.queryByLabelText("Working directory")).toBeNull();
    expect(screen.queryByPlaceholderText(/Playwright/)).toBeNull();
    expect(
      screen.getByText("Runs inside the workspace, e.g. npx -y <package>."),
    ).toBeTruthy();
    expect(screen.getByText("Environment variables")).toBeTruthy();
    expect(
      screen.getByPlaceholderText("Saved · Browser: API_TOKEN"),
    ).toBeTruthy();
    fireEvent.changeText(
      screen.getByLabelText("Command line"),
      "npx -y browser-mcp --headless",
    );
    server.unmount();

    const { navigation } = renderSettingsPage(
      <PluginEditorPage draftKey="d1" />,
      { client, environment: { resources } },
    );
    expect(screen.queryByText("Available to")).toBeNull();
    const save = screen.getByTestId("save-plugin");
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.press(save);
    await waitFor(() => expect(update).toHaveBeenCalled());
    const payload = (update.mock.calls[0] as unknown[])[1] as PluginUpdateIn;
    expect(payload.mcp_servers?.[0]).toMatchObject({
      id: "m1",
      transport: "stdio",
      command: "npx",
      args: ["-y", "browser-mcp", "--headless"],
      env: { API_TOKEN: "{{credential.api_token}}" },
    });
    // The saved secret keeps its credential; no value is sent again.
    expect(payload.credential_requirements).toEqual([
      {
        key: "api_token",
        required: true,
        service_id: "svc-api",
        credential_id: "cred-api",
      },
      {
        key: "token",
        description: "",
        required: true,
        service_id: "svc-env",
        credential_id: null,
      },
    ]);
    await waitFor(() => expect(navigation.pop).toHaveBeenCalled());
  });

  it("edits a URL server's headers and marks secret names", async () => {
    const mcp = emptyMcpForm();
    const resources = draftStore(
      { ...emptyPluginForm(), name: "Docs", mcps: [mcp] },
      null,
    );
    const { navigation } = renderSettingsPage(
      <PluginMcpPage draftKey="d1" uid={mcp.uid} />,
      {
        client: editorClient(),
        environment: { resources },
      },
    );
    fireEvent.press(screen.getByRole("radio", { name: "URL" }));
    expect(screen.queryByLabelText("Command line")).toBeNull();
    fireEvent.changeText(
      screen.getByLabelText("Server URL"),
      "https://docs.example/sse",
    );
    fireEvent.press(screen.getByRole("button", { name: "SSE" }));
    // Sign-in needs an OAuth credential requirement.
    expect(screen.queryByText("Sign-in")).toBeNull();
    expect(screen.getByText("Headers")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Add header" }));
    fireEvent.changeText(screen.getByLabelText("Header 1 name"), "X-Api-Key");
    expect(screen.getByRole("switch", { name: "Secret" })).toBeChecked();
    fireEvent.changeText(screen.getByLabelText("Header 1 value"), "s3cret");
    let saved =
      resources.get<PluginDraft>("plugin-draft:d1").data!.form.mcps[0]!;
    expect(saved).toMatchObject({
      kind: "url",
      url: "https://docs.example/sse",
      transport: "sse",
      variables: [
        expect.objectContaining({
          key: "X-Api-Key",
          value: "s3cret",
          secret: true,
        }),
      ],
    });

    // A secret can use an existing credential instead of a value.
    fireEvent.press(
      await screen.findByRole("button", { name: "Comes from, Entered value" }),
    );
    const choice = navigation.choose.mock.calls[0]![0] as SettingsChoice;
    expect(
      choice.groups.flatMap((group) => group.options).map((o) => o.label),
    ).toEqual([
      "Enter a value",
      "Each workspace adds its own",
      "GitHub token",
      "Team key",
    ]);
    act(() => choice.onSelect("cred-team"));
    saved = resources.get<PluginDraft>("plugin-draft:d1").data!.form.mcps[0]!;
    expect(saved.variables[0]).toMatchObject({
      source: "credential",
      value: "",
      credentialId: "cred-team",
      serviceId: "svc-team",
    });
    expect(screen.queryByLabelText("Header 1 value")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Comes from, Team key" }),
    ).toBeTruthy();
  });

  it("adds servers from a pasted configuration", async () => {
    const resources = draftStore({ ...emptyPluginForm(), name: "Tools" }, null);
    const { navigation } = renderSettingsPage(
      <PluginMcpImportPage draftKey="d1" />,
      { client: editorClient(), environment: { resources } },
    );
    expect(screen.getByTestId("add-pasted-servers")).toBeDisabled();
    fireEvent.changeText(screen.getByLabelText("Configuration"), "{ nope");
    expect(screen.getByText("This is not valid JSON.")).toBeTruthy();
    expect(screen.getByTestId("add-pasted-servers")).toBeDisabled();
    fireEvent.changeText(
      screen.getByLabelText("Configuration"),
      JSON.stringify({
        mcpServers: {
          github: {
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            env: { GITHUB_TOKEN: "<token>" },
          },
          linear: { url: "https://mcp.linear.app/sse" },
        },
      }),
    );
    expect(screen.getByText("Adds Github, Linear.")).toBeTruthy();
    fireEvent.press(screen.getByTestId("add-pasted-servers"));
    expect(navigation.pop).toHaveBeenCalled();
    const { mcps } = resources.get<PluginDraft>("plugin-draft:d1").data!.form;
    expect(mcps).toEqual([
      expect.objectContaining({
        name: "Github",
        kind: "command",
        commandLine: "npx -y @modelcontextprotocol/server-github",
      }),
      expect.objectContaining({
        name: "Linear",
        kind: "url",
        transport: "sse",
      }),
    ]);
  });

  it("adds a credential requirement under Advanced", async () => {
    const resources = draftStore({ ...emptyPluginForm(), name: "Tools" }, null);
    const client = editorClient();
    const editor = renderSettingsPage(<PluginEditorPage draftKey="d1" />, {
      admin: false,
      client,
      environment: { resources },
    });
    expect(screen.getByRole("link", { name: "More credentials" })).toBeTruthy();
    // Wait for the credential services the requirement page offers.
    await waitFor(() =>
      expect(screen.getByTestId("save-plugin")).toBeEnabled(),
    );
    editor.unmount();

    const list = renderSettingsPage(<PluginRequirementsPage draftKey="d1" />, {
      admin: false,
      client,
      environment: { resources },
    });
    fireEvent.press(
      await screen.findByRole("button", { name: "Add credential" }),
    );
    const uid =
      resources.get<PluginDraft>("plugin-draft:d1").data!.form.requirements[0]!
        .uid;
    expect(list.navigation.push).toHaveBeenCalledWith(
      `plugin-requirement:d1:${uid}`,
    );
    list.unmount();

    const page = renderSettingsPage(
      <PluginRequirementPage draftKey="d1" uid={uid} />,
      { admin: false, client, environment: { resources } },
    );
    // Members get no hints at admin-only settings.
    expect(screen.queryByText(/Credential services/)).toBeNull();
    fireEvent.press(
      await screen.findByRole("button", { name: "Credential service, Choose" }),
    );
    const choice = page.navigation.choose.mock.calls[0]![0] as SettingsChoice;
    act(() => choice.onSelect("svc-env"));
    expect(
      await screen.findByText("Use it in values as {{credential.github}}."),
    ).toBeTruthy();

    // Optionally the plugin always uses one credential of the service.
    fireEvent.press(
      await screen.findByRole("button", {
        name: "Credential, Each workspace adds its own",
      }),
    );
    const credentialChoice = page.navigation.choose.mock
      .calls[1]![0] as SettingsChoice;
    expect(
      credentialChoice.groups
        .flatMap((group) => group.options)
        .map((o) => o.label),
    ).toEqual(["Each workspace adds its own", "GitHub token"]);
    act(() => credentialChoice.onSelect("cred-1"));
    expect(
      resources.get<PluginDraft>("plugin-draft:d1").data!.form.requirements[0],
    ).toMatchObject({ serviceId: "svc-env", credentialId: "cred-1" });
  });
});

describe("API keys", () => {
  const permissions: ApiKeyPermission[] = [
    {
      value: "skills:read",
      label: "",
      description: "Read skills",
      group: "skills",
    },
    {
      value: "skills:write",
      label: "",
      description: "Write skills",
      group: "skills",
    },
  ];
  const key = (overrides: Partial<ApiKey> = {}): ApiKey => ({
    id: "k1",
    name: "n8n",
    key_prefix: "kai_ab12",
    is_active: true,
    created_at: "2026-10-01T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    permissions: ["skills:read", "skills:write"],
    ...overrides,
  });

  it("explains a list that cannot load and retries", async () => {
    const list = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce([key()]);
    renderSettingsPage(<ApiKeysPage />, {
      client: mockClient({
        apiKeys: { list, permissions: jest.fn(async () => permissions) },
      }),
    });
    expect(await screen.findByText("Couldn’t load API keys")).toBeTruthy();
    expect(screen.getByText("Server unavailable")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("key-k1")).toBeTruthy();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("keeps the keys and offers Retry when a refresh fails", async () => {
    const list = jest
      .fn()
      .mockResolvedValueOnce([key()])
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce([key()]);
    const client = mockClient({
      apiKeys: { list, permissions: jest.fn(async () => permissions) },
    });
    const { environment } = renderSettingsPage(<ApiKeysPage />, { client });
    expect(await screen.findByTestId("key-k1")).toBeTruthy();
    await act(() =>
      environment.resources.load("api-keys", () => client.apiKeys.list(), true),
    );
    expect(screen.getByText("Server unavailable")).toBeTruthy();
    expect(screen.getByTestId("key-k1")).toBeTruthy();
    expect(screen.queryByText("Couldn’t load API keys")).toBeNull();
    await act(async () =>
      fireEvent.press(screen.getByRole("button", { name: "Retry" })),
    );
    expect(list).toHaveBeenCalledTimes(3);
    expect(screen.queryByText("Server unavailable")).toBeNull();
  });

  it("offers New API key in the empty state", async () => {
    const { navigation } = renderSettingsPage(<ApiKeysPage />, {
      client: mockClient({
        apiKeys: {
          list: jest.fn(async () => []),
          permissions: jest.fn(async () => permissions),
        },
      }),
    });
    expect(await screen.findByText("No API keys yet")).toBeTruthy();
    fireEvent.press(screen.getByTestId("empty-new-api-key"));
    expect(navigation.push).toHaveBeenCalledWith("api-key-new");
  });

  it("retries permissions that cannot load and still offers revocation", async () => {
    const load = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce(permissions);
    renderSettingsPage(<ApiKeyPage keyId="k1" />, {
      client: mockClient({
        apiKeys: { list: jest.fn(async () => [key()]), permissions: load },
      }),
    });
    expect(await screen.findByText("Server unavailable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Revoke key" })).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("switch", { name: "Full access" }),
    ).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("retries permissions of a new key that cannot load", async () => {
    const load = jest
      .fn()
      .mockRejectedValueOnce(new Error("Server unavailable"))
      .mockResolvedValueOnce(permissions);
    renderSettingsPage(<NewApiKeyPage />, {
      client: mockClient({
        apiKeys: { list: jest.fn(async () => []), permissions: load },
      }),
    });
    expect(await screen.findByText("Server unavailable")).toBeTruthy();
    fireEvent.press(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("switch", { name: "Full access" }),
    ).toBeTruthy();
  });

  it("creates a restricted key only with a selection and shows the secret once", async () => {
    const resources = new ResourceStore();
    const create = jest.fn(async () => ({
      ...key({ permissions: ["skills:read"] }),
      key: "kai_secret",
    }));
    const client = mockClient({
      apiKeys: {
        list: jest.fn(async () => []),
        permissions: jest.fn(async () => permissions),
        create,
      },
    });
    const { navigation, unmount } = renderSettingsPage(<NewApiKeyPage />, {
      client,
      environment: { resources },
    });
    fireEvent.changeText(screen.getByLabelText("Name"), "n8n");
    fireEvent.press(await screen.findByRole("switch", { name: "Full access" }));
    // Restricting starts with everything selected; the last one stays on.
    fireEvent.press(screen.getByRole("switch", { name: "skills:write" }));
    expect(screen.getByRole("switch", { name: "skills:read" })).toBeDisabled();
    fireEvent.press(screen.getByTestId("create-api-key"));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: "n8n",
        expires_at: null,
        permissions: ["skills:read"],
      }),
    );
    expect(navigation.replace).toHaveBeenCalledWith("api-key-created:k1");
    unmount();

    renderSettingsPage(<ApiKeyCreatedPage keyId="k1" />, {
      client,
      environment: { resources },
    });
    expect(screen.getByText("API key: kai_secret")).toBeTruthy();
  });

  it("treats a key with every permission as full access and revokes it", async () => {
    const revoke = jest.fn(async () => undefined);
    const updatePermissions = jest.fn(async () =>
      key({ permissions: ["skills:read"] }),
    );
    renderSettingsPage(<ApiKeyPage keyId="k1" />, {
      client: mockClient({
        apiKeys: {
          list: jest.fn(async () => [key()]),
          permissions: jest.fn(async () => permissions),
          revoke,
          updatePermissions,
        },
      }),
    });
    const full = await screen.findByRole("switch", { name: "Full access" });
    expect(full.props.accessibilityState.checked).toBe(true);
    fireEvent.press(full);
    fireEvent.press(screen.getByRole("switch", { name: "skills:write" }));
    await waitFor(() =>
      expect(updatePermissions).toHaveBeenCalledWith("k1", ["skills:read"]),
    );
    const alert = confirmAlert("Revoke");
    fireEvent.press(screen.getByRole("button", { name: "Revoke key" }));
    await alert.press();
    expect(revoke).toHaveBeenCalledWith("k1");
    expect(
      await screen.findByText("Revoked keys cannot be used or changed."),
    ).toBeTruthy();
  });
});
