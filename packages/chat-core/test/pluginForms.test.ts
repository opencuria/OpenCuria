import { describe, expect, it } from "vitest";
import type { CredentialService, Plugin } from "../src/plugins";
import {
  boundCredentialKey,
  credentialSourceLabel,
  mcpEndpoint,
  pluginAvailability,
  pluginContents,
  serverVariableList,
} from "../src/plugins";
import {
  applySkillMarkdown,
  emptyMcpForm,
  emptyPluginForm,
  emptyRequirementForm,
  emptySkillForm,
  emptyVariableRow,
  entryErrors,
  isSavedSecret,
  formToCreateIn,
  formToUpdateIn,
  generalErrors,
  joinCommandLine,
  keepUsableCredentials,
  mcpLabel,
  parseMcpConfig,
  parseSkillMarkdown,
  pluginToForm,
  removeEntry,
  requirementKeyFor,
  requirementLabel,
  serverNameFrom,
  setMcpAuthentication,
  setMcpKind,
  setMcpOAuthRequirement,
  setRequirementService,
  skillLabel,
  splitCommandLine,
  updateEntry,
  validatePluginForm,
  type PluginFormModel,
} from "../src/pluginForms";

function service(overrides: Partial<CredentialService>): CredentialService {
  return {
    id: "svc",
    name: "API Key",
    slug: "api-key",
    description: "",
    credential_type: "env",
    env_var_name: "API_KEY",
    target_path: "",
    label: "",
    organization_id: "org-1",
    oauth_server_url: "",
    is_active: true,
    ...overrides,
  };
}

