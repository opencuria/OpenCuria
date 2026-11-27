import {
  createLocalFiles,
  joinUri,
  type LocalFiles,
} from "../../platform/localFiles";

const FILE = "last-chats.json";
/** Apps remembered at most; the least recently used go first. */
const LIMIT = 50;

type Entries = Record<string, { sessionId: string; usedAt: number }>;

let files: LocalFiles | null = null;
let cache: Entries | null = null;

function store(): LocalFiles {
  files ??= createLocalFiles("opencuria-companion", "documents");
  return files;
}

async function load(): Promise<Entries> {
  if (cache) return cache;
  try {
    const uri = joinUri(store().root, FILE);
    cache = store().exists(uri)
      ? (JSON.parse(await store().readText(uri)) as Entries)
      : {};
  } catch {
    cache = {};
  }
  return cache;
}

function key(account: string, appId: string): string {
  return `${account}\n${appId}`;
}

/**
 * The chat the companion showed last for an app of an account, so it opens
 * there again; kept in OpenCuria's own documents, never in storage apps share.
 */
export async function lastCompanionChat(
  account: string,
  appId: string,
): Promise<string | null> {
  return (await load())[key(account, appId)]?.sessionId ?? null;
}

export async function rememberCompanionChat(
  account: string,
  appId: string,
  sessionId: string,
): Promise<void> {
  const entries = { ...(await load()) };
  entries[key(account, appId)] = { sessionId, usedAt: Date.now() };
  const kept = Object.fromEntries(
    Object.entries(entries)
      .sort(([, a], [, b]) => b.usedAt - a.usedAt)
      .slice(0, LIMIT),
  );
  cache = kept;
  try {
    store().makeDirectory(store().root);
    store().writeText(joinUri(store().root, FILE), JSON.stringify(kept));
  } catch {
    // Only a convenience: the companion then opens the newest chat.
  }
}
