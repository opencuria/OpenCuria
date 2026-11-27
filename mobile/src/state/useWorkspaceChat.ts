import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { Alert } from "react-native";
import { KeyboardController } from "react-native-keyboard-controller";

import { appendUploadMentions } from "@opencuria/chat-core/chatUploads";
import { runNotice } from "@opencuria/chat-core/composerSheets";
import { messageRenderKey } from "@opencuria/chat-core/optimisticPrompts";
import {
  formatSendTime,
  sendLaterTaskInput,
} from "@opencuria/chat-core/sendLater";
import {
  filterMentionCandidates,
  mentionFileSearchQuery,
} from "@opencuria/chat-core/harnessMentions";
import { isWorkspaceReadyForChat } from "@opencuria/chat-core/workspaceReadiness";
import {
  harnessAgentDefaults,
  harnessSessionModels,
  isNewChatModelMissing,
  resolveCatalogModel,
  snapEffort,
} from "@opencuria/chat-core/harnessModels";
import type {
  HarnessId,
  HarnessMessage,
  HarnessSession,
  HarnessSessionMode,
} from "@opencuria/chat-core/harness";
import type {
  AgentConfig,
  HarnessConversation,
  HarnessEngine,
  ProviderModel,
  ScheduledTask,
  Skill,
  Workspace,
} from "@opencuria/client";
import type { ComposerAttachment } from "../components/ChatComposer";
import type { ComposerSubmission } from "../components/ChatComposer";
import type {
  ComposerNotice,
  ComposerQueue,
} from "../components/ComposerSheets";
import {
  modelSheetEngines,
  type ModelSheetEngine,
} from "../components/ModelSheet";
import { subagentHeader } from "../components/subagentHeader";
import type { ChatMediaResolver } from "../components/useWorkspaceMedia";
import { appScope } from "../apps/appPresentation";
import type { NotificationTarget } from "../notifications/target";
import { offlineStore } from "../offline/offlineStore";
import {
  SavedChatView,
  SavedChatWriter,
  conversationFromSession,
  savedConversations,
} from "../offline/savedChats";
import {
  MediaCache,
  clearPickedPhotoCache,
  uploadAttachments,
} from "../platform";
import { useSession } from "../screens/SessionContext";
import type { FileViewerTarget } from "../tools/files";
import { showToast } from "../ui/toast";
import { useVoiceDictation } from "../voice/useVoiceDictation";
import { ChatController, type ChatControllerSnapshot } from "./chatController";
import { useChatDraft } from "./useChatDraft";

export const HIDDEN_WORKSPACE_STATES = new Set([
  "removed",
  "deleted",
  "deleting",
  "pending_deletion",
]);
const EMPTY: ChatControllerSnapshot = {
  sessions: [],
  messages: [],
  todos: [],
  permissions: [],
  questions: [],
  steers: [],
  scheduled: [],
  activeSessionId: null,
  activeSession: null,
  activeSessionLineage: [],
  workspaceId: null,
  loading: false,
  error: null,
  connected: false,
  busy: false,
};
export const EMPTY_CHAT = EMPTY;
const noopSubscribe = (_listener: () => void) => () => undefined;
const emptySnapshot = () => EMPTY;

/** Sheets of the chat itself; screens add their own names. */
export type ChatSheet = "settings" | "rename" | "file";

export type ChatNavigationRequest = {
  revision: number;
  workspaceId: string;
  sessionId?: string;
  newChat?: boolean;
  notificationTarget?: NotificationTarget;
};

/** A sent prompt edited in the composer; the chat's own draft waits meanwhile. */
type PromptEdit = {
  messageId: string;
  /** The prompt's bubble; sending the edit shows the new text in it. */
  bubbleKey: string;
  sessionId: string;
  text: string;
  attachments: ComposerAttachment[];
  /** Sent and waiting for the server; a failure gives the text back. */
  sending?: boolean;
};

export interface WorkspaceChatOptions {
  /**
   * Keep the chat in this workspace: the app companion shows only the
   * workspace its app comes from. Without it, the chat may move between
   * workspaces and opens a usable one first.
   */
  workspaceId?: string;
  /**
   * With `workspaceId`: the chat that opens first, e.g. the last one used;
   * without one (or when it is gone) the workspace's newest chat.
   */
  initialSessionId?: string | null;
  /** Sheets that stay open offline; every other sheet closes. */
  offlineSheets?: ReadonlySet<string>;
  /** Shown over an open app (the companion): recordings keep its sounds. */
  overApp?: boolean;
  /** A pending request (a notification) owns navigation; retries wait for it. */
  navigationBlocked?: boolean;
  /** Retrying a stuck navigation also retries this screen's own requests. */
  onRetryNavigation?: () => void;
  /** A navigation starts without a notification target. */
  onNavigationRequest?: (request: ChatNavigationRequest) => void;
  /** A navigation completed. */
  onNavigated?: (request: ChatNavigationRequest) => void;
  /** Also refreshed when OpenCuria returns to the foreground. */
  onForegroundRefresh?: () => Promise<unknown>;
  /** "Send later" created a task (a new chat that starts later). */
  onScheduledTask?: (task: ScheduledTask) => void;
  /** Open Settings on a page, e.g. `["providers"]`. */
  onOpenSettings?: (path: string[]) => void;
}

/**
 * Everything one workspace chat needs, shared by OpenCuria's main screen and
 * the companion over an open app: lists, catalogs, the chat controller, the
 * draft, dictation, sending, editing, scheduling, gates and navigation.
 * Screens add their chrome (drawer or sheet) around `WorkspaceChatView`.
 */