function plugin(overrides: Partial<Plugin> = {}): Plugin {
  return {
    id: "p1",
    name: "GitHub tools",
    slug: "github-tools",
    description: "Issues and PRs",
    scope: "personal",
    enabled: true,
    published: true,
    organization_id: null,
    is_global: false,
    org_enabled: false,
    available: true,
    editable: true,
    skills: [
      {
        id: "s2",
        name: "Second",
        slug: "second",
        description: "",
        body: "B",
        position: 1,
      },
      {
        id: "s1",
        name: "Review",
        slug: "review",
        description: "Review PRs",
        body: "Look closely.",
        position: 0,
      },
    ],
    mcp_servers: [
      {
        id: "m1",
        name: "GitHub",
        slug: "github",
        transport: "stdio",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-github", "a b"],
        cwd: "/workspace",
        env: { LOG_LEVEL: "info", GITHUB_TOKEN: "{{credential.github_token}}" },
        url: "",
        headers: {},
        auth_type: "none",
        oauth_requirement_key: "",
        resources: { desktop: { activation: "first_tool" } },
      },
    ],
    credential_requirements: [
      {
        id: "r1",
        key: "github_token",
        description: "",
        required: true,
        service_id: "svc-gh",
        service_name: "GITHUB_TOKEN (GitHub tools)",
        service_slug: "github-token-github-tools",
        credential_type: "env",
        credential_id: "c1",
        credential_name: "GitHub tools: GITHUB_TOKEN",
        credential_scope: "personal",
      },
    ],
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

function form(overrides: Partial<PluginFormModel> = {}): PluginFormModel {
  return { ...emptyPluginForm(), name: "Tools", ...overrides };
}

describe("command lines", () => {
  it("splits like a shell without expansion and joins back", () => {
    const argv = splitCommandLine(
      `npx -y @acme/mcp --root "/my dir" --name='x y' a\\ b`,
    );
    expect(argv).toEqual([
      "npx",
      "-y",
      "@acme/mcp",
      "--root",
      "/my dir",
      "--name=x y",
      "a b",
    ]);
    expect(splitCommandLine(joinCommandLine(argv))).toEqual(argv);
    expect(() => splitCommandLine(`npx "oops`)).toThrow(/Unterminated/);
    expect(splitCommandLine("$HOME;rm")).toEqual(["$HOME;rm"]);
  });

  it("shows stdio servers as one copyable line", () => {
    expect(
      mcpEndpoint({
        transport: "stdio",
        command: "npx",
        args: ["-y", "pkg", "a b"],
        url: "",
      }),
    ).toBe("npx -y pkg 'a b'");
  });
});

describe("plugin <-> form", () => {
  it("round-trips skills, servers and saved secrets", () => {
    const model = pluginToForm(plugin());
    expect(model.scope).toBe("personal");
    expect(model.skills.map((skill) => skill.id)).toEqual(["s1", "s2"]);
    const server = model.mcps[0]!;
    expect(server.kind).toBe("command");
    expect(server.commandLine).toBe(
      "npx -y @modelcontextprotocol/server-github 'a b'",
    );
    expect(
      server.variables.map((row) => [
        row.key,
        row.secret,
        row.source,
        row.credentialName,
      ]),
    ).toEqual([
      ["LOG_LEVEL", false, "value", ""],
      ["GITHUB_TOKEN", true, "value", "GitHub tools: GITHUB_TOKEN"],
    ]);
    expect(isSavedSecret(server.variables[1]!)).toBe(true);
    // The secret's requirement belongs to its variable, not the advanced list.
    expect(model.requirements).toEqual([]);
    const payload = formToUpdateIn(model);
    expect(payload.skills?.[0]).toMatchObject({
      id: "s1",
      name: "Review",
      description: "Review PRs",
      position: 0,
      slug: "",
    });
    expect(payload.mcp_servers?.[0]).toMatchObject({
      id: "m1",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github", "a b"],
      env: { LOG_LEVEL: "info", GITHUB_TOKEN: "{{credential.github_token}}" },
      resources: { desktop: { activation: "first_tool" } },
      url: "",
      headers: {},
    });
    // An empty saved secret keeps its credential: no value is sent.
    expect(payload.credential_requirements).toEqual([
      {
        key: "github_token",
        required: true,
        service_id: "svc-gh",
        credential_id: "c1",
      },
    ]);
    expect(payload).not.toHaveProperty("scope");
    expect(JSON.stringify(payload)).not.toMatch(/timeout/);
  });

  it("sends new secret values and the chosen scope on create", () => {
    const server = {
      ...emptyMcpForm("url"),
      name: "Docs",
      url: "https://mcp.example.com/mcp",
      variables: [
        { ...emptyVariableRow("Authorization", "Bearer t"), secret: true },
        emptyVariableRow("X-Team", "core"),
      ],
    };
    const payload = formToCreateIn(
      form({ scope: "organization", mcps: [server] }),
    );
    expect(payload.scope).toBe("organization");
    expect(payload.mcp_servers?.[0]).toMatchObject({
      transport: "streamable_http",
      url: "https://mcp.example.com/mcp",
      headers: {
        Authorization: "{{credential.authorization}}",
        "X-Team": "core",
      },
      command: "",
      args: [],
      env: {},
    });
    // A new secret becomes a credential service of the plugin with its value.
    expect(payload.credential_requirements).toEqual([
      {
        key: "authorization",
        required: true,
        new_service: {
          name: "Authorization (Tools)",
          env_var_name: "Authorization",
        },
        credential_id: null,
        value: "Bearer t",
      },
    ]);
  });

  it("links a chosen credential or leaves the secret to each workspace", () => {
    const server = {
      ...emptyMcpForm("command"),
      name: "Cmd",
      commandLine: "npx -y pkg",
      variables: [
        {
          ...emptyVariableRow("API_KEY"),
          source: "credential" as const,
          credentialId: "c9",
          credentialName: "Team key",
          serviceId: "svc-key",
        },
        { ...emptyVariableRow("OTHER_TOKEN"), source: "per_user" as const },
      ],
    };
    const payload = formToCreateIn(form({ mcps: [server] }));
    expect(payload.mcp_servers?.[0]?.env).toEqual({
      API_KEY: "{{credential.api_key}}",
      OTHER_TOKEN: "{{credential.other_token}}",
    });
    expect(payload.credential_requirements).toEqual([
      {
        key: "api_key",
        required: true,
        service_id: "svc-key",
        credential_id: "c9",
      },
      {
        key: "other_token",
        required: true,
        new_service: {
          name: "OTHER_TOKEN (Tools)",
          env_var_name: "OTHER_TOKEN",
        },
        credential_id: null,
      },
    ]);
  });

  it("switching kind clears what the other kind uses", () => {
    const start = form({
      mcps: [
        {
          ...emptyMcpForm("command"),
          name: "A",
          commandLine: "npx a",
          variables: [emptyVariableRow("TOKEN", "x")],
          desktop: "first_tool",
        },
      ],
    });
    const uid = start.mcps[0]!.uid;
    const url = setMcpKind(start, uid, "url").mcps[0]!;
    expect(url).toMatchObject({
      kind: "url",
      commandLine: "",
      desktop: "none",
    });
    expect(url.variables).toEqual([]);
    const back = setMcpKind(
      {
        ...start,
        mcps: [{ ...url, authType: "oauth", oauthRequirementKey: "k" }],
      },
      uid,
      "command",
    ).mcps[0]!;
    expect(back).toMatchObject({ authType: "none", oauthRequirementKey: "" });
    expect(start.mcps[0]!.kind).toBe("command");
  });

  it("marks variables with secret-looking names as secret", () => {
    expect(emptyVariableRow("GITHUB_TOKEN").secret).toBe(true);
    expect(emptyVariableRow("API_KEY").secret).toBe(true);
    expect(emptyVariableRow("LOG_LEVEL").secret).toBe(false);
  });
});

describe("imports", () => {
  it("reads the mcpServers JSON of Claude and Cursor", () => {
    const result = parseMcpConfig(
      JSON.stringify({
        mcpServers: {
          github: {
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            env: { GITHUB_PERSONAL_ACCESS_TOKEN: "<YOUR_TOKEN>", MODE: "full" },
          },
          docs: { url: "https://mcp.example.com/sse" },
          remote: {
            type: "http",
            url: "https://api.example.com/mcp",
            headers: { Authorization: "Bearer ${TOKEN}" },
          },
        },
      }),
    );
    expect(result.error).toBeUndefined();
    const [github, docs, remote] = result.servers;
    expect(github).toMatchObject({
      name: "Github",
      kind: "command",
      commandLine: "npx -y @modelcontextprotocol/server-github",
    });
    expect(
      github!.variables.map((row) => [row.key, row.value, row.secret]),
    ).toEqual([
      ["GITHUB_PERSONAL_ACCESS_TOKEN", "", true],
      ["MODE", "full", false],
    ]);
    expect(docs).toMatchObject({ kind: "url", transport: "sse" });
    expect(remote).toMatchObject({ transport: "streamable_http" });
    expect(remote!.variables[0]).toMatchObject({
      key: "Authorization",
      value: "",
      secret: true,
    });
  });

  it("reads VS Code servers, single objects, URLs and command lines", () => {
    expect(
      parseMcpConfig(
        JSON.stringify({
          servers: { fs: { command: "uvx", args: ["mcp-fs"] } },
        }),
      ).servers[0],
    ).toMatchObject({ name: "Fs", commandLine: "uvx mcp-fs" });
    expect(
      parseMcpConfig(JSON.stringify({ command: "node", args: ["server.js"] }))
        .servers[0],
    ).toMatchObject({ commandLine: "node server.js" });
    expect(
      parseMcpConfig("https://mcp.notion.com/mcp").servers[0],
    ).toMatchObject({ kind: "url", name: "Notion" });
    expect(
      parseMcpConfig("claude mcp add pw -- npx -y @playwright/mcp@latest")
        .servers[0],
    ).toMatchObject({
      commandLine: "npx -y @playwright/mcp@latest",
      name: "Playwright",
    });
    expect(parseMcpConfig("{nope").error).toMatch(/not valid JSON/);
    expect(parseMcpConfig(JSON.stringify({ a: 1 })).error).toMatch(
      /No MCP server/,
    );
    expect(parseMcpConfig("  ").error).toBeTruthy();
  });

  it("derives readable server names", () => {
    expect(
      serverNameFrom("npx -y @modelcontextprotocol/server-filesystem /"),
    ).toBe("Filesystem");
    expect(serverNameFrom("uvx mcp-server-fetch")).toBe("Fetch");
    expect(serverNameFrom("https://mcp.deepwiki.com/mcp")).toBe("Deepwiki");
  });

  it("parses SKILL.md frontmatter and fills only empty fields", () => {
    const text = [
      "---",
      "name: release-checklist",
      "description: >",
      "  Ship a release",
      "  safely.",
      "metadata:",
      "  version: 1",
      "---",
      "",
      "# Steps",
    ].join("\n");
    expect(parseSkillMarkdown(text)).toEqual({
      name: "release-checklist",
      description: "Ship a release safely.",
      body: "# Steps",
    });
    const applied = applySkillMarkdown(emptySkillForm(), text);
    expect(applied).toMatchObject({
      name: "Release checklist",
      description: "Ship a release safely.",
      body: "# Steps",
    });
    const kept = applySkillMarkdown(
      { ...emptySkillForm(), name: "Mine", description: "Keep" },
      text,
    );
    expect(kept).toMatchObject({ name: "Mine", description: "Keep" });
    expect(applySkillMarkdown(emptySkillForm(), "plain").body).toBe("plain");
  });
});

describe("validation", () => {
  it("requires names, instructions, a command or URL and secret values", () => {
    const errors = validatePluginForm(
      form({
        name: "",
        skills: [emptySkillForm()],
        mcps: [
          { ...emptyMcpForm("command"), name: "Cmd" },
          { ...emptyMcpForm("url"), name: "Web", url: "ftp://x" },
          {
            ...emptyMcpForm("command"),
            name: "Secret",
            commandLine: "npx a",
            variables: [{ ...emptyVariableRow("TOKEN"), secret: true }],
          },
        ],
      }),
    );
    expect(generalErrors(errors)).toEqual(["Enter a plugin name."]);
    expect(entryErrors(errors, skillLabel(0))).toEqual([
      "Enter a name.",
      "Write its instructions.",
    ]);
    expect(entryErrors(errors, 'MCP server "Cmd"')).toEqual([
      "Enter the command that starts the server.",
    ]);
    expect(entryErrors(errors, 'MCP server "Web"')).toEqual([
      "Enter an http(s) URL.",
    ]);
    expect(entryErrors(errors, 'MCP server "Secret"')).toEqual([
      'Enter the secret value of "TOKEN".',
    ]);
  });

  it("accepts a saved secret without a new value and a SKILL.md body", () => {
    const errors = validatePluginForm(
      form({
        skills: [
          {
            ...emptySkillForm(),
            body: "---\nname: from-file\n---\nDo it.",
          },
        ],
        mcps: [
          {
            ...emptyMcpForm("command"),
            name: "Cmd",
            commandLine: "npx -y pkg",
            variables: [
              {
                ...emptyVariableRow("TOKEN"),
                secret: true,
                credentialId: "c1",
                credentialName: "Saved token",
                serviceId: "svc",
              },
            ],
          },
        ],
      }),
    );
    expect(errors).toEqual([]);
  });

  it("rejects shell syntax, bad names, duplicates and unknown credentials", () => {
    const errors = validatePluginForm(
      form({
        mcps: [
          { ...emptyMcpForm("command"), name: "A", commandLine: "$(evil) x" },
          {
            ...emptyMcpForm("command"),
            name: "a",
            commandLine: "npx b",
            variables: [
              emptyVariableRow("BAD-NAME", "x"),
              emptyVariableRow("REF", "{{credential.missing}}"),
              emptyVariableRow("TPL", "{{other}}"),
            ],
          },
        ],
      }),
    );
    expect(entryErrors(errors, 'MCP server "A"')[0]).toMatch(/shell syntax/);
    const second = entryErrors(errors, 'MCP server "a"');
    expect(second).toContain("Another MCP server has the same name.");
    expect(second).toContain('"BAD-NAME" is not a valid variable name.');
    expect(
      second.some((error) => error.includes('unknown credential "missing"')),
    ).toBe(true);
    expect(
      second.some((error) => error.includes("unsupported placeholder")),
    ).toBe(true);
  });

  it("checks OAuth servers against their required MCP OAuth service", () => {
    const oauth = service({
      id: "oauth",
      name: "Notion OAuth",
      credential_type: "mcp_oauth",
      oauth_server_url: "https://mcp.notion.com/mcp",
    });
    let model = form({
      requirements: [emptyRequirementForm()],
      mcps: [{ ...emptyMcpForm("url"), name: "Notion" }],
    });
    model = setRequirementService(model, model.requirements[0]!.uid, oauth);
    expect(model.requirements[0]).toMatchObject({
      reqKey: "notion_oauth",
      credentialType: "mcp_oauth",
    });
    model = setMcpAuthentication(model, model.mcps[0]!.uid, "oauth");
    model = setMcpOAuthRequirement(model, model.mcps[0]!.uid, "notion_oauth", [
      oauth,
    ]);
    expect(model.mcps[0]!.url).toBe("https://mcp.notion.com/mcp");
    expect(
      validatePluginForm({ ...model, availableServices: [oauth] }),
    ).toEqual([]);
    const drifted = updateEntry(model, "mcps", model.mcps[0]!.uid, (mcp) => ({
      ...mcp,
      url: "https://elsewhere.example/mcp",
    }));
    expect(
      validatePluginForm({ ...drifted, availableServices: [oauth] }).join(" "),
    ).toMatch(/must be https:\/\/mcp.notion.com\/mcp/);
    const off = setMcpAuthentication(model, model.mcps[0]!.uid, "none");
    expect(off.mcps[0]!.oauthRequirementKey).toBe("");
  });

  it("names entries exactly as validation errors do", () => {
    const req = { ...emptyRequirementForm(), reqKey: "api_key" };
    expect(requirementLabel(req, 0)).toBe('Requirement "api_key"');
    expect(mcpLabel({ ...emptyMcpForm(), name: " " }, 2)).toBe("MCP server #3");
    expect(requirementKeyFor("API Key")).toBe("api_key");
    const model = form({ skills: [emptySkillForm(), emptySkillForm()] });
    const removed = removeEntry(model, "skills", model.skills[0]!.uid);
    expect(removed.skills).toHaveLength(1);
    expect(model.skills).toHaveLength(2);
  });
});

describe("plugin credentials", () => {
  it("keeps optional secrets and their descriptions", () => {
    const [stored] = plugin().credential_requirements;
    const model = pluginToForm(
      plugin({
        credential_requirements: [
          { ...stored!, required: false, description: "Raises rate limits" },
        ],
      }),
    );
    expect(formToUpdateIn(model).credential_requirements).toEqual([
      {
        key: "github_token",
        required: false,
        description: "Raises rate limits",
        service_id: "svc-gh",
        credential_id: "c1",
      },
    ]);
  });

  it("drops credentials an organization plugin cannot use", () => {
    const start = form({
      scope: "organization",
      mcps: [
        {
          ...emptyMcpForm("command"),
          name: "Cmd",
          commandLine: "npx pkg",
          variables: [
            {
              ...emptyVariableRow("API_KEY"),
              source: "credential",
              credentialId: "mine",
              credentialName: "Mine",
              serviceId: "svc-personal",
            },
            {
              ...emptyVariableRow("TEAM_KEY"),
              source: "credential",
              credentialId: "team",
              credentialName: "Team",
              serviceId: "svc-team",
            },
          ],
        },
      ],
      requirements: [{ ...emptyRequirementForm(), credentialId: "mine" }],
    });
    const next = keepUsableCredentials(start, (id) => id === "team");
    expect(
      next.mcps[0]!.variables.map((row) => [
        row.source,
        row.credentialId,
        row.serviceId,
      ]),
    ).toEqual([
      ["value", "", ""],
      ["credential", "team", "svc-team"],
    ]);
    expect(next.requirements[0]!.credentialId).toBe("");
  });

  it("tells secret variables and where their credential comes from", () => {
    expect(boundCredentialKey("{{credential.api_key}}")).toBe("api_key");
    expect(boundCredentialKey("Bearer {{credential.api_key}}")).toBeNull();
    expect(serverVariableList(plugin().mcp_servers[0]!)).toEqual([
      { name: "LOG_LEVEL", secret: false },
      { name: "GITHUB_TOKEN", secret: true },
    ]);
    const [requirement] = plugin().credential_requirements;
    expect(credentialSourceLabel(requirement!)).toBe(
      "Uses “GitHub tools: GITHUB_TOKEN”",
    );
    expect(
      credentialSourceLabel({ credential_id: null, credential_name: "" }),
    ).toBe("Each workspace adds its own");
  });
});

describe("plugin presentation", () => {
  it("summarizes contents and availability", () => {
    expect(pluginContents(plugin())).toBe("2 skills · 1 MCP server");
    expect(pluginContents({ skills: [], mcp_servers: [] })).toBe("");
    expect(pluginAvailability(plugin())).toBe("Available");
    expect(
      pluginAvailability(plugin({ scope: "organization", org_enabled: false })),
    ).toBe("Not released");
    expect(
      pluginAvailability(plugin({ scope: "builtin", published: false })),
    ).toBe("Unpublished");
  });
});
