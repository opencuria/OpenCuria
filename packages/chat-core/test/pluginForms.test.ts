import { describe, expect, it } from "vitest";
import type { CredentialService } from "../src/plugins";
import {
  emptyMcpForm,
  emptyPluginForm,
  emptyRequirementForm,
  emptySkillForm,
  entryErrors,
  generalErrors,
  mcpLabel,
  removeEntry,
  requirementKeyFor,
  requirementLabel,
  setMcpAuthentication,
  setMcpOAuthRequirement,
  setMcpTransport,
  setRequirementService,
  skillLabel,
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

const NOTION = service({
  id: "notion",
  name: "Notion OAuth",
  credential_type: "mcp_oauth",
  oauth_server_url: "https://mcp.notion.com/mcp",
});

function formWith(changes: Partial<PluginFormModel>): PluginFormModel {
  return { ...emptyPluginForm(), ...changes };
}

describe("plugin form entries", () => {
  it("replaces and removes one entry by uid without touching the input", () => {
    const first = emptySkillForm();
    const second = emptySkillForm();
    const form = formWith({ skills: [first, second] });

    const renamed = updateEntry(form, "skills", second.uid, (skill) => ({
      ...skill,
      name: "Basics",
    }));
    expect(renamed.skills.map((skill) => skill.name)).toEqual(["", "Basics"]);
    expect(renamed.skills[0]).toBe(first);
    expect(form.skills[1]!.name).toBe("");

    expect(removeEntry(renamed, "skills", first.uid).skills).toEqual([
      renamed.skills[1],
    ]);
  });
});

describe("plugin MCP server edits", () => {
  it("clears what the new transport forbids", () => {
    const stdio = {
      ...emptyMcpForm(),
      command: "npx",
      argsText: "-y",
      desktop: "first_tool" as const,
      env: [{ uid: "e", key: "TOKEN", value: "x" }],
    };
    const form = formWith({ mcps: [stdio] });

    const http = setMcpTransport(form, stdio.uid, "streamable_http");
    expect(http.mcps[0]).toMatchObject({
      transport: "streamable_http",
      desktop: "none",
      command: "",
      argsText: "",
      env: [],
    });
    expect(form.mcps[0]).toBe(stdio);
    expect(setMcpTransport(form, stdio.uid, "stdio").mcps[0]).toBe(stdio);

    const oauth = {
      ...http.mcps[0]!,
      url: "https://mcp.example.com/mcp",
      headers: [{ uid: "h", key: "X-Team", value: "a" }],
      authType: "oauth" as const,
      oauthRequirementKey: "notion",
    };
    expect(
      setMcpTransport(formWith({ mcps: [oauth] }), oauth.uid, "stdio").mcps[0],
    ).toMatchObject({
      transport: "stdio",
      url: "",
      headers: [],
      authType: "none",
      oauthRequirementKey: "",
    });
  });

  it("forgets the OAuth requirement only when authentication is turned off", () => {
    const mcp = {
      ...emptyMcpForm(),
      transport: "streamable_http" as const,
      authType: "oauth" as const,
      oauthRequirementKey: "notion",
    };
    const form = formWith({ mcps: [mcp] });

    expect(setMcpAuthentication(form, mcp.uid, "oauth").mcps[0]).toMatchObject({
      authType: "oauth",
      oauthRequirementKey: "notion",
    });
    expect(setMcpAuthentication(form, mcp.uid, "none").mcps[0]).toMatchObject({
      authType: "none",
      oauthRequirementKey: "",
    });
  });

  it("adopts the endpoint of a linked MCP OAuth service only", () => {
    const mcp = { ...emptyMcpForm(), url: "https://stale.example/mcp" };
    const oauthRequirement = {
      ...emptyRequirementForm(),
      reqKey: "notion",
      serviceId: NOTION.id,
    };
    const envRequirement = {
      ...emptyRequirementForm(),
      reqKey: "token",
      serviceId: "svc",
    };
    const form = formWith({
      mcps: [mcp],
      requirements: [oauthRequirement, envRequirement],
    });
    const services = [NOTION, service({})];

    expect(
      setMcpOAuthRequirement(form, mcp.uid, "notion", services).mcps[0],
    ).toMatchObject({
      oauthRequirementKey: "notion",
      url: NOTION.oauth_server_url,
    });
    expect(
      setMcpOAuthRequirement(form, mcp.uid, "token", services).mcps[0],
    ).toMatchObject({ oauthRequirementKey: "token", url: mcp.url });
  });
});

describe("plugin requirement service edits", () => {
  it("takes over the service type and derives an empty key from its name", () => {
    const requirement = emptyRequirementForm();
    const form = formWith({ requirements: [requirement] });

    expect(requirementKeyFor("API Key")).toBe("api_key");
    expect(
      setRequirementService(
        form,
        requirement.uid,
        service({ credential_type: "file" }),
      ).requirements[0],
    ).toMatchObject({
      serviceId: "svc",
      credentialType: "file",
      reqKey: "api_key",
    });

    const typed = formWith({
      requirements: [{ ...requirement, reqKey: "token" }],
    });
    expect(
      setRequirementService(typed, requirement.uid, service({}))
        .requirements[0]!.reqKey,
    ).toBe("token");
    expect(setRequirementService(form, "missing", service({}))).toBe(form);
  });

  it("links the only OAuth server, or the servers already using the key", () => {
    const requirement = emptyRequirementForm();
    const server = {
      ...emptyMcpForm(),
      transport: "streamable_http" as const,
      authType: "oauth" as const,
    };
    const plain = { ...emptyMcpForm(), url: "https://other.example/mcp" };
    const form = formWith({
      mcps: [server, plain],
      requirements: [requirement],
    });

    const linked = setRequirementService(form, requirement.uid, NOTION);
    expect(linked.requirements[0]).toMatchObject({
      credentialType: "mcp_oauth",
      reqKey: "notion_oauth",
    });
    expect(linked.mcps[0]).toMatchObject({
      oauthRequirementKey: "notion_oauth",
      url: NOTION.oauth_server_url,
    });
    expect(linked.mcps[1]).toBe(plain);

    const second = { ...server, uid: "second" };
    const ambiguous = formWith({
      mcps: [server, second],
      requirements: [requirement],
    });
    expect(
      setRequirementService(ambiguous, requirement.uid, NOTION).mcps,
    ).toEqual(ambiguous.mcps);

    const keyed = formWith({
      mcps: [server, { ...second, oauthRequirementKey: "notion" }],
      requirements: [{ ...requirement, reqKey: "notion" }],
    });
    expect(setRequirementService(keyed, requirement.uid, NOTION).mcps).toEqual([
      server,
      {
        ...second,
        oauthRequirementKey: "notion",
        url: NOTION.oauth_server_url,
      },
    ]);
  });
});

describe("plugin validation labels", () => {
  it("names entries exactly as validation errors do", () => {
    const skill = emptySkillForm();
    const unnamedMcp = emptyMcpForm();
    const namedMcp = { ...emptyMcpForm(), name: " Browser " };
    const unnamedRequirement = emptyRequirementForm();
    const keyedRequirement = { ...emptyRequirementForm(), reqKey: "token" };
    const errors = validatePluginForm(
      formWith({
        skills: [skill],
        mcps: [unnamedMcp, namedMcp],
        requirements: [unnamedRequirement, keyedRequirement],
      }),
    );

    expect(skillLabel(0)).toBe("Skill #1");
    expect(mcpLabel(namedMcp, 1)).toBe('MCP server "Browser"');
    expect(requirementLabel(keyedRequirement, 1)).toBe('Requirement "token"');
    for (const label of [
      skillLabel(0),
      mcpLabel(unnamedMcp, 0),
      mcpLabel(namedMcp, 1),
      requirementLabel(unnamedRequirement, 0),
      requirementLabel(keyedRequirement, 1),
    ]) {
      expect(entryErrors(errors, label).length, label).toBeGreaterThan(0);
    }
    expect(entryErrors(errors, skillLabel(0))).toContain("Name is required.");
    expect(generalErrors(errors)).toEqual(["Plugin name is required."]);
  });
});
