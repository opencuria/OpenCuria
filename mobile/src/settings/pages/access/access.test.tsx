import React from "react";
import { Alert, Linking, type AlertButton } from "react-native";
import { act, fireEvent, screen, waitFor } from "@testing-library/react-native";
import {
  emptyPluginForm,
  formToCreateIn,
} from "@opencuria/chat-core/pluginForms";
import type {
  ApiKey,
  ApiKeyPermission,
  Credential,
  CredentialService,
  Plugin,
} from "@opencuria/client";
import { ResourceStore } from "../../resources";
import type { SettingsChoice } from "../../SettingsContext";
import { mockClient, renderSettingsPage, testUser } from "../../testUtils";
import { ApiKeyCreatedPage } from "./ApiKeyCreatedPage";
import { ApiKeyPage } from "./ApiKeyPage";
import { CredentialPage } from "./CredentialPage";
import { CredentialServicesPage } from "./CredentialServicesPage";
import { CredentialsPage } from "./CredentialsPage";
import { NewApiKeyPage } from "./NewApiKeyPage";
import { NewCredentialPage } from "./NewCredentialPage";
import { NewCredentialServicePage } from "./NewCredentialServicePage";
import { PluginEditorPage } from "./PluginEditorPage";
import { PluginSkillPage } from "./PluginEntryPages";
import { PluginPage } from "./PluginPage";
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
    const alert = confirmAlert("Delete");
    fireEvent.press(screen.getByRole("button", { name: "Delete credential" }));
    await alert.press();
    expect(remove).toHaveBeenCalledWith("cred-1");
    expect(navigation.pop).toHaveBeenCalled();
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
    name: "Playwright",
    slug: "playwright",
    description: "Browser automation",
    enabled: true,
    published: true,
    organization_id: "org-1",
    is_global: false,
    org_enabled: false,
    skills: [
      {
        id: "k1",
        name: "Basics",
        slug: "basics",
        body: "Use the browser.",
        position: 0,
      },
    ],
    mcp_servers: [],
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
      },
    ],
    ...overrides,
  });

  it("offers New plugin to admins only", async () => {
    renderSettingsPage(<PluginsPage />, {
      admin: false,
      client: mockClient({
        plugins: { list: jest.fn(async () => [plugin()]) },
      }),
    });
    // Activation is an admin setting; members never see a plugin's status.
    expect(
      await screen.findByRole("button", {
        name: "Playwright, Organization · 1 skill · 1 service",
      }),
    ).toBeTruthy();
    expect(screen.queryByText(/Inactive/)).toBeNull();
    expect(screen.queryByText(/Activate a plugin/)).toBeNull();
    expect(screen.queryByTestId("new-plugin")).toBeNull();
  });

  it("activates a plugin, opens a requirement's credential and deletes it", async () => {
    const setActive = jest.fn(async () => plugin({ org_enabled: true }));
    const remove = jest.fn(async () => undefined);
    const { navigation } = renderSettingsPage(<PluginPage pluginId="p1" />, {
      client: mockClient({
        plugins: {
          list: jest.fn(async () => [plugin()]),
          setActive,
          delete: remove,
        },
      }),
    });
    fireEvent.press(
      await screen.findByRole("switch", {
        name: "Enabled for this organization",
      }),
    );
    await waitFor(() => expect(setActive).toHaveBeenCalledWith("p1", true));
    fireEvent.press(
      screen.getByRole("button", { name: "GitHub, Required · ENV" }),
    );
    expect(navigation.push).toHaveBeenCalledWith("credential-new:svc-env");
    fireEvent.press(
      screen.getByRole("button", { name: "More for Playwright" }),
    );
    const alert = confirmAlert("Delete");
    fireEvent.press(screen.getByRole("button", { name: "Delete" }));
    await alert.press();
    expect(remove).toHaveBeenCalledWith("p1");
  });

  it("creates a plugin from the shared draft", async () => {
    const resources = new ResourceStore();
    const form = {
      ...emptyPluginForm(),
      name: "Tools",
      skills: [{ uid: "s1", name: "Basics", body: "" }],
    };
    resources.set<PluginDraft>("plugin-draft:d1", { pluginId: null, form });
    const create = jest.fn(async () => plugin({ id: "p9" }));
    const client = mockClient({
      credentials: { listServices: jest.fn(async () => []) },
      plugins: { list: jest.fn(async () => []), create },
    });
    const skill = renderSettingsPage(
      <PluginSkillPage draftKey="d1" uid="s1" />,
      {
        client,
        environment: { resources },
      },
    );
    expect(screen.getByText("Body (Markdown) is required.")).toBeTruthy();
    fireEvent.changeText(screen.getByLabelText("Body"), "Use the tools.");
    skill.unmount();

    const { navigation } = renderSettingsPage(
      <PluginEditorPage draftKey="d1" />,
      {
        client,
        environment: { resources },
      },
    );
    const save = await screen.findByTestId("save-plugin");
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.press(save);
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        formToCreateIn({
          ...form,
          skills: [{ uid: "s1", name: "Basics", body: "Use the tools." }],
        }),
      ),
    );
    expect(navigation.replace).toHaveBeenCalledWith("plugin:p9");
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
