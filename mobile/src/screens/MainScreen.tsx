import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  ActivityIndicator,
  Alert,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useVideoPlayer, VideoView } from "expo-video";
import {
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Ellipsis,
  LogOut,
  Menu,
  Moon,
  MoreHorizontal,
  Plus,
  Search,
  Settings2,
  Sun,
  SunMoon,
} from "lucide-react-native";
import { appendUploadMentions } from "@opencuria/chat-core/chatUploads";
import {
  filterMentionCandidates,
  mentionFileSearchQuery,
} from "@opencuria/chat-core/harnessMentions";
import { isWorkspaceReadyForChat } from "@opencuria/chat-core/workspaceReadiness";
import {
  formatEffort,
  resolveCatalogModel,
  snapEffort,
} from "@opencuria/chat-core/harnessModels";
import type {
  HarnessSession,
  HarnessSessionMode,
} from "@opencuria/chat-core/harness";
import type {
  AgentConfig,
  HarnessEngine,
  HarnessConversation,
  ProviderModel,
  Skill,
  Workspace,
} from "@opencuria/client";
import {
  ChatComposer,
  ChatGates,
  ChatMessages,
  type ComposerAttachment,
} from "../components";
import {
  ChatController,
  type ChatControllerSnapshot,
} from "../state/chatController";
import {
  MediaCache,
  clearPickedPhotoCache,
  pickDocuments,
  pickPhoto,
  uploadAttachments,
  type OpenedWorkspaceFile,
} from "../platform";
import { Banner, Button, IconButton, Sheet } from "../ui/primitives";
import { useTheme } from "../ui/theme";
import { useSession } from "./SessionContext";

const HIDDEN = new Set(["removed", "deleted", "deleting", "pending_deletion"]);
const EMPTY: ChatControllerSnapshot = {
  sessions: [],
  messages: [],
  todos: [],
  permissions: [],
  questions: [],
  activeSessionId: null,
  activeSession: null,
  activeSessionLineage: [],
  workspaceId: null,
  loading: false,
  error: null,
  connected: false,
  busy: false,
};
const noopSubscribe = (_listener: () => void) => () => undefined;
const emptySnapshot = () => EMPTY;

type SheetName =
  | "drawer"
  | "workspaces"
  | "profile"
  | "settings"
  | "rename"
  | "media"
  | "conversationActions"
  | null;
type NavigationRequest = {
  revision: number;
  workspaceId: string;
  sessionId?: string;
  newChat?: boolean;
};

