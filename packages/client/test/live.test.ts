import { afterAll, describe, expect, it } from "vitest";
import {
  createClient,
  FileTransferError,
  type FileFindResult,
  type TokenPair,
} from "../src/index";

interface LiveManifest {
  http_url: string;
  socket_url: string;
  email: string;
  org_id: string;
  primary_workspace_id: string;
  foreign_workspace_id: string;
  seeded_session_id: string;
  workspace_root: string;
}

const env =
  (
    globalThis as typeof globalThis & {
      process?: { env?: Record<string, string | undefined> };
    }
  ).process?.env ?? {};
const liveEnabled = env.MOBILE_CLIENT_LIVE === "1";
const manifestPath = env.MOBILE_CLIENT_MANIFEST;
const password = env.MOBILE_E2E_PASSWORD;
const live = describe.skipIf(!liveEnabled || !manifestPath || !password);

async function readManifest(path: string): Promise<LiveManifest> {
  const fs = await import("node:fs/promises");
  return JSON.parse(await fs.readFile(path, "utf8")) as LiveManifest;
}

live("real mobile E2E backend contract (opt-in)", () => {
  let client: ReturnType<typeof createClient> | undefined;
  let manifest: LiveManifest;
  let accessToken: string | null = null;
  let refreshToken: string | null = null;
  let organizationId: string | null = null;

  afterAll(() => client?.realtime.dispose());

  it("logs in, verifies workspace/conversation DTOs, harness timeline and actual file event shapes", async () => {
    manifest = await readManifest(manifestPath!);
    organizationId = manifest.org_id;
    client = createClient({
      apiBaseUrl: `${manifest.http_url.replace(/\/$/, "")}/api/v1`,
      wsBaseUrl: manifest.socket_url,
      getAccessToken: () => accessToken,
      getRefreshToken: () => refreshToken,
      setTokens: (tokens: TokenPair) => {
        accessToken = tokens.access_token;
        refreshToken = tokens.refresh_token;
      },
      getOrganizationId: () => organizationId,
      onAuthenticationExpired: () => {
        throw new Error("Fixture auth unexpectedly expired");
      },
      fileTransferTimeoutMs: 10_000,
    });

    await client.auth.login(manifest.email, password!);
    expect(await client.auth.me()).toMatchObject({ email: manifest.email });

    const workspaces = await client.workspaces.list();
    expect(
      workspaces.some(({ id }) => id === manifest.primary_workspace_id),
    ).toBe(true);
    const conversations = await client.conversations.list();
    expect(
      conversations.some(
        ({ session_id }) => session_id === manifest.seeded_session_id,
      ),
    ).toBe(true);
    const sessions = await client.harness.listSessions(
      manifest.primary_workspace_id,
    );
    expect(sessions.some(({ id }) => id === manifest.seeded_session_id)).toBe(
      true,
    );
    const timeline = await client.harness.timeline(manifest.seeded_session_id);
    expect(timeline.session.id).toBe(manifest.seeded_session_id);
    const detailPart = timeline.messages
      .flatMap(({ parts }) => parts)
      .find(({ detail_loaded }) => detail_loaded === false);
    if (detailPart)
      expect(
        await client.harness.getPart(manifest.seeded_session_id, detailPart.id),
      ).toMatchObject({ id: detailPart.id });

    client.realtime.connect();
    const release = client.realtime.subscribeWorkspace(
      manifest.primary_workspace_id,
    );
    try {
      await waitForConnected(client);
      const listed = await client.files.listFiles(
        manifest.primary_workspace_id,
        "/workspace",
      );
      expect(listed.entries.some(({ name }) => name === "README.md")).toBe(
        true,
      );

      const found: FileFindResult = await client.files.findFiles(
        manifest.primary_workspace_id,
        "hello.py",
      );
      expect(found.paths).toContain("/workspace/src/hello.py");

      const source = await client.files.readFile(
        manifest.primary_workspace_id,
        "/workspace/src/hello.py",
      );
      expect(source.size).toBeGreaterThan(0);
      expect(source.mime_type).toBeTruthy();
      expect(Buffer.from(source.content, "base64").toString("utf8")).toContain(
        "hello",
      );

      const uploadDirectory = "/workspace/.opencuria/user-uploaded";
      const filename = `shared-client-${Date.now()}.txt`;
      const bytes = Buffer.from("shared client real fixture upload", "utf8");
      const upload = await client.files.uploadFile(
        manifest.primary_workspace_id,
        {
          directory: uploadDirectory,
          filename,
          base64: bytes.toString("base64"),
        },
      );
      expect(upload.status).toBe("success");
      expect(upload.path).toBe(`${uploadDirectory}/${filename}`);

      const uploaded = await client.files.readFile(
        manifest.primary_workspace_id,
        upload.path,
      );
      expect(Buffer.from(uploaded.content, "base64")).toEqual(bytes);
      const downloaded = await client.files.downloadFile(
        manifest.primary_workspace_id,
        upload.path,
      );
      expect(downloaded.size).toBe(bytes.length);
      expect(Buffer.from(downloaded.content, "base64")).toEqual(bytes);

      await expect(
        client.files.listFiles(manifest.foreign_workspace_id, "/workspace"),
      ).rejects.toBeInstanceOf(FileTransferError);
    } finally {
      release();
    }
  }, 90_000);
});

async function waitForConnected(
  instance: ReturnType<typeof createClient>,
): Promise<void> {
  const deadline = Date.now() + 15_000;
  while (!instance.realtime.isConnected && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  expect(instance.realtime.isConnected).toBe(true);
}