export function useWorkspaceChat(options: WorkspaceChatOptions = {}) {
  const {
    client,
    session,
    user,
    offline,
    retry,
    registerResource,
    registerForegroundRefresh,
  } = useSession();
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const lockedWorkspaceId = options.workspaceId ?? null;
  const offlineRef = useRef(offline);
  offlineRef.current = offline;
  const controller = useMemo(
    () => (client ? new ChatController(client) : null),
    [client],
  );
  const subscribe = controller?.subscribe ?? noopSubscribe;
  const getSnapshot = controller?.getSnapshot ?? emptySnapshot;
  const liveChat = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  // The account's chats, lists and pictures saved for offline use.
  const accountScope = appScope(
    client?.api.baseUrl ?? "",
    session?.organizationId,
    user?.id,
  );
  const hasUser = Boolean(user);
  const saved = useMemo(
    () => (hasUser ? offlineStore().account(accountScope) : null),
    [hasUser, accountScope],
  );
  const savedRef = useRef(saved);
  savedRef.current = saved;
  const savedView = useMemo(() => new SavedChatView(saved), [saved]);
  const savedChat = useSyncExternalStore(
    savedView.subscribe,
    savedView.getSnapshot,
    savedView.getSnapshot,
  );
  // Offline, the chat on screen is the copy saved on this device.
  const chat = offline ? savedChat : liveChat;

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [conversations, setConversations] = useState<HarnessConversation[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [engines, setEngines] = useState<HarnessEngine[]>([]);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [engineModels, setEngineModels] = useState<
    Record<string, ProviderModel[]>
  >({});
  const [agentConfigs, setAgentConfigs] = useState<AgentConfig[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [recentModels, setRecentModels] = useState<
    Array<{ model: string; effort: string; last_used_at: string }>
  >([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [navigation, setNavigation] = useState<ChatNavigationRequest | null>(
    null,
  );
  const [completedNavigationRevision, setCompletedNavigationRevision] =
    useState(0);
  const navigationRevision = useRef(0);
  const [actionBusy, setActionBusy] = useState(false);
  const sendingRef = useRef(false);
  const workspaceRef = useRef<string | null>(null);
  const [sheet, setSheet] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [editing, setEditing] = useState<PromptEdit | null>(null);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [mode, setMode] = useState<HarnessSessionMode>("build");
  const [harness, setHarness] = useState<HarnessId>("native");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  // Each chat, new chat and engine keeps its own draft, as in the webapp.
  const draftScope = useMemo(
    () =>
      chat.workspaceId
        ? {
            workspaceId: chat.workspaceId,
            sessionId: chat.activeSessionId,
            harnessId: chat.activeSessionId
              ? (chat.activeSession?.harness_id ?? "native")
              : harness,
          }
        : null,
    [
      chat.workspaceId,
      chat.activeSessionId,
      chat.activeSession?.harness_id,
      harness,
    ],
  );
  const {
    draft,
    setDraft,
    followNext: followDraft,
    editDraft,
    flush: flushDrafts,
  } = useChatDraft(saved, draftScope);
  // Dictation's text goes to the draft of the chat it was recorded in.
  const voice = useVoiceDictation({
    client,
    account: hasUser ? accountScope : null,
    offline,
    scope: draftScope,
    editDraft,
    flushDrafts,
    onOpenSettings: () => optionsRef.current.onOpenSettings?.(["providers"]),
    mixWithOthers: options.overApp,
  });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** Why the workspace list could not load; cleared by the next success. */
  const [workspacesError, setWorkspacesError] = useState<string | null>(null);
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [resolving, setResolving] = useState<Set<string>>(new Set());
  const [file, setFile] = useState<FileViewerTarget | null>(null);
  const [offlineNoticeHidden, setOfflineNoticeHidden] = useState(false);
  const [noticeInset, setNoticeInset] = useState(0);
  const generation = useRef(0);
  const overviewGeneration = useRef(0);
  const catalogGeneration = useRef(0);
  const selectionDirty = useRef(false);
  /** The next workspace change carries the new chat's model choice along. */
  const keepSelection = useRef(false);
  const mountedRef = useRef(false);
  const clientRef = useRef(client);
  const loadOverviewRef = useRef<(background?: boolean) => Promise<void>>(
    async () => undefined,
  );
  const loadCatalogsRef = useRef<() => Promise<void>>(async () => undefined);
  const searchGeneration = useRef(0);
  const selectedWorkspaceRef = workspaceRef;
  selectedWorkspaceRef.current = workspaceId;
  const resolveCounts = useRef(new Map<string, number>());

  const workspace = workspaces.find((item) => item.id === workspaceId) ?? null;
  const isTransitioning = Boolean(
    workspace?.active_operation || workspace?.intervention_required,
  );
  const ready =
    !offline &&
    Boolean(workspace && isWorkspaceReadyForChat(workspace, isTransitioning));
  const catalogForEngine = (id: HarnessId) =>
    id === "native" ? models : (engineModels[id] ?? []);
  const modelsForEngine = harnessSessionModels(
    catalogForEngine(harness),
    harness,
    chat.activeSessionId ? (chat.activeSession?.model ?? "") : undefined,
  );
  const selectedModel = resolveCatalogModel(modelsForEngine, model);
  const contextKey = `${client?.api.baseUrl ?? ""}:${session?.organizationId ?? ""}:${workspaceId ?? ""}:${chat.activeSessionId ?? "new"}`;
  useEffect(() => {
    setFile(null);
    setSheet((current) => (current === "file" ? null : current));
  }, [contextKey]);
  const activeIsSubtask = Boolean(chat.activeSession?.parent_id);
  const navigationReady = Boolean(
    workspaceId &&
    navigation?.workspaceId === workspaceId &&
    navigation.revision === navigationRevision.current &&
    navigation.revision === completedNavigationRevision &&
    chat.workspaceId === workspaceId &&
    !chat.loading,
  );
  /** The chat cannot take a message, as opposed to one being on its way. */
  const chatUnavailable = !navigationReady || !ready || activeIsSubtask;
  /** A new chat cannot start without a model; the server would refuse it. */
  const missingModel =
    !offline && !chat.activeSessionId && isNewChatModelMissing(harness, model);
  /** No usable workspace yet: the empty chat shows the first-run guide. */
  const needsSetup =
    !lockedWorkspaceId &&
    !offline &&
    !loading &&
    !workspacesError &&
    workspaces.every((item) => HIDDEN_WORKSPACE_STATES.has(item.status));
  /** The agent is working: a message goes into its running turn. */
  const steering =
    navigationReady &&
    !activeIsSubtask &&
    chat.activeSession?.status === "busy";
  const runOptions = () => ({
    harnessId: harness,
    mode,
    model,
    reasoningEffort: effort,
    skillIds,
  });
  /**
   * The prompt edit on screen. It ends with its chat, offline, or once its
   * bubble is gone, e.g. edited elsewhere; sent from here, it keeps the bubble.
   */
  const activeEdit =
    editing &&
    !offline &&
    editing.sessionId === chat.activeSessionId &&
    (!navigationReady ||
      chat.messages.some(
        (message) => messageRenderKey(message) === editing.bubbleKey,
      ))
      ? editing
      : null;
  useEffect(() => {
    if (editing && !activeEdit) setEditing(null);
  }, [activeEdit, editing]);
  /** Text typed into the composer: the edit on screen or the chat's draft. */
  const setComposerText = (update: string | ((current: string) => string)) => {
    if (!activeEdit) return setDraft(update);
    setEditing(
      (current) =>
        current && {
          ...current,
          text: typeof update === "function" ? update(current.text) : update,
        },
    );
  };
  const setComposerAttachments = (files: ComposerAttachment[]) => {
    if (!activeEdit) return setAttachments(files);
    setEditing((current) => current && { ...current, attachments: files });
  };

  useEffect(() => {
    if (!controller) return;
    const dispose = () => controller.dispose();
    const unregister = registerResource(dispose);
    return () => {
      unregister();
      dispose();
    };
  }, [controller, registerResource]);

  const media = useMemo(
    () =>
      client && workspaceId
        ? new MediaCache(
            client,
            workspaceId,
            `${client.api.baseUrl}:${session?.organizationId ?? ""}`,
          )
        : null,
    [client, workspaceId, session?.organizationId],
  );
  useEffect(() => {
    if (!media) return;
    const dispose = () => {
      void media.dispose();
      clearPickedPhotoCache();
    };
    const unregister = registerResource(dispose);
    return () => {
      unregister();
      dispose();
    };
  }, [media, registerResource]);

  const activeSessionRef = useRef<string | null>(null);
  activeSessionRef.current = chat.activeSessionId;
  /**
   * Pictures come from the workspace and are saved with the chat; offline,
   * or while the workspace is stopped, the saved copy shows. Large media wait
   * for a tap (`describe`).
   */
  const resolveMedia = useMemo<ChatMediaResolver>(() => {
    /** Finds the copy saved with the chat shown when it was called. */
    const savedCopy = (path: string) => {
      const sessionId = activeSessionRef.current;
      const account = savedRef.current;
      return () =>
        sessionId && account
          ? account.media(sessionId, path).catch(() => null)
          : Promise.resolve(null);
    };
    const resolve = async (path: string) => {
      const sessionId = activeSessionRef.current;
      const account = savedRef.current;
      const saved = savedCopy(path);
      if (offlineRef.current) {
        const uri = await saved();
        if (uri) return uri;
        throw Object.assign(
          new Error("This picture is not saved on this device."),
          { retryable: false },
        );
      }
      if (!media) throw new Error("No workspace selected");
      try {
        const pinned = await media.resolvePinned(path);
        if (sessionId && account)
          void account
            .saveMedia(sessionId, path, pinned.uri)
            .catch(() => undefined);
        return pinned;
      } catch (cause) {
        const uri = await saved();
        if (uri) return uri;
        throw cause;
      }
    };
    // Without the workspace, saved copies show as they are.
    const describe = async (path: string) => {
      const saved = savedCopy(path);
      if (offlineRef.current) return null;
      if (!media) throw new Error("No workspace selected");
      try {
        return await media.describe(path);
      } catch (cause) {
        if (await saved()) return null;
        throw cause;
      }
    };
    return Object.assign(resolve, { describe });
  }, [media]);

  const loadOverview = useCallback(
    async (background = false) => {
      if (!client || !mountedRef.current || clientRef.current !== client)
        return;
      const requestGeneration = ++overviewGeneration.current;
      if (!background) setLoading(true);
      try {
        if (offlineRef.current) {
          await loadSavedOverview(requestGeneration);
          return;
        }
        const [ws, rows, loadedSkills] = await Promise.all([
          client.workspaces.list(),
          client.conversations.list(),
          client.skills.list(),
        ]);
        if (
          !mountedRef.current ||
          clientRef.current !== client ||
          requestGeneration !== overviewGeneration.current
        )
          return;
        setWorkspaces(ws);
        setWorkspacesError(null);
        setConversations(rows);
        setSkills(loadedSkills);
        void savedRef.current?.saveOverview(ws, rows).catch(() => undefined);
        const chosen = selectedWorkspaceRef.current;
        const locked = optionsRef.current.workspaceId;
        let nextId: string | null;
        if (locked) {
          nextId = ws.some(
            (item) =>
              item.id === locked && !HIDDEN_WORKSPACE_STATES.has(item.status),
          )
            ? locked
            : null;
        } else {
          const fallback =
            ws.find(
              (item) =>
                isWorkspaceReadyForChat(
                  item,
                  Boolean(item.active_operation || item.intervention_required),
                ) && !item.has_active_session,
            ) ?? ws.find((item) => !HIDDEN_WORKSPACE_STATES.has(item.status));
          nextId =
            chosen && ws.some((item) => item.id === chosen)
              ? chosen
              : (fallback?.id ?? null);
        }
        if (chosen !== nextId) {
          selectedWorkspaceRef.current = nextId;
          setWorkspaceId(nextId);
          if (nextId) {
            const selectedWorkspace = ws.find((item) => item.id === nextId)!;
            const revision = ++navigationRevision.current;
            // A locked chat opens the chat asked for, else the newest one.
            const workspaceRows = locked
              ? rows
                  .filter((row) => row.workspace_id === nextId)
                  .sort((a, b) =>
                    b.last_message_at.localeCompare(a.last_message_at),
                  )
              : [];
            const initialSession = locked
              ? (workspaceRows.find(
                  (row) =>
                    row.session_id === optionsRef.current.initialSessionId,
                )?.session_id ?? workspaceRows[0]?.session_id)
              : undefined;
            setNavigation(
              initialSession
                ? { revision, workspaceId: nextId, sessionId: initialSession }
                : {
                    revision,
                    workspaceId: nextId,
                    newChat: isWorkspaceReadyForChat(
                      selectedWorkspace,
                      Boolean(
                        selectedWorkspace.active_operation ||
                        selectedWorkspace.intervention_required,
                      ),
                    ),
                  },
            );
          } else {
            navigationRevision.current += 1;
            setNavigation(null);
          }
        }
        setError(
          locked && !nextId
            ? "The workspace of this app is no longer available."
            : null,
        );
      } catch (cause) {
        if (
          mountedRef.current &&
          requestGeneration === overviewGeneration.current
        ) {
          setError(errorMessage(cause, "Could not load workspace data."));
          setWorkspacesError(
            errorMessage(cause, "Check your connection and try again."),
          );
        }
      } finally {
        if (
          mountedRef.current &&
          clientRef.current === client &&
          requestGeneration === overviewGeneration.current
        )
          // The latest request owns completion, even when a background refresh
          // superseded the initial load before it could clear the spinner.
          setLoading(false);
      }
    },
    [client],
  );
  loadOverviewRef.current = loadOverview;

  /** Offline: the saved lists, and the chat opened last when none is open. */
  async function loadSavedOverview(requestGeneration: number): Promise<void> {
    const account = savedRef.current;
    const [overview, chats] = await Promise.all([
      account?.loadOverview().catch(() => null) ?? null,
      account?.chats().catch(() => []) ?? [],
    ]);
    if (
      !mountedRef.current ||
      requestGeneration !== overviewGeneration.current ||
      !offlineRef.current
    )
      return;
    const fresher = new Map(
      (overview?.conversations ?? []).map((row) => [row.session_id, row]),
    );
    setWorkspaces(overview?.workspaces ?? []);
    setWorkspacesError(null);
    setConversations(
      savedConversations(
        chats.map((chat) => ({
          ...chat,
          conversation: chat.conversation
            ? (fresher.get(chat.sessionId) ?? chat.conversation)
            : null,
        })),
      ),
    );
    const locked = optionsRef.current.workspaceId;
    const latest = chats.find(
      (chat) => chat.conversation && (!locked || chat.workspaceId === locked),
    );
    if (!selectedWorkspaceRef.current && latest) {
      selectedWorkspaceRef.current = latest.workspaceId;
      setWorkspaceId(latest.workspaceId);
      setNavigation({
        revision: ++navigationRevision.current,
        workspaceId: latest.workspaceId,
        sessionId: latest.sessionId,
      });
    }
    setError(null);
  }

  const loadCatalogs = useCallback(async () => {
    if (
      !client ||
      !mountedRef.current ||
      clientRef.current !== client ||
      offlineRef.current
    )
      return;
    const requestGeneration = ++catalogGeneration.current;
    const engineRequest = client.harness.listEngines();
    const results = await Promise.allSettled([
      engineRequest,
      client.harness.listEngineModels("native"),
      engineRequest.then(async (rows) => {
        const external = rows.filter((engine) => engine.id !== "native");
        const results = await Promise.allSettled(
          external.map(async (engine) => {
            const entries = await client.harness.listEngineModels(engine.id);
            return [engine.id, entries] as const;
          }),
        );
        return {
          catalogs: Object.fromEntries(
            results.flatMap((result) =>
              result.status === "fulfilled" ? [result.value] : [],
            ),
          ),
          failed: results.some((result) => result.status === "rejected"),
        };
      }),
      client.harness.listAgentConfigs(),
      client.harness.listRecentModels(),
    ]);
    if (
      !mountedRef.current ||
      clientRef.current !== client ||
      requestGeneration !== catalogGeneration.current
    )
      return;
    const [
      engineResult,
      modelResult,
      externalResult,
      configResult,
      recentResult,
    ] = results;
    setEngines(engineResult.status === "fulfilled" ? engineResult.value : []);
    setModels(modelResult.status === "fulfilled" ? modelResult.value : []);
    setEngineModels(
      externalResult.status === "fulfilled"
        ? externalResult.value.catalogs
        : {},
    );
    setAgentConfigs(
      configResult.status === "fulfilled" ? configResult.value : [],
    );
    setRecentModels(
      recentResult.status === "fulfilled" ? recentResult.value : [],
    );
    setCatalogError(
      results.some((result) => result.status === "rejected") ||
        (externalResult.status === "fulfilled" && externalResult.value.failed)
        ? "Some model settings are unavailable. Workspace chat and history are still available."
        : null,
    );
  }, [client]);
  loadCatalogsRef.current = loadCatalogs;
  /** Settings changed providers, models or agent defaults. */
  const reloadCatalogs = useCallback(() => {
    void loadCatalogs();
  }, [loadCatalogs]);
  const refreshWorkspaces = useCallback(
    () => loadOverview(true),
    [loadOverview],
  );

  useEffect(() => {
    mountedRef.current = true;
    clientRef.current = client;
    overviewGeneration.current += 1;
    catalogGeneration.current += 1;
    generation.current += 1;
    selectedWorkspaceRef.current = null;
    setWorkspaceId(null);
    setNavigation(null);
    setAttachments([]);
    setSkillIds([]);
    setConversations([]);
    setWorkspaces([]);
    setWorkspacesError(null);
    setCatalogError(null);
    setEngines([]);
    setModels([]);
    setEngineModels({});
    selectionDirty.current = false;
    if (client) {
      void loadOverview();
      void loadCatalogs();
    } else {
      setLoading(false);
    }
    return () => {
      mountedRef.current = false;
      if (clientRef.current === client) clientRef.current = null;
      overviewGeneration.current += 1;
      catalogGeneration.current += 1;
      generation.current += 1;
    };
  }, [client, loadOverview, loadCatalogs]);

  const retryPendingNavigation = useCallback(() => {
    if (
      navigation &&
      navigation.revision === navigationRevision.current &&
      navigation.revision !== completedNavigationRevision &&
      !optionsRef.current.navigationBlocked
    ) {
      const revision = ++navigationRevision.current;
      setNavigation({ ...navigation, revision });
    }
    optionsRef.current.onRetryNavigation?.();
  }, [navigation, completedNavigationRevision]);

  useEffect(
    () =>
      registerForegroundRefresh(async () => {
        // Offline, the session reconnects first; going online reloads.
        if (!client || !controller || offlineRef.current) return;
        await Promise.allSettled([
          loadOverview(true),
          controller.refresh(),
          loadCatalogs(),
          optionsRef.current.onForegroundRefresh?.(),
        ]);
        retryPendingNavigation();
      }),
    [
      client,
      controller,
      loadOverview,
      loadCatalogs,
      registerForegroundRefresh,
      retryPendingNavigation,
    ],
  );

  const workspaceScopeKey = lockedWorkspaceId
    ? workspaces.some((item) => item.id === lockedWorkspaceId)
      ? lockedWorkspaceId
      : ""
    : workspaces
        .map((item) => item.id)
        .sort()
        .join("\0");
  useEffect(() => {
    if (!client) return;
    const releases = workspaceScopeKey
      ? workspaceScopeKey
          .split("\0")
          .map((id) => client.realtime.subscribeWorkspace(id))
      : [];
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        void loadOverviewRef.current(true);
      }, 180);
    };
    const onWorkspaceError = (event: {
      workspace_id: string;
      error: string;
    }) => {
      if (event.workspace_id === selectedWorkspaceRef.current)
        setError(event.error);
      schedule();
    };
    const detach = [
      client.realtime.on("harness.conversations_changed", schedule),
      client.realtime.on("workspace:status_changed", schedule),
      client.realtime.on("workspace:operation_changed", schedule),
      client.realtime.on("runner:online", schedule),
      client.realtime.on("runner:offline", schedule),
      client.realtime.on("workspace:error", onWorkspaceError),
      client.realtime.onReconnect(() => {
        schedule();
        void loadCatalogsRef.current();
      }),
    ];
    return () => {
      if (timer) clearTimeout(timer);
      detach.forEach((off) => off());
      releases.forEach((release) => release());
    };
  }, [client, workspaceScopeKey, loadCatalogs]);

  useEffect(() => {
    if (!navigation || (!offline && !controller)) return;
    let alive = true;
    const request = navigation;
    const run = offline
      ? savedView.open(
          request.workspaceId,
          request.newChat ? undefined : request.sessionId,
        )
      : request.newChat
        ? controller!.newChat(request.workspaceId)
        : controller!.openWorkspace(request.workspaceId, request.sessionId);
    void run
      .then(() => {
        if (alive && request.revision === navigationRevision.current) {
          setCompletedNavigationRevision(request.revision);
          setError(null);
          optionsRef.current.onNavigated?.(request);
        }
      })
      .catch((cause: unknown) => {
        if (alive && request.revision === navigationRevision.current)
          setError(errorMessage(cause, "Could not load conversations."));
      });
    return () => {
      alive = false;
    };
  }, [controller, navigation, offline, savedView]);

  // Going offline or back online moves the open chat to the other source
  // (the copy saved on this device or the server) and reloads the lists.
  const previousOffline = useRef(offline);
  useEffect(() => {
    if (previousOffline.current === offline) return;
    previousOffline.current = offline;
    setOfflineNoticeHidden(false);
    setError(null);
    if (offline) {
      setFile(null);
      const keep = optionsRef.current.offlineSheets;
      setSheet((current) => (current && !keep?.has(current) ? null : current));
    }
    void loadOverviewRef.current(true);
    if (!offline) void loadCatalogsRef.current();
  }, [offline]);

  // The chat being read is saved, so it opens offline later.
  const conversationsRef = useRef(conversations);
  conversationsRef.current = conversations;
  const workspacesRef = useRef(workspaces);
  workspacesRef.current = workspaces;
  useEffect(() => {
    if (!controller || !saved || offline) return;
    const writer = new SavedChatWriter(
      controller,
      saved,
      (session) =>
        conversationsRef.current.find((row) => row.session_id === session.id) ??
        conversationFromSession(
          session,
          workspacesRef.current.find((item) => item.id === session.workspace_id)
            ?.name ?? "",
        ),
    );
    return writer.start();
  }, [controller, saved, offline]);

  useEffect(() => {
    const keep = keepSelection.current;
    keepSelection.current = false;
    if (keep && !chat.activeSessionId) return;
    selectionDirty.current = false;
    const selected = chat.activeSession;
    if (selected) {
      setMode(selected.mode);
      setHarness(selected.harness_id ?? "native");
      setModel(selected.model ?? "");
      setEffort(selected.reasoning_effort ?? "");
      setSkillIds(selected.skill_ids ?? []);
    } else {
      setModel("");
      setEffort("");
    }
  }, [
    chat.activeSessionId,
    workspaceId,
    chat.activeSession?.model,
    chat.activeSession?.reasoning_effort,
    chat.activeSession?.mode,
    chat.activeSession?.harness_id,
  ]);

  useEffect(() => {
    if (chat.activeSessionId || selectionDirty.current) return;
    const defaults = harnessAgentDefaults(harness, mode, {
      engines,
      agentConfigs,
      models: catalogForEngine(harness),
    });
    setModel(defaults.model);
    setEffort(defaults.effort);
  }, [
    chat.activeSessionId,
    workspaceId,
    agentConfigs,
    harness,
    engineModels,
    models,
    engines,
    mode,
  ]);

  const refresh = useCallback(async () => {
    if (!client) return;
    if (offlineRef.current) {
      await retry();
      return;
    }
    await Promise.allSettled([loadOverview(true), controller?.refresh()]);
    retryPendingNavigation();
  }, [client, controller, loadOverview, retry, retryPendingNavigation]);

  const selectMode = (next: HarnessSessionMode) => {
    setMode(next);
    if (harness !== "native" || selectionDirty.current) return;
    const config = agentConfigs.find((item) => item.agent === next);
    const nextModel =
      modelsForEngine.find((item) => item.id === config?.model) ??
      modelsForEngine[0];
    setModel(nextModel?.id ?? "");
    setEffort(snapEffort(nextModel, config?.effort ?? ""));
  };
  /** An agent is an engine and mode pair, as in the webapp composer. */
  const selectAgent = (next: HarnessId, nextMode: HarnessSessionMode) => {
    if (next === harness) {
      selectMode(nextMode);
      return;
    }
    if (
      chat.activeSessionId ||
      (next !== "native" &&
        !engines.find((engine) => engine.id === next)?.connected)
    )
      return;
    selectionDirty.current = false;
    // The draft on screen stays, as in the webapp.
    followDraft(
      (from, to) => !from.sessionId && !to.sessionId && to.harnessId === next,
    );
    setHarness(next);
    setMode(nextMode);
    const defaults = harnessAgentDefaults(next, nextMode, {
      engines,
      agentConfigs,
      models: catalogForEngine(next),
    });
    setModel(defaults.model);
    setEffort(defaults.effort);
  };
  const selectModel = (next: string) => {
    if (!modelsForEngine.some((item) => item.id === next)) return;
    selectionDirty.current = true;
    setModel(next);
    setEffort(snapEffort(resolveCatalogModel(modelsForEngine, next), effort));
  };
  const selectEffort = (value: string) => {
    selectionDirty.current = true;
    setEffort(value);
  };

  const searchMentions = useCallback(
    async (query: string) => {
      const request = ++searchGeneration.current;
      const selectedId = selectedWorkspaceRef.current;
      if (!client || !selectedId) return filterMentionCandidates(query, []);
      const fileQuery = mentionFileSearchQuery(query);
      if (fileQuery === null) return filterMentionCandidates(query, []);
      try {
        const result = await client.files.findFiles(selectedId, fileQuery);
        if (
          request !== searchGeneration.current ||
          selectedId !== selectedWorkspaceRef.current
        )
          return [];
        const paths = result.error
          ? []
          : result.paths.filter((path) => path.startsWith("/workspace/"));
        return filterMentionCandidates(query, paths);
      } catch {
        return request === searchGeneration.current
          ? filterMentionCandidates(query, [])
          : [];
      }
    },
    [client],
  );

  const send = useCallback(
    async ({
      text,
      skillIds: usedSkills,
      attachments: files,
    }: {
      text: string;
      skillIds: string[];
      attachments: ComposerAttachment[];
    }) => {
      if (sendingRef.current) return;
      if (
        !client ||
        !controller ||
        !workspace ||
        !navigationReady ||
        !ready ||
        activeIsSubtask
      )
        throw new Error(
          "Choose an available, running workspace before sending a message.",
        );
      sendingRef.current = true;
      const ownerGeneration = generation.current;
      const ownerWorkspace = workspace.id;
      const ownerSession = chat.activeSessionId;
      const submittedDraft = draft;
      const options = {
        harnessId: harness,
        mode,
        model,
        reasoningEffort: effort,
        skillIds: usedSkills,
      };
      // The text shows while attachments upload; their mentions follow. A
      // steer goes to the queue instead.
      const dropPreview =
        files.length &&
        controller.getSnapshot().activeSession?.status !== "busy"
          ? controller.previewPrompt(text, options)
          : undefined;
      // What is typed while a new chat is created stays with that chat.
      const stopFollowing = ownerSession
        ? undefined
        : followDraft(
            (from, to) =>
              !from.sessionId &&
              Boolean(to.sessionId) &&
              to.workspaceId === ownerWorkspace,
          );
      try {
        const paths = files.length
          ? await uploadAttachments(client, ownerWorkspace, files)
          : [];
        if (
          ownerGeneration !== generation.current ||
          selectedWorkspaceRef.current !== ownerWorkspace ||
          controller.getSnapshot().activeSessionId !== ownerSession
        ) {
          throw new Error(
            "The workspace or conversation changed before sending. Please try again.",
          );
        }
        const prompt = appendUploadMentions(text, paths);
        if (!prompt.trim())
          throw new Error("Attach a file or enter a message first.");
        // A running agent receives the message in its current turn.
        if (controller.getSnapshot().activeSession?.status === "busy") {
          await controller.steer(prompt, options);
          if (
            ownerGeneration === generation.current &&
            selectedWorkspaceRef.current === ownerWorkspace
          ) {
            setDraft((current) =>
              current === submittedDraft || current.trim() === text
                ? ""
                : current,
            );
            setAttachments([]);
            setError(null);
          }
          return;
        }
        const accepted = await controller.send(prompt, options);
        if (
          ownerGeneration !== generation.current ||
          selectedWorkspaceRef.current !== ownerWorkspace
        )
          return;
        setDraft((current) =>
          current === submittedDraft || current.trim() === text ? "" : current,
        );
        setAttachments([]);
        setError(null);
        setMode(accepted.mode ?? mode);
        await Promise.allSettled([
          client.harness.saveRecentModel(model, effort),
          loadOverview(true),
        ]);
      } finally {
        // Sent prompts were taken over; this only takes back an unsent one.
        dropPreview?.();
        stopFollowing?.();
        sendingRef.current = false;
      }
    },
    [
      client,
      controller,
      workspace,
      ready,
      activeIsSubtask,
      chat.activeSessionId,
      harness,
      mode,
      model,
      effort,
      draft,
      setDraft,
      followDraft,
      loadOverview,
      navigationReady,
    ],
  );

  /** Moves a sent prompt into the composer; the transcript ends with it. */
  const startEdit = (message: HarnessMessage) => {
    if (!chat.activeSessionId) return;
    setEditing({
      messageId: message.id,
      bubbleKey: messageRenderKey(message),
      sessionId: chat.activeSessionId,
      text: message.content,
      attachments: [],
    });
  };

  /** Sends the edited prompt: the chat restarts from it. */
  const submitEdit = async ({
    text,
    skillIds: usedSkills,
    attachments: files,
  }: ComposerSubmission) => {
    const target = activeEdit;
    if (!client || !controller || !workspace || !target || chatUnavailable)
      throw new Error(
        "Choose an available, running workspace before editing a message.",
      );
    const markSending = (sending: boolean) =>
      setEditing((current) =>
        current?.messageId === target.messageId
          ? { ...current, sending }
          : current,
      );
    markSending(true);
    try {
      const ownerGeneration = generation.current;
      const paths = files.length
        ? await uploadAttachments(client, workspace.id, files)
        : [];
      if (
        ownerGeneration !== generation.current ||
        controller.getSnapshot().activeSessionId !== target.sessionId
      )
        throw new Error(
          "The conversation changed before sending. Please try again.",
        );
      const prompt = appendUploadMentions(text, paths);
      if (!prompt.trim())
        throw new Error("Attach a file or enter a message first.");
      await controller.edit(
        target.messageId,
        prompt,
        { ...runOptions(), skillIds: usedSkills },
        target.sessionId,
      );
    } catch (error) {
      // The composer puts the text back into this edit.
      markSending(false);
      throw error;
    }
    setEditing((current) =>
      current?.messageId === target.messageId ? null : current,
    );
    setError(null);
    await Promise.allSettled([
      client.harness.saveRecentModel(model, effort),
      loadOverview(true),
    ]);
  };

  /**
   * "Send later": an open chat gets a scheduled message; a new chat becomes a
   * one-time scheduled task that starts it.
   */
  const scheduleDraft = useCallback(
    async (
      { text, skillIds: usedSkills, attachments: files }: ComposerSubmission,
      sendAt: Date,
    ) => {
      if (
        !client ||
        !controller ||
        !workspace ||
        !navigationReady ||
        !ready ||
        activeIsSubtask
      )
        throw new Error(
          "Choose an available, running workspace before scheduling a message.",
        );
      const ownerGeneration = generation.current;
      const ownerWorkspace = workspace.id;
      const ownerSession = chat.activeSessionId;
      const paths = files.length
        ? await uploadAttachments(client, ownerWorkspace, files)
        : [];
      if (
        ownerGeneration !== generation.current ||
        selectedWorkspaceRef.current !== ownerWorkspace ||
        controller.getSnapshot().activeSessionId !== ownerSession
      )
        throw new Error(
          "The workspace or conversation changed before scheduling. Please try again.",
        );
      const prompt = appendUploadMentions(text, paths);
      if (!prompt.trim())
        throw new Error("Attach a file or enter a message first.");
      if (ownerSession) {
        await controller.schedule(prompt, sendAt, {
          harnessId: harness,
          mode,
          model,
          reasoningEffort: effort,
          skillIds: usedSkills,
        });
      } else {
        const task = await client.scheduledTasks.create(
          sendLaterTaskInput({
            workspaceId: ownerWorkspace,
            prompt,
            at: sendAt,
            mode,
            harnessId: harness,
            model,
            reasoningEffort: effort,
            skillIds: usedSkills,
          }),
        );
        optionsRef.current.onScheduledTask?.(task);
        showToast({
          title: "New chat scheduled",
          message: formatSendTime(sendAt),
          tone: "success",
        });
      }
      if (ownerGeneration === generation.current) {
        setAttachments([]);
        setError(null);
      }
    },
    [
      client,
      controller,
      workspace,
      ready,
      activeIsSubtask,
      chat.activeSessionId,
      harness,
      mode,
      model,
      effort,
      navigationReady,
    ],
  );

  /** Puts withdrawn text back below what the user is typing. */
  const restoreToDraft = useCallback(
    (texts: string[], skills?: string[]) => {
      const restored = texts.filter((text) => text.trim()).join("\n\n");
      if (restored)
        setDraft((current) =>
          current.trim()
            ? `${current.replace(/\s+$/, "")}\n\n${restored}`
            : restored,
        );
      if (skills?.length)
        setSkillIds((current) => [
          ...current,
          ...skills.filter((id) => !current.includes(id)),
        ]);
    },
    // `setDraft` is stable for the chat on screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  /** Runs an outgoing-message action; a failure becomes a toast. */
  const queueAction = useCallback(
    async (title: string, action: () => Promise<void>) => {
      try {
        await action();
      } catch (cause) {
        showToast({
          title,
          message: errorMessage(cause, "Please try again."),
          tone: "error",
        });
      }
    },
    [],
  );

  const outgoing: ComposerQueue | undefined =
    controller && navigationReady && !offline
      ? {
          steers: chat.steers,
          scheduled: chat.scheduled,
          canSendNow: !chatUnavailable,
          onWithdrawSteer: (id) =>
            queueAction("Couldn’t withdraw the message", async () => {
              const text = await controller.withdrawSteer(id);
              if (text) restoreToDraft([text]);
            }),
          onSendNow: (id) =>
            queueAction("Couldn’t send the message", () =>
              controller.sendScheduledNow(id),
            ),
          onReschedule: (id, sendAt) =>
            queueAction("Couldn’t change the time", () =>
              controller.reschedule(id, sendAt),
            ),
          onEdit: (id) =>
            queueAction("Couldn’t edit the message", async () => {
              const message = await controller.withdrawScheduled(id);
              if (message) restoreToDraft([message.content], message.skill_ids);
            }),
          onDelete: (id) =>
            queueAction("Couldn’t delete the message", async () => {
              await controller.withdrawScheduled(id);
            }),
        }
      : undefined;
  // Failed and stopped runs show above the composer, as in the webapp.
  const runNoticeState = useMemo(
    () => runNotice(chat.messages),
    [chat.messages],
  );
  const notice: ComposerNotice | undefined =
    controller && navigationReady && !offline
      ? {
          notice: runNoticeState,
          onDismiss: (id) => controller.dismissNotice(id),
        }
      : undefined;

  const requestNavigation = (
    workspaceId: string,
    options: {
      sessionId?: string;
      newChat?: boolean;
      notificationTarget?: NotificationTarget;
    } = {},
  ) => {
    // The chat opened next shows its own draft.
    followDraft(null);
    setEditing(null);
    const revision = ++navigationRevision.current;
    generation.current += 1;
    selectedWorkspaceRef.current = workspaceId;
    setWorkspaceId(workspaceId);
    const request = { revision, workspaceId, ...options };
    optionsRef.current.onNavigationRequest?.(request);
    setNavigation(request);
  };

  /** Opens a chat of the lists; screens check that its workspace exists. */
  const openConversation = (row: HarnessConversation) => {
    setAttachments([]);
    setSheet(null);
    requestNavigation(row.workspace_id, { sessionId: row.session_id });
  };

  const openWorkspace = (chosen: Workspace) => {
    setError(null);
    const isReady = isWorkspaceReadyForChat(
      chosen,
      Boolean(chosen.active_operation || chosen.intervention_required),
    );
    requestNavigation(chosen.id, {
      newChat: isReady,
    });
  };

  const chooseWorkspace = (chosen: Workspace) => {
    setAttachments([]);
    setSkillIds([]);
    setSheet(null);
    openWorkspace(chosen);
  };

  /** A new chat keeps its draft, attachments, skills and model in another workspace. */
  const moveNewChat = (id: string) => {
    const chosen = workspaces.find((item) => item.id === id);
    if (!chosen || chosen.id === workspaceId) return;
    keepSelection.current = true;
    openWorkspace(chosen);
    followDraft(
      (from, to) =>
        !from.sessionId && !to.sessionId && to.workspaceId === chosen.id,
    );
  };

  const newChat = () => {
    if (!workspace || !ready) return;
    setAttachments([]);
    setSkillIds([]);
    setError(null);
    setSheet(null);
    requestNavigation(workspace.id, { newChat: true });
  };

  const gateAction = async (id: string, action: () => Promise<void>) => {
    if (resolveCounts.current.has(id)) return;
    const count = 1;
    resolveCounts.current.set(id, count);
    setResolving((previous) => new Set(previous).add(id));
    try {
      await action();
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not resolve this request."));
      throw cause;
    } finally {
      const remaining = (resolveCounts.current.get(id) ?? 1) - 1;
      if (remaining > 0) resolveCounts.current.set(id, remaining);
      else {
        resolveCounts.current.delete(id);
        setResolving((previous) => {
          const next = new Set(previous);
          next.delete(id);
          return next;
        });
      }
    }
  };

  const rename = (item: HarnessConversation | HarnessSession) => {
    setRenameId("id" in item ? item.id : item.session_id);
    setRenameText(item.title);
    setSheet("rename");
  };
  const saveRename = async () => {
    if (!controller || !renameId || !renameText.trim()) return;
    setRenaming(true);
    try {
      await controller.rename(renameId, renameText.trim());
      await loadOverview(true);
      setSheet(null);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not rename conversation."));
    } finally {
      setRenaming(false);
    }
  };

  const deleteConversation = async (conversation: HarnessConversation) => {
    if (!client || actionBusy) return;
    setActionBusy(true);
    try {
      await client.harness.deleteSession(conversation.session_id);
      if (conversation.workspace_id === workspaceId)
        await controller?.refresh();
      await loadOverview(true);
      setSheet(null);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not delete conversation."));
    } finally {
      setActionBusy(false);
    }
  };
  /** Asks before a chat and its history are deleted. */
  const confirmDelete = (item: HarnessConversation) => {
    Alert.alert(
      "Delete conversation?",
      `Delete “${item.title || "New chat"}” and its history? This cannot be undone.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: () => {
            void deleteConversation(item);
          },
        },
      ],
    );
  };

  const markConversation = async (conversation: HarnessConversation) => {
    if (!client || !conversation || actionBusy) return;
    setActionBusy(true);
    try {
      if (conversation.unread)
        await client.harness.markRead(conversation.session_id);
      else await client.harness.markUnread(conversation.session_id);
      if (conversation.workspace_id === workspaceId)
        await controller?.refresh();
      await loadOverview(true);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not update conversation status."));
    } finally {
      setActionBusy(false);
    }
  };

  // A chat keeps its engine; only a new chat offers every engine.
  const engineOptions: ModelSheetEngine[] = modelSheetEngines(
    engines.filter((item) => !chat.activeSessionId || item.id === harness),
  );
  const activeConversation =
    conversations.find((row) => row.session_id === chat.activeSessionId) ??
    null;
  /** A subagent's conversation opens like a page pushed onto its parent chat. */
  const subagent = subagentHeader(
    chat.activeSession,
    chat.activeSessionLineage,
  );
  const openRelatedChat = (sessionId: string) => {
    if (workspaceId) requestNavigation(workspaceId, { sessionId });
  };
  /** Sheets cover the chat, so the keyboard closes before one opens. */
  const openSheet = (name: string) => {
    void KeyboardController.dismiss().catch(() => undefined);
    setSheet(name);
  };
  const closeSheet = (name: string) =>
    setSheet((current) => (current === name ? null : current));
  const insertIntoChat = chatUnavailable
    ? undefined
    : (text: string) => {
        setComposerText((current) =>
          !current || /\s$/.test(current)
            ? `${current}${text}`
            : `${current} ${text}`,
        );
        setSheet(null);
        setFile(null);
      };
  const openFile = (target: FileViewerTarget) => {
    setFile(target);
    openSheet("file");
  };
  const closeFile = () => {
    setSheet(null);
    setFile(null);
  };

  return {
    client,
    session,
    user,
    offline,
    controller,
    chat,
    accountScope,
    saved,
    workspaces,
    setWorkspaces,
    workspace,
    workspaceId,
    conversations,
    skills,
    engines,
    models,
    engineModels,
    agentConfigs,
    recentModels,
    catalogError,
    loading,
    error,
    setError,
    workspacesError,
    navigation,
    /** Counts navigations; a stale async result compares against it. */
    navigationRevision,
    navigationReady,
    ready,
    isTransitioning,
    chatUnavailable,
    missingModel,
    needsSetup,
    steering,
    contextKey,
    media,
    resolveMedia,
    sheet,
    setSheet,
    openSheet,
    closeSheet,
    file,
    openFile,
    closeFile,
    offlineNoticeHidden,
    setOfflineNoticeHidden,
    noticeInset,
    setNoticeInset,
    attachments,
    setComposerAttachments,
    setAttachments,
    activeEdit,
    setEditing,
    draft,
    /** The chat whose draft is on screen; dictation and recordings go there. */
    draftScope,
    setComposerText,
    skillIds,
    setSkillIds,
    mode,
    harness,
    model,
    effort,
    selectedModel,
    modelsForEngine,
    engineOptions,
    selectAgent,
    selectModel,
    selectEffort,
    voice,
    resolving,
    gateAction,
    send,
    submitEdit,
    scheduleDraft,
    startEdit,
    restoreToDraft,
    outgoing,
    notice,
    searchMentions,
    refresh,
    loadOverview,
    loadCatalogs,
    reloadCatalogs,
    refreshWorkspaces,
    requestNavigation,
    openConversation,
    openWorkspace,
    chooseWorkspace,
    moveNewChat,
    newChat,
    openRelatedChat,
    subagent,
    activeConversation,
    activeIsSubtask,
    actionBusy,
    rename,
    renameText,
    setRenameText,
    renaming,
    saveRename,
    confirmDelete,
    deleteConversation,
    markConversation,
    insertIntoChat,
  };
}

export type WorkspaceChat = ReturnType<typeof useWorkspaceChat>;

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