export function MainScreen(): React.JSX.Element {
  const insets = useSafeAreaInsets();
  const { theme, mode: themeMode, setMode: setThemeMode } = useTheme();
  const {
    client,
    session,
    user,
    organizations,
    signOut,
    selectOrganization,
    registerResource,
    registerForegroundRefresh,
  } = useSession();
  const controller = useMemo(
    () => (client ? new ChatController(client) : null),
    [client],
  );
  const subscribe = controller?.subscribe ?? noopSubscribe;
  const getSnapshot = controller?.getSnapshot ?? emptySnapshot;
  const chat = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [conversations, setConversations] = useState<HarnessConversation[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [engines, setEngines] = useState<HarnessEngine[]>([]);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [claudeModels, setClaudeModels] = useState<ProviderModel[]>([]);
  const [agentConfigs, setAgentConfigs] = useState<AgentConfig[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [recentModels, setRecentModels] = useState<
    Array<{ model: string; effort: string; last_used_at: string }>
  >([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [navigation, setNavigation] = useState<NavigationRequest | null>(null);
  const navigationRevision = useRef(0);
  const [actionConversation, setActionConversation] =
    useState<HarnessConversation | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const workspaceRef = useRef<string | null>(null);
  const [sheet, setSheet] = useState<SheetName>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [mode, setMode] = useState<HarnessSessionMode>("build");
  const [harness, setHarness] = useState<"native" | "claude">("native");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [drawerError, setDrawerError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [resolving, setResolving] = useState<Set<string>>(new Set());
  const [file, setFile] = useState<OpenedWorkspaceFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const generation = useRef(0);
  const overviewGeneration = useRef(0);
  const catalogGeneration = useRef(0);
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
  const ready = Boolean(
    workspace && isWorkspaceReadyForChat(workspace, isTransitioning),
  );
  const currentBusy = Boolean(chat.activeSession?.status === "busy");
  const workspaceInUse = Boolean(workspace?.has_active_session);
  const occupiedElsewhere = workspaceInUse && !currentBusy;
  const canStart = ready && !workspaceInUse;
  const modelsForEngine = harness === "claude" ? claudeModels : models;
  const selectedModel = resolveCatalogModel(modelsForEngine, model);
  const modeConfig = agentConfigs.find(
    (item) => item.mode === mode || item.agent === mode,
  );
  const contextKey = `${client?.api.baseUrl ?? ""}:${session?.organizationId ?? ""}:${workspaceId ?? ""}:${chat.activeSessionId ?? "new"}`;
  const activeIsSubtask = Boolean(chat.activeSession?.parent_id);
  const navigationReady = Boolean(
    workspaceId &&
    navigation?.workspaceId === workspaceId &&
    navigation.revision === navigationRevision.current &&
    chat.workspaceId === workspaceId &&
    !chat.loading,
  );
  const chatDisabled =
    sending ||
    !navigationReady ||
    !ready ||
    activeIsSubtask ||
    (!chat.activeSessionId && !canStart) ||
    occupiedElsewhere ||
    (!currentBusy && chat.sessions.some((item) => item.status === "busy"));
  const runOptions = () => ({
    harnessId: harness,
    mode,
    model,
    reasoningEffort: effort,
    skillIds,
  });

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

  const resolveMedia = useCallback(
    (path: string) =>
      media
        ? media.resolvePinned(path)
        : Promise.reject(new Error("No workspace selected")),
    [media],
  );

  const loadOverview = useCallback(
    async (background = false) => {
      if (!client || !mountedRef.current || clientRef.current !== client)
        return;
      const requestGeneration = ++overviewGeneration.current;
      if (!background) setLoading(true);
      try {
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
        setConversations(rows);
        setSkills(loadedSkills);
        const chosen = selectedWorkspaceRef.current;
        const fallback =
          ws.find(
            (item) =>
              isWorkspaceReadyForChat(
                item,
                Boolean(item.active_operation || item.intervention_required),
              ) && !item.has_active_session,
          ) ?? ws.find((item) => !HIDDEN.has(item.status));
        const nextId =
          chosen && ws.some((item) => item.id === chosen)
            ? chosen
            : (fallback?.id ?? null);
        if (chosen !== nextId) {
          selectedWorkspaceRef.current = nextId;
          setWorkspaceId(nextId);
          if (nextId) {
            const selectedWorkspace = ws.find((item) => item.id === nextId)!;
            const revision = ++navigationRevision.current;
            setNavigation({
              revision,
              workspaceId: nextId,
              newChat:
                isWorkspaceReadyForChat(
                  selectedWorkspace,
                  Boolean(
                    selectedWorkspace.active_operation ||
                    selectedWorkspace.intervention_required,
                  ),
                ) && !selectedWorkspace.has_active_session,
            });
          } else {
            navigationRevision.current += 1;
            setNavigation(null);
          }
        }
        setError(null);
      } catch (cause) {
        if (
          mountedRef.current &&
          requestGeneration === overviewGeneration.current
        )
          setError(errorMessage(cause, "Could not load workspace data."));
      } finally {
        if (
          mountedRef.current &&
          !background &&
          requestGeneration === overviewGeneration.current
        )
          setLoading(false);
      }
    },
    [client],
  );
  loadOverviewRef.current = loadOverview;

  const loadCatalogs = useCallback(async () => {
    if (!client || !mountedRef.current || clientRef.current !== client) return;
    const requestGeneration = catalogGeneration.current;
    const results = await Promise.allSettled([
      client.harness.listEngines(),
      client.harness.listProviderModels(),
      client.harness.listClaudeModels(),
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
      claudeResult,
      configResult,
      recentResult,
    ] = results;
    setEngines(engineResult.status === "fulfilled" ? engineResult.value : []);
    setModels(modelResult.status === "fulfilled" ? modelResult.value : []);
    setClaudeModels(
      claudeResult.status === "fulfilled" ? claudeResult.value : [],
    );
    setAgentConfigs(
      configResult.status === "fulfilled" ? configResult.value : [],
    );
    setRecentModels(
      recentResult.status === "fulfilled" ? recentResult.value : [],
    );
    setCatalogError(
      results.some((result) => result.status === "rejected")
        ? "Some model settings are unavailable. Workspace chat and history are still available."
        : null,
    );
  }, [client]);
  loadCatalogsRef.current = loadCatalogs;

  useEffect(() => {
    mountedRef.current = true;
    clientRef.current = client;
    overviewGeneration.current += 1;
    catalogGeneration.current += 1;
    generation.current += 1;
    selectedWorkspaceRef.current = null;
    setWorkspaceId(null);
    setNavigation(null);
    setDraft("");
    setAttachments([]);
    setSkillIds([]);
    setConversations([]);
    setWorkspaces([]);
    setCatalogError(null);
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

  useEffect(
    () =>
      registerForegroundRefresh(async () => {
        if (!client || !controller) return;
        await Promise.allSettled([loadOverview(true), controller.refresh()]);
      }),
    [client, controller, loadOverview, registerForegroundRefresh],
  );

  const workspaceScopeKey = workspaces
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
    if (!controller || !navigation) return;
    let alive = true;
    const request = navigation;
    const run = request.newChat
      ? controller.newChat(request.workspaceId)
      : controller.openWorkspace(request.workspaceId, request.sessionId);
    void run
      .then(() => {
        if (alive && request.revision === navigationRevision.current)
          setError(null);
      })
      .catch((cause: unknown) => {
        if (alive && request.revision === navigationRevision.current)
          setError(errorMessage(cause, "Could not load conversations."));
      });
    return () => {
      alive = false;
    };
  }, [controller, navigation]);

  useEffect(() => {
    const selected = chat.activeSession;
    if (selected) {
      setMode(selected.mode);
      setHarness(selected.harness_id ?? "native");
      setModel(selected.model ?? "");
      setEffort(selected.reasoning_effort ?? "");
      setSkillIds(selected.skill_ids ?? []);
      return;
    }
    const config = agentConfigs.find(
      (item) => item.mode === mode || item.agent === mode,
    );
    const catalog = harness === "claude" ? claudeModels : models;
    const preferred = config?.model ?? "";
    const defaultModel = catalog.some((item) => item.id === preferred)
      ? preferred
      : (catalog[0]?.id ?? "");
    setModel(defaultModel);
    setEffort(
      snapEffort(
        resolveCatalogModel(catalog, defaultModel),
        config?.effort ?? "",
      ),
    );
  }, [chat.activeSessionId, agentConfigs, harness, claudeModels, models]);

  const refresh = useCallback(async () => {
    if (!client) return;
    setRefreshing(true);
    await Promise.allSettled([loadOverview(true), controller?.refresh()]);
    setRefreshing(false);
  }, [client, controller, loadOverview]);

  const selectMode = (next: HarnessSessionMode) => {
    setMode(next);
    const config = agentConfigs.find(
      (item) => item.mode === next || item.agent === next,
    );
    const catalog = harness === "claude" ? claudeModels : models;
    const preferred = config?.model ?? "";
    const nextModel = catalog.some((item) => item.id === preferred)
      ? preferred
      : catalog.some((item) => item.id === model)
        ? model
        : (catalog[0]?.id ?? "");
    setModel(nextModel);
    setEffort(
      snapEffort(
        resolveCatalogModel(catalog, nextModel),
        config?.effort ?? effort,
      ),
    );
  };
  const selectHarness = (next: "native" | "claude") => {
    setHarness(next);
    const catalog = next === "claude" ? claudeModels : models;
    const preferredModel = modeConfig?.model || model || "";
    const selectedModel =
      catalog.find((item) => item.id === preferredModel)?.id ??
      catalog[0]?.id ??
      "";
    setModel(selectedModel);
    setEffort(snapEffort(resolveCatalogModel(catalog, selectedModel), effort));
  };
  const selectModel = (next: string) => {
    setModel(next);
    setEffort(snapEffort(resolveCatalogModel(modelsForEngine, next), effort));
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
      if (!chat.activeSessionId && !canStart)
        throw new Error(
          workspaceInUse
            ? "This workspace is already running a task. Wait for it to finish before starting a new chat."
            : "This workspace is unavailable for a new chat.",
        );
      if (
        chat.activeSessionId &&
        (occupiedElsewhere ||
          chat.sessions.some(
            (item) =>
              item.status === "busy" && item.id !== chat.activeSessionId,
          ))
      )
        throw new Error(
          "Another conversation is running in this workspace. Wait for it to finish.",
        );
      if (files.length && !client.realtime.isConnected)
        throw new Error(
          "Connect to the workspace before uploading attachments.",
        );
      sendingRef.current = true;
      setSending(true);
      const ownerGeneration = generation.current;
      const ownerWorkspace = workspace.id;
      const ownerSession = chat.activeSessionId;
      const submittedDraft = draft;
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
        const accepted = await controller.send(prompt, {
          harnessId: harness,
          mode,
          model,
          reasoningEffort: effort,
          skillIds: usedSkills,
        });
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
        sendingRef.current = false;
        setSending(false);
      }
    },
    [
      client,
      controller,
      workspace,
      ready,
      activeIsSubtask,
      chat.activeSessionId,
      chat.sessions,
      canStart,
      workspaceInUse,
      occupiedElsewhere,
      harness,
      mode,
      model,
      effort,
      draft,
      loadOverview,
      navigationReady,
    ],
  );

  const requestNavigation = (
    workspaceId: string,
    options: { sessionId?: string; newChat?: boolean } = {},
  ) => {
    const revision = ++navigationRevision.current;
    generation.current += 1;
    selectedWorkspaceRef.current = workspaceId;
    setWorkspaceId(workspaceId);
    setNavigation({ revision, workspaceId, ...options });
  };

  const openConversation = (row: HarnessConversation) => {
    const owner = workspaces.find((item) => item.id === row.workspace_id);
    if (!owner) {
      setDrawerError("This conversation’s workspace is no longer available.");
      return;
    }
    setDraft("");
    setAttachments([]);
    setSheet(null);
    setDrawerError(null);
    requestNavigation(owner.id, { sessionId: row.session_id });
  };

  const chooseWorkspace = (chosen: Workspace) => {
    setDraft("");
    setAttachments([]);
    setSkillIds([]);
    setSearch("");
    setSheet(null);
    setError(null);
    const isReady = isWorkspaceReadyForChat(
      chosen,
      Boolean(chosen.active_operation || chosen.intervention_required),
    );
    requestNavigation(chosen.id, {
      newChat: isReady && !chosen.has_active_session,
    });
  };

  const newChat = () => {
    if (!workspace || !canStart) return;
    setDraft("");
    setAttachments([]);
    setSkillIds([]);
    setError(null);
    setSheet(null);
    requestNavigation(workspace.id, { newChat: true });
  };

  const viewFile = async (path: string) => {
    if (!media) return;
    try {
      setFile(await media.openFile(path));
      setFileError(null);
      setSheet("media");
    } catch (cause) {
      setError(errorMessage(cause, "Could not open this file."));
    }
  };
  const viewAttachment = async (attachment: {
    uri: string;
    filename: string;
    mime: string;
  }) => {
    if (!media) return;
    try {
      const uri = await media.saveAttachment(attachment.uri);
      const kind: OpenedWorkspaceFile["kind"] = attachment.mime.startsWith(
        "image/",
      )
        ? "image"
        : attachment.mime.startsWith("video/")
          ? "video"
          : "binary";
      setFile({
        uri,
        mimeType: attachment.mime,
        name: attachment.filename,
        kind,
      });
      setFileError(null);
      setSheet("media");
    } catch (cause) {
      setError(errorMessage(cause, "Could not open this attachment."));
    }
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
  const sessionMenu = (item: HarnessConversation) => {
    setActionConversation(item);
    setConfirmDelete(false);
    setSheet("conversationActions");
  };

  const markConversation = async () => {
    if (!client || !actionConversation || actionBusy) return;
    setActionBusy(true);
    try {
      if (actionConversation.unread)
        await client.harness.markRead(actionConversation.session_id);
      else await client.harness.markUnread(actionConversation.session_id);
      if (actionConversation.workspace_id === workspaceId)
        await controller?.refresh();
      await loadOverview(true);
      setSheet(null);
      setActionConversation(null);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not update conversation status."));
    } finally {
      setActionBusy(false);
    }
  };

  const deleteConversation = async () => {
    if (!client || !actionConversation || actionBusy) return;
    setActionBusy(true);
    try {
      await client.harness.deleteSession(actionConversation.session_id);
      if (actionConversation.workspace_id === workspaceId)
        await controller?.refresh();
      await loadOverview(true);
      setSheet(null);
      setActionConversation(null);
      setConfirmDelete(false);
      setError(null);
    } catch (cause) {
      setError(errorMessage(cause, "Could not delete conversation."));
    } finally {
      setActionBusy(false);
    }
  };

  const chosenModels = harness === "claude" ? claudeModels : models;
  const visibleWorkspaces = workspaces.filter(
    (item) => !HIDDEN.has(item.status),
  );
  const profileTitle = user?.first_name?.trim() || user?.email || "Account";
  const filteredWorkspaces = visibleWorkspaces.filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const filteredChats = conversations
    .filter((item) =>
      `${item.title} ${item.workspace_name}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    )
    .sort(
      (a, b) =>
        Date.parse(b.last_message_at || b.updated_at) -
        Date.parse(a.last_message_at || a.updated_at),
    );
  const attentionChats = filteredChats.filter((item) => item.needs_attention);
  const recentChats = filteredChats.filter((item) => !item.needs_attention);

  if (!client || !user)
    return (
      <View style={[styles.center, { backgroundColor: theme.background }]}>
        <ActivityIndicator color={theme.primary} />
      </View>
    );

  return (
    <KeyboardAvoidingView
      style={[
        styles.root,
        { backgroundColor: theme.background, paddingBottom: insets.bottom },
      ]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={Platform.OS === "ios" ? insets.top : 0}
    >
      <View
        style={[
          styles.header,
          {
            paddingTop: Math.max(insets.top, 8),
            backgroundColor: theme.surface,
            borderBottomColor: theme.border,
          },
        ]}
      >
        <IconButton
          icon={<Menu size={21} color={theme.text} />}
          label="Open conversation drawer"
          onPress={() => {
            setSearch("");
            setDrawerError(null);
            setSheet("drawer");
          }}
          testID="main-menu"
        />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Workspace: ${workspace?.name ?? "Select workspace"}`}
          onPress={() => {
            setSearch("");
            setSheet("workspaces");
          }}
          style={styles.titleArea}
        >
          <Text numberOfLines={1} style={[styles.title, { color: theme.text }]}>
            {workspace?.name ?? "Select workspace"}
          </Text>
          <View style={styles.subTitle}>
            <View
              style={[
                styles.dot,
                {
                  backgroundColor: ready
                    ? theme.success
                    : workspace?.has_active_session
                      ? theme.warning
                      : theme.subtleText,
                },
              ]}
            />
            <Text style={{ color: theme.mutedText, fontSize: 12 }}>
              {workspaceLabel(workspace, isTransitioning)}
            </Text>
            <ChevronDown size={14} color={theme.mutedText} />
          </View>
        </Pressable>
        <IconButton
          icon={<Plus size={21} color={theme.primary} />}
          label="New chat"
          onPress={() => {
            void newChat();
          }}
          disabled={!canStart}
          testID="new-chat"
        />
        <IconButton
          icon={<MoreHorizontal size={21} color={theme.text} />}
          label="Profile and settings"
          onPress={() => setSheet("profile")}
          testID="profile-menu"
        />
      </View>

      {workspace && !ready ? (
        <View style={styles.notice}>
          <Banner
            tone={workspace.has_active_session ? "warning" : "info"}
            message={readinessMessage(workspace, isTransitioning)}
            testID="workspace-status"
          />
        </View>
      ) : null}
      {workspace && ready && !chat.connected ? (
        <View style={styles.notice}>
          <Banner
            tone="warning"
            message="Live connection is reconnecting. Chat history remains available; uploads need a live connection."
            testID="realtime-status"
          />
        </View>
      ) : null}
      {workspace &&
      workspace.status === "running" &&
      !workspace.runner_online ? (
        <View style={styles.notice}>
          <Banner
            tone="warning"
            message="The workspace runner is offline. History is still available; messages and uploads require a running runner."
            testID="runner-offline"
          />
        </View>
      ) : null}
      {error || chat.error ? (
        <View style={styles.notice}>
          <Banner
            tone="error"
            message={error || chat.error || ""}
            actionLabel="Retry"
            onAction={() => {
              void refresh();
            }}
            onDismiss={() => setError(null)}
            testID="chat-error"
          />
        </View>
      ) : null}
      {catalogError ? (
        <View style={styles.notice}>
          <Banner
            tone="warning"
            message={catalogError}
            actionLabel="Retry"
            onAction={() => {
              void loadCatalogs();
            }}
            onDismiss={() => setCatalogError(null)}
            testID="catalog-warning"
          />
        </View>
      ) : null}
      {loading && !workspaces.length ? (
        <View style={styles.loading}>
          <ActivityIndicator color={theme.primary} />
          <Text style={{ color: theme.mutedText }}>Loading workspaces…</Text>
        </View>
      ) : null}
      {!visibleWorkspaces.length && !loading ? (
        <View style={styles.noWorkspace}>
          <View
            style={[styles.emptyMark, { backgroundColor: theme.surfaceMuted }]}
          >
            <CircleAlert size={24} color={theme.primary} />
          </View>
          <Text style={[styles.emptyTitle, { color: theme.text }]}>
            No workspaces yet
          </Text>
          <Text style={[styles.emptyText, { color: theme.mutedText }]}>
            Create a workspace in OpenCuria on the web, then return here to
            chat.
          </Text>
          <Button
            title="Refresh"
            variant="outline"
            onPress={() => {
              void refresh();
            }}
            busy={refreshing}
          />
        </View>
      ) : null}
      {activeIsSubtask ? (
        <View style={styles.notice}>
          <Banner
            tone="info"
            message="This is a read-only subtask conversation. Continue the conversation from its parent chat."
            testID="subtask-readonly"
          />
        </View>
      ) : null}

      {chat.activeSessionLineage.length > 1 ? (
        <View
          style={{
            flexDirection: "row",
            flexWrap: "wrap",
            paddingHorizontal: 18,
            gap: 4,
          }}
        >
          {chat.activeSessionLineage.map((ancestor, index) => (
            <Pressable
              key={ancestor.id}
              accessibilityRole="button"
              accessibilityLabel={`Open parent conversation ${ancestor.title}`}
              onPress={() => {
                if (workspaceId)
                  requestNavigation(workspaceId, { sessionId: ancestor.id });
              }}
              style={{
                minHeight: 44,
                justifyContent: "center",
                paddingHorizontal: 6,
              }}
            >
              <Text style={{ color: theme.primary, fontSize: 13 }}>
                {index ? "› " : "‹ "}
                {ancestor.title || ancestor.agent_name}
              </Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      <ChatMessages
        key={contextKey}
        onDismissNotice={(id) => controller!.dismissNotice(id)}
        messages={chat.messages}
        sessions={chat.sessions}
        todos={chat.todos}
        busy={chat.busy}
        disabled={!ready}
        resolveMedia={resolveMedia}
        mediaScopeKey={`${contextKey}:${chat.connected}`}
        onEdit={async (messageId, text) => {
          await controller!.edit(
            messageId,
            text,
            runOptions(),
            chat.activeSessionId,
          );
        }}
        onFork={async (messageId) => {
          await controller?.fork(messageId);
          setError(null);
        }}
        onOpenSubtask={(id) => {
          setDraft("");
          setAttachments([]);
          setSkillIds([]);
          if (workspaceId) requestNavigation(workspaceId, { sessionId: id });
        }}
        onRequestPartDetail={(sessionId, partId) =>
          controller!.requestPartDetail(sessionId, partId)
        }
        onOpenFile={(path) => {
          void viewFile(path);
        }}
        onOpenAttachment={(attachment) => {
          void viewAttachment(attachment);
        }}
        testID="chat-transcript"
      />

      {workspace ? (
        <ChatComposer
          draft={draft}
          onDraftChange={(value) => setDraft(value)}
          onSend={send}
          onStop={async () => {
            await controller?.abort();
          }}
          busy={chat.busy}
          disabled={chatDisabled}
          placeholder={
            chatDisabled
              ? !navigationReady
                ? "Loading workspace conversations…"
                : "Choose an available, running workspace…"
              : undefined
          }
          settings={{
            harness: harness === "claude" ? "Claude" : "OpenCuria",
            mode,
            model: selectedModel?.name ?? model,
            effort: effort ? formatEffort(effort) : "",
          }}
          onSettings={() => setSheet("settings")}
          skills={skills.map((item) => ({ id: item.id, name: item.name }))}
          selectedSkillIds={skillIds}
          onSelectedSkillIdsChange={setSkillIds}
          attachments={attachments}
          onAttachmentsChange={setAttachments}
          onSearchMentions={searchMentions}
          onTakePhoto={async () => pickPhoto("camera")}
          onChoosePhotos={async () => pickPhoto("library")}
          onChooseFiles={async () => pickDocuments()}
          contextKey={contextKey}
          testID="chat-composer"
        />
      ) : null}

      <ChatGates
        permissions={chat.permissions}
        questions={chat.questions}
        resolving={chat.permissions.some((item) =>
          resolving.has(item.request_id),
        )}
        submitting={chat.questions.some((item) =>
          resolving.has(item.request_id),
        )}
        onResolvePermission={(id, response) =>
          gateAction(id, () => controller!.resolvePermission(id, response))
        }
        onSubmitQuestion={(id, answers) =>
          gateAction(id, () => controller!.resolveQuestion(id, answers))
        }
        onSkipQuestion={(id) =>
          gateAction(id, () => controller!.resolveQuestion(id, [], true))
        }
      />

      <Sheet
        visible={sheet === "workspaces"}
        onClose={() => setSheet(null)}
        title="Choose workspace"
        testID="workspace-picker-sheet"
      >
        <SearchField
          value={search}
          onChangeText={setSearch}
          placeholder="Search workspaces…"
          label="Search workspaces"
        />
        {!filteredWorkspaces.length ? (
          <Text style={{ color: theme.mutedText, textAlign: "center" }}>
            No workspaces found.
          </Text>
        ) : (
          filteredWorkspaces.map((item) => (
            <WorkspaceRow
              key={item.id}
              workspace={item}
              selected={workspaceId === item.id}
              onPress={() => {
                void chooseWorkspace(item);
              }}
            />
          ))
        )}
      </Sheet>

      <Sheet
        visible={sheet === "drawer"}
        onClose={() => setSheet(null)}
        title="OpenCuria"
        testID="conversation-drawer"
        maxHeight="96%"
      >
        {drawerError ? (
          <Banner
            tone="error"
            message={drawerError}
            onDismiss={() => setDrawerError(null)}
          />
        ) : null}
        <Button
          title="New chat"
          variant="primary"
          icon={<Plus size={17} color={theme.primaryForeground} />}
          onPress={() => {
            void newChat();
          }}
          disabled={!canStart}
          testID="drawer-new-chat"
        />
        {!canStart && workspace ? (
          <Text style={{ color: theme.subtleText, fontSize: 12 }}>
            New chats need an available, running workspace.
          </Text>
        ) : null}
        <SearchField
          value={search}
          onChangeText={setSearch}
          placeholder="Search workspaces and chats…"
          label="Search workspaces and conversations"
        />
        <SectionTitle title="WORKSPACES" />
        {visibleWorkspaces
          .filter((item) =>
            item.name.toLowerCase().includes(search.toLowerCase()),
          )
          .map((item) => (
            <WorkspaceRow
              key={item.id}
              workspace={item}
              selected={workspaceId === item.id}
              onPress={() => {
                void chooseWorkspace(item);
              }}
              compact
            />
          ))}
        {attentionChats.length ? (
          <>
            <SectionTitle title="ACTION REQUIRED" tone={theme.warning} />
            {attentionChats.map((item) => (
              <ConversationRow
                key={item.session_id}
                row={item}
                onPress={() => {
                  void openConversation(item);
                }}
                onMore={() => sessionMenu(item)}
              />
            ))}
          </>
        ) : null}
        <SectionTitle title="RECENT" />
        {recentChats.map((item) => (
          <ConversationRow
            key={item.session_id}
            row={item}
            onPress={() => {
              void openConversation(item);
            }}
            onMore={() => sessionMenu(item)}
          />
        ))}
        {!filteredChats.length ? (
          <Text
            style={{
              color: theme.mutedText,
              textAlign: "center",
              paddingVertical: 18,
            }}
          >
            No conversations yet.
          </Text>
        ) : null}
      </Sheet>

      <Sheet
        visible={sheet === "profile"}
        onClose={() => setSheet(null)}
        title="Profile & settings"
        testID="profile-sheet"
      >
        <View
          style={[styles.accountCard, { backgroundColor: theme.surfaceMuted }]}
        >
          <View style={[styles.avatar, { backgroundColor: theme.primary }]}>
            <Text style={{ color: theme.primaryForeground, fontWeight: "700" }}>
              {profileTitle.slice(0, 1).toUpperCase()}
            </Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text
              style={{ color: theme.text, fontSize: 16, fontWeight: "600" }}
            >
              {profileTitle}
            </Text>
            <Text style={{ color: theme.mutedText, fontSize: 13 }}>
              {user.email}
            </Text>
          </View>
        </View>
        <SectionTitle title="ORGANIZATION" />
        {!organizations.length ? (
          <Text style={{ color: theme.mutedText }}>
            No organizations available.
          </Text>
        ) : (
          organizations.map((org) => (
            <Pressable
              key={org.id}
              accessibilityRole="button"
              accessibilityState={{
                selected: org.id === session?.organizationId,
              }}
              onPress={() => {
                setSheet(null);
                void selectOrganization(org.id);
              }}
              style={[styles.row, { borderBottomColor: theme.border }]}
            >
              <Text style={{ color: theme.text, flex: 1 }}>{org.name}</Text>
              {org.id === session?.organizationId ? (
                <Check color={theme.primary} size={17} />
              ) : null}
            </Pressable>
          ))
        )}
        <SectionTitle title="APPEARANCE" />
        <View
          style={[styles.themeGroup, { backgroundColor: theme.surfaceMuted }]}
        >
          {(
            [
              ["system", SunMoon, "System"],
              ["light", Sun, "Light"],
              ["dark", Moon, "Dark"],
            ] as const
          ).map(([value, Icon, label]) => (
            <Pressable
              key={value}
              accessibilityRole="button"
              accessibilityState={{ selected: themeMode === value }}
              onPress={() => setThemeMode(value)}
              style={[
                styles.themeChoice,
                themeMode === value && { backgroundColor: theme.surface },
              ]}
            >
              <Icon
                size={16}
                color={themeMode === value ? theme.primary : theme.mutedText}
              />
              <Text style={{ color: theme.text, fontSize: 12 }}>{label}</Text>
            </Pressable>
          ))}
        </View>
        <Button
          title="Sign out"
          variant="outline"
          icon={<LogOut size={17} color={theme.danger} />}
          onPress={() => {
            Alert.alert(
              "Sign out?",
              "Your secure sign-in token will be removed from this device.",
              [
                { text: "Cancel", style: "cancel" },
                {
                  text: "Sign out",
                  style: "destructive",
                  onPress: () => {
                    setSheet(null);
                    void signOut();
                  },
                },
              ],
            );
          }}
          textStyle={{ color: theme.danger }}
          style={{ alignSelf: "stretch", marginTop: 8 }}
          testID="sign-out"
        />
      </Sheet>

      <Sheet
        visible={sheet === "settings"}
        onClose={() => setSheet(null)}
        title="Chat settings"
        testID="model-settings-sheet"
      >
        <SectionTitle title="MODE" />
        <View style={styles.chips}>
          {(["build", "plan"] as const).map((item) => (
            <Choice
              key={item}
              title={item === "build" ? "Build" : "Plan"}
              selected={item === mode}
              onPress={() => selectMode(item)}
            />
          ))}
        </View>
        <SectionTitle title="ENGINE" />
        <View style={styles.chips}>
          {(["native", "claude"] as const)
            .filter(
              (id) =>
                !engines.length || engines.some((engine) => engine.id === id),
            )
            .map((id) => {
              const item = engines.find((engine) => engine.id === id);
              return (
                <Choice
                  key={id}
                  title={
                    item?.name ?? (id === "native" ? "OpenCuria" : "Claude")
                  }
                  selected={harness === id}
                  disabled={Boolean(
                    chat.activeSessionId ||
                    (item && (!item.connected || !item.modes.includes(mode))),
                  )}
                  onPress={() => selectHarness(id)}
                />
              );
            })}
        </View>
        <SectionTitle title="MODEL" />
        {!chosenModels.length ? (
          <Text style={{ color: theme.mutedText }}>
            No models are available for this engine.
          </Text>
        ) : (
          <ScrollView style={{ maxHeight: 250 }} nestedScrollEnabled>
            {[
              ...new Set([
                ...recentModels
                  .filter((row) =>
                    chosenModels.some((item) => item.id === row.model),
                  )
                  .map((row) => row.model),
                ...chosenModels.map((item) => item.id),
              ]),
            ].map((id) => {
              const item = chosenModels.find((entry) => entry.id === id);
              if (!item) return null;
              return (
                <Pressable
                  key={id}
                  accessibilityRole="button"
                  accessibilityState={{ selected: model === id }}
                  onPress={() => selectModel(id)}
                  style={[styles.row, { borderBottomColor: theme.border }]}
                >
                  <View style={{ flex: 1 }}>
                    <Text style={{ color: theme.text, fontSize: 14 }}>
                      {item.name}
                    </Text>
                    <Text style={{ color: theme.mutedText, fontSize: 11 }}>
                      {item.provider ?? "Model"}
                      {item.context_length
                        ? ` · ${Math.round(item.context_length / 1000)}k context`
                        : ""}
                    </Text>
                  </View>
                  {model === id ? (
                    <Check color={theme.primary} size={17} />
                  ) : null}
                </Pressable>
              );
            })}
          </ScrollView>
        )}
        <SectionTitle title="REASONING EFFORT" />
        {selectedModel?.reasoning_efforts.length ? (
          <View style={styles.chips}>
            {selectedModel.reasoning_efforts.map((item) => (
              <Choice
                key={item}
                title={formatEffort(item)}
                selected={effort === item}
                onPress={() => setEffort(item)}
              />
            ))}
          </View>
        ) : (
          <Text style={{ color: theme.mutedText, fontSize: 13 }}>
            This model does not expose reasoning effort settings.
          </Text>
        )}
        <Button
          title="Done"
          variant="primary"
          onPress={() => setSheet(null)}
          style={{ alignSelf: "stretch", marginTop: 8 }}
        />
      </Sheet>

      <Sheet
        visible={sheet === "rename"}
        onClose={() => setSheet(null)}
        title="Rename conversation"
        testID="rename-session-sheet"
      >
        <TextInput
          value={renameText}
          onChangeText={setRenameText}
          accessibilityLabel="Conversation name"
          placeholder="Conversation name"
          placeholderTextColor={theme.subtleText}
          style={[
            styles.renameInput,
            { color: theme.text, borderColor: theme.border },
          ]}
          autoFocus
        />
        <Button
          title="Save name"
          variant="primary"
          onPress={() => {
            void saveRename();
          }}
          disabled={!renameText.trim()}
          busy={renaming}
          style={{ alignSelf: "stretch" }}
        />
      </Sheet>
      <Sheet
        visible={sheet === "conversationActions"}
        onClose={() => {
          setSheet(null);
          setActionConversation(null);
          setConfirmDelete(false);
        }}
        title={confirmDelete ? "Delete conversation?" : "Conversation actions"}
        testID="conversation-actions-sheet"
      >
        {actionConversation ? (
          confirmDelete ? (
            <>
              <Text style={{ color: theme.text, fontSize: 15, lineHeight: 22 }}>
                Delete “{actionConversation.title || "New chat"}” and its
                history? This cannot be undone.
              </Text>
              <Button
                title="Delete conversation"
                variant="danger"
                onPress={() => {
                  void deleteConversation();
                }}
                busy={actionBusy}
                style={{ alignSelf: "stretch" }}
                testID="confirm-delete-conversation"
              />
              <Button
                title="Cancel"
                variant="outline"
                onPress={() => setConfirmDelete(false)}
                disabled={actionBusy}
                style={{ alignSelf: "stretch" }}
              />
            </>
          ) : (
            <>
              <Text
                numberOfLines={2}
                style={{ color: theme.mutedText, fontSize: 13 }}
              >
                {actionConversation.title || "New chat"} ·{" "}
                {actionConversation.workspace_name}
              </Text>
              <Button
                title="Rename"
                variant="secondary"
                onPress={() => {
                  rename(actionConversation);
                  setActionConversation(null);
                }}
                style={{ alignSelf: "stretch" }}
              />
              <Button
                title={actionConversation.unread ? "Mark read" : "Mark unread"}
                variant="secondary"
                onPress={() => {
                  void markConversation();
                }}
                busy={actionBusy}
                style={{ alignSelf: "stretch" }}
              />
              <Button
                title="Delete"
                variant="outline"
                textStyle={{ color: theme.danger }}
                onPress={() => setConfirmDelete(true)}
                disabled={actionBusy}
                style={{ alignSelf: "stretch" }}
              />
              <Button
                title="Cancel"
                variant="ghost"
                onPress={() => {
                  setSheet(null);
                  setActionConversation(null);
                }}
                style={{ alignSelf: "stretch" }}
              />
            </>
          )
        ) : null}
      </Sheet>
      <MediaSheet
        visible={sheet === "media"}
        file={file}
        error={fileError}
        onError={setFileError}
        onClose={() => {
          setSheet(null);
          setFile(null);
        }}
      />
    </KeyboardAvoidingView>
  );
}

function workspaceLabel(
  item: Workspace | null,
  transitioning: boolean,
): string {
  if (!item) return "Choose workspace";
  if (item.intervention_required) return "Needs attention";
  if (transitioning && item.active_operation)
    return item.active_operation.replaceAll("_", " ");
  if (item.has_active_session) return "Busy";
  if (item.status === "running")
    return item.runner_online ? "Running" : "Runner offline";
  return item.status.replaceAll("_", " ");
}
function readinessMessage(item: Workspace, transitioning: boolean): string {
  if (item.has_active_session)
    return "This workspace is already running a task. Conversation history is available, but wait for the run to finish before starting another chat.";
  if (item.intervention_required)
    return "This workspace needs attention before it can accept messages.";
  if (transitioning)
    return `Workspace is ${item.active_operation?.replaceAll("_", " ") || "updating"}. Chat is disabled until it is ready.`;
  if (item.status !== "running")
    return `Workspace is ${item.status.replaceAll("_", " ")}. Select a running workspace to send messages.`;
  if (!item.runner_online)
    return "Workspace runner is offline. Conversation history remains available.";
  return "Workspace is not ready for chat.";
}
function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function SectionTitle({
  title,
  tone,
}: {
  title: string;
  tone?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Text style={[styles.section, { color: tone ?? theme.subtleText }]}>
      {title}
    </Text>
  );
}
function SearchField({
  value,
  onChangeText,
  placeholder,
  label,
}: {
  value: string;
  onChangeText: (value: string) => void;
  placeholder: string;
  label: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View
      style={[
        styles.searchBox,
        { backgroundColor: theme.surfaceMuted, borderColor: theme.border },
      ]}
    >
      <Search size={16} color={theme.subtleText} />
      <TextInput
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={theme.subtleText}
        accessibilityLabel={label}
        style={[styles.searchInput, { color: theme.text }]}
      />
    </View>
  );
}
function WorkspaceRow({
  workspace,
  selected,
  onPress,
  compact = false,
}: {
  workspace: Workspace;
  selected: boolean;
  onPress: () => void;
  compact?: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  const ready = isWorkspaceReadyForChat(
    workspace,
    Boolean(workspace.active_operation || workspace.intervention_required),
  );
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={[
        styles.row,
        { borderBottomColor: theme.border, minHeight: compact ? 44 : 56 },
      ]}
    >
      <View
        style={[
          styles.dot,
          {
            backgroundColor: ready
              ? theme.success
              : workspace.has_active_session
                ? theme.warning
                : theme.subtleText,
          },
        ]}
      />
      <View style={{ flex: 1 }}>
        <Text
          numberOfLines={1}
          style={{ color: theme.text, fontSize: compact ? 14 : 15 }}
        >
          {workspace.name}
        </Text>
        {!compact || workspace.has_active_session ? (
          <Text style={{ color: theme.mutedText, fontSize: 11 }}>
            {workspaceLabel(
              workspace,
              Boolean(
                workspace.active_operation || workspace.intervention_required,
              ),
            )}
            {workspace.has_active_session ? " · history only" : ""}
          </Text>
        ) : null}
      </View>
      {selected ? (
        <Check size={17} color={theme.primary} />
      ) : (
        <ChevronRight size={17} color={theme.subtleText} />
      )}
    </Pressable>
  );
}
function ConversationRow({
  row,
  onPress,
  onMore,
}: {
  row: HarnessConversation;
  onPress: () => void;
  onMore: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const state = row.needs_attention
    ? "Needs attention"
    : row.status === "busy"
      ? "Working"
      : row.unread
        ? "Unread"
        : ago(row.last_message_at || row.updated_at);
  return (
    <View style={[styles.conversationRow, { borderBottomColor: theme.border }]}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`${row.title || "New chat"} in ${row.workspace_name}, ${state}`}
        onPress={onPress}
        style={styles.conversationMain}
      >
        <View
          style={[
            styles.dot,
            {
              backgroundColor: row.needs_attention
                ? theme.warning
                : row.status === "busy"
                  ? theme.primary
                  : row.unread
                    ? theme.info
                    : theme.subtleText,
            },
          ]}
        />
        <View style={{ flex: 1 }}>
          <Text
            numberOfLines={1}
            style={{
              color: theme.text,
              fontSize: 14,
              fontWeight: row.unread || row.needs_attention ? "600" : "400",
            }}
          >
            {row.title || "New chat"}
          </Text>
          <Text
            numberOfLines={1}
            style={{ color: theme.mutedText, fontSize: 12 }}
          >
            {row.workspace_name} · {state}
          </Text>
        </View>
      </Pressable>
      <IconButton
        icon={<Ellipsis size={18} color={theme.mutedText} />}
        label={`Actions for ${row.title || "conversation"}`}
        onPress={onMore}
        testID={`conversation-actions-${row.session_id}`}
      />
    </View>
  );
}
function Choice({
  title,
  selected,
  disabled = false,
  onPress,
}: {
  title: string;
  selected: boolean;
  disabled?: boolean;
  onPress: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected, disabled }}
      disabled={disabled}
      onPress={onPress}
      style={[
        styles.choice,
        {
          backgroundColor: selected ? theme.primary : theme.surfaceMuted,
          opacity: disabled ? 0.45 : 1,
        },
      ]}
    >
      <Text
        style={{
          color: selected ? theme.primaryForeground : theme.text,
          fontSize: 13,
          fontWeight: "600",
        }}
      >
        {title}
      </Text>
    </Pressable>
  );
}
function ago(value: string): string {
  const when = Date.parse(value);
  if (!Number.isFinite(when)) return "";
  const mins = Math.max(0, Math.floor((Date.now() - when) / 60000));
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}
function MediaSheet({
  visible,
  file,
  error,
  onError,
  onClose,
}: {
  visible: boolean;
  file: OpenedWorkspaceFile | null;
  error: string | null;
  onError: (value: string | null) => void;
  onClose: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <Sheet
      visible={visible}
      onClose={onClose}
      title={file?.name ?? "Attachment"}
      testID="media-viewer-sheet"
      maxHeight="94%"
    >
      {error ? <Banner tone="error" message={error} /> : null}
      {file?.kind === "image" ? (
        <Image
          source={{ uri: file.uri }}
          resizeMode="contain"
          style={{ width: "100%", height: 380 }}
          accessibilityLabel={file.name}
        />
      ) : null}
      {file?.kind === "video" ? <VideoPreview uri={file.uri} /> : null}
      {file?.kind === "text" ? (
        <ScrollView style={{ maxHeight: 450 }}>
          <Text
            selectable
            style={{
              color: theme.text,
              fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
              fontSize: 13,
              lineHeight: 19,
            }}
          >
            {file.text ?? ""}
          </Text>
        </ScrollView>
      ) : null}
      {file && file.kind === "binary" ? (
        <View
          style={[styles.docPreview, { backgroundColor: theme.surfaceMuted }]}
        >
          <Settings2 color={theme.primary} size={24} />
          <Text style={{ color: theme.text, fontWeight: "600" }}>
            {file.mimeType}
          </Text>
          <Text style={{ color: theme.mutedText, textAlign: "center" }}>
            Downloaded securely. Open or share this document with another app.
          </Text>
          <Button
            title="Share or open"
            variant="primary"
            onPress={() => {
              void import("expo-sharing")
                .then(({ shareAsync }) =>
                  shareAsync(file.uri, {
                    mimeType: file.mimeType,
                    dialogTitle: file.name,
                  }),
                )
                .catch((cause: unknown) =>
                  onError(errorMessage(cause, "Could not share file.")),
                );
            }}
          />
        </View>
      ) : null}
    </Sheet>
  );
}
function VideoPreview({ uri }: { uri: string }): React.JSX.Element {
  const player = useVideoPlayer(uri, (instance) => {
    instance.play();
  });
  return (
    <VideoView
      player={player}
      nativeControls
      contentFit="contain"
      style={{ width: "100%", height: 300, borderRadius: 16 }}
    />
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  header: {
    minHeight: 62,
    paddingHorizontal: 8,
    flexDirection: "row",
    alignItems: "center",
    gap: 1,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  titleArea: {
    flex: 1,
    minWidth: 0,
    paddingHorizontal: 8,
    paddingVertical: 4,
    alignItems: "flex-start",
  },
  title: { fontSize: 15, fontWeight: "600", lineHeight: 20 },
  subTitle: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    marginTop: 2,
  },
  dot: { width: 7, height: 7, borderRadius: 4 },
  notice: { paddingHorizontal: 12, paddingTop: 7 },
  loading: {
    position: "absolute",
    zIndex: 2,
    top: 76,
    alignSelf: "center",
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    padding: 9,
    borderRadius: 14,
    backgroundColor: "#77777720",
  },
  noWorkspace: {
    position: "absolute",
    zIndex: 2,
    top: "25%",
    alignSelf: "center",
    alignItems: "center",
    paddingHorizontal: 30,
    gap: 10,
  },
  emptyMark: {
    width: 52,
    height: 52,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
  },
  emptyTitle: { fontSize: 20, fontWeight: "600" },
  emptyText: { fontSize: 14, lineHeight: 21, textAlign: "center" },
  section: {
    fontSize: 11,
    fontWeight: "700",
    letterSpacing: 0.8,
    marginTop: 8,
    marginBottom: 2,
  },
  row: {
    minHeight: 54,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 4,
  },
  conversationRow: {
    minHeight: 56,
    flexDirection: "row",
    alignItems: "center",
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  conversationMain: {
    flex: 1,
    minHeight: 56,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  searchBox: {
    minHeight: 46,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderRadius: 13,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 12,
    marginVertical: 4,
  },
  searchInput: { flex: 1, fontSize: 15, paddingVertical: 10 },
  accountCard: {
    minHeight: 72,
    borderRadius: 16,
    padding: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
  },
  avatar: {
    width: 42,
    height: 42,
    borderRadius: 15,
    alignItems: "center",
    justifyContent: "center",
  },
  themeGroup: { flexDirection: "row", padding: 4, borderRadius: 14 },
  themeChoice: {
    flex: 1,
    minHeight: 42,
    alignItems: "center",
    justifyContent: "center",
    flexDirection: "row",
    gap: 6,
    borderRadius: 11,
  },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  choice: {
    minHeight: 39,
    borderRadius: 13,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 14,
    paddingVertical: 9,
  },
  renameInput: {
    minHeight: 52,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 14,
    paddingHorizontal: 14,
    fontSize: 16,
  },
  docPreview: {
    minHeight: 210,
    borderRadius: 18,
    padding: 22,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
  },
});
