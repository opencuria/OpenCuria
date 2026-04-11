/**
 * Shared test state persisted across spec files via JSON.
 */
import fs from 'fs';
import path from 'path';

const STATE_PATH = path.join(__dirname, '..', 'state', 'test-state.json');

export interface TestState {
  prefix: string;

  // Credential Services
  credentialServiceId?: string;
  credentialServiceSlug?: string;

  // Agent Definitions
  agentDefinitionId?: string;
  agentDuplicateId?: string;

  // Image Definitions
  imageDefinitionDockerId?: string;
  imageDefinitionQemuId?: string;
  imageDefinitionDockerBuildReady?: boolean;
  imageDefinitionQemuBuildReady?: boolean;
  qemuImageArtifactId?: string;

  // Credentials
  credentialEnvId?: string;
  credentialSshId?: string;
  credentialOrgId?: string;

  // Skills
  skillPersonalId?: string;
  skillOrgId?: string;

  // API Keys
  apiKeyId?: string;
  apiKeyValue?: string;

  // Workspaces
  workspaceDockerName?: string;
  workspaceDockerId?: string;
  workspaceQemuName?: string;
  workspaceQemuId?: string;

  // Images
  capturedImageDockerId?: string;
  clonedWorkspaceId?: string;
  clonedWorkspaceName?: string;

  // Runner info (discovered)
  dockerRunnerId?: string;
  qemuRunnerId?: string;
  qemuRunnerOnline?: boolean;
}

export function loadState(): TestState {
  try {
    const raw = fs.readFileSync(STATE_PATH, 'utf-8');
    const parsed = JSON.parse(raw);
    if (!parsed.prefix) {
      parsed.prefix = `e2e-${Math.floor(Date.now() / 1000)}`;
    }
    return parsed;
  } catch {
    return { prefix: `e2e-${Math.floor(Date.now() / 1000)}` };
  }
}

export function saveState(state: TestState): void {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

export function resetState(): void {
  const state: TestState = { prefix: `e2e-${Math.floor(Date.now() / 1000)}` };
  saveState(state);
}
