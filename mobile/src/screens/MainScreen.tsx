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
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { KeyboardController } from "react-native-keyboard-controller";

import { useVideoPlayer, VideoView } from "expo-video";
import { CircleAlert, Settings2 } from "lucide-react-native";
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
  harnessEngineChoices,
  harnessEngineLabel,
  harnessEngineDefaults,
  harnessSessionModels,
} from "@opencuria/chat-core/harnessModels";
import type {
  HarnessId,
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
  ChatLayout,
  ChatMessages,
  type ComposerAttachment,
} from "../components";
import { ModelSheet, type ModelSheetAgent } from "../components/ModelSheet";
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
import { Banner, Button, NativeModal, NativeAction } from "../ui/primitives";
import { Drawer } from "../ui/Drawer";
import { useChromeTopInset } from "../ui/chromeInset";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { WorkspaceSidebar } from "../components/WorkspaceSidebar";
import { NativeList } from "../ui/NativeList";
import { SegmentedControl } from "@expo/ui/community/segmented-control";
import { NativePicker } from "../ui/selection";
import { NativeTextField } from "../ui/NativeTextField";
import { useTheme } from "../ui/theme";
import { useSession } from "./SessionContext";
import { useNotifications } from "../notifications/NotificationContext";
import { NotificationSettings } from "../notifications/NotificationSettings";
import { setViewedNotificationSession } from "../notifications/native";

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
  | null;
type NavigationRequest = {
  revision: number;
  workspaceId: string;
  sessionId?: string;
  newChat?: boolean;
};

export function MainScreen(): React.JSX.Element {
  const notifications = useNotifications();
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
  const [engineModels, setEngineModels] = useState<
    Record<string, ProviderModel[]>
  >({});
  const [agentConfigs, setAgentConfigs] = useState<AgentConfig[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [recentModels, setRecentModels] = useState<
    Array<{ model: string; effort: string; last_used_at: string }>
  >([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [navigation, setNavigation] = useState<NavigationRequest | null>(null);
  const navigationRevision = useRef(0);
  const [actionBusy, setActionBusy] = useState(false);
  const sendingRef = useRef(false);
  const workspaceRef = useRef<string | null>(null);
  const [sheet, setSheet] = useState<SheetName>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [mode, setMode] = useState<HarnessSessionMode>("build");
  const [harness, setHarness] = useState<HarnessId>("native");
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
  const [noticeInset, setNoticeInset] = useState(0);
  const generation = useRef(0);
  const overviewGeneration = useRef(0);
  const catalogGeneration = useRef(0);
  const selectionDirty = useRef(false);
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
  const catalogForEngine = (id: HarnessId) =>
    id === "native" ? models : (engineModels[id] ?? []);
  const modelsForEngine = harnessSessionModels(
    catalogForEngine(harness),
    harness,
    chat.activeSessionId ? (chat.activeSession?.model ?? "") : undefined,
  );
  const selectedModel = resolveCatalogModel(modelsForEngine, model);
  const modeConfig = agentConfigs.find((item) => item.agent === mode);
  const contextKey = `${client?.api.baseUrl ?? ""}:${session?.organizationId ?? ""}:${workspaceId ?? ""}:${chat.activeSessionId ?? "new"}`;
  const activeIsSubtask = Boolean(chat.activeSession?.parent_id);
  const navigationReady = Boolean(
    workspaceId &&
    navigation?.workspaceId === workspaceId &&
    navigation.revision === navigationRevision.current &&
    chat.workspaceId === workspaceId &&
    !chat.loading,
  );
  /** The chat cannot take a message, as opposed to one being on its way. */
  const chatUnavailable = !navigationReady || !ready || activeIsSubtask;
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
                ),
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
    const requestGeneration = ++catalogGeneration.current;
    const engineRequest = client.harness.listEngines();
    const results = await Promise.allSettled([
      engineRequest,
      client.harness.listEngineModels("native"),
      engineRequest.then(async (rows) => {
        const external = harnessEngineChoices(rows).filter(
          (engine) =>
            engine.id !== "native" &&
            (engine.id === "claude" ||
              rows.some((row) => row.id === engine.id)),
        );
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

  useEffect(
    () =>
      registerForegroundRefresh(async () => {
        if (!client || !controller) return;
        await Promise.allSettled([
          loadOverview(true),
          controller.refresh(),
          loadCatalogs(),
        ]);
      }),
    [client, controller, loadOverview, loadCatalogs, registerForegroundRefresh],
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
    const defaults = harnessEngineDefaults(harness, engines, {
      model: modeConfig?.model ?? "",
      effort: modeConfig?.effort ?? "",
    });
    const catalog = catalogForEngine(harness);
    const selected =
      catalog.find((item) => item.id === defaults.model) ?? catalog[0];
    setModel(selected?.id ?? defaults.model);
    setEffort(
      selected ? snapEffort(selected, defaults.effort) : defaults.effort,
    );
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
    setRefreshing(true);
    await Promise.allSettled([loadOverview(true), controller?.refresh()]);
    setRefreshing(false);
  }, [client, controller, loadOverview]);

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
    setHarness(next);
    setMode(nextMode);
    const config = agentConfigs.find((item) => item.agent === nextMode);
    const defaults = harnessEngineDefaults(next, engines, {
      model: config?.model ?? "",
      effort: config?.effort ?? "",
    });
    const catalog = catalogForEngine(next);
    const selected =
      catalog.find((item) => item.id === defaults.model) ?? catalog[0];
    setModel(selected?.id ?? defaults.model);
    setEffort(
      selected ? snapEffort(selected, defaults.effort) : defaults.effort,
    );
  };
  const selectModel = (next: string) => {
    if (!modelsForEngine.some((item) => item.id === next)) return;
    selectionDirty.current = true;
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
      if (files.length && !client.realtime.isConnected)
        throw new Error(
          "Connect to the workspace before uploading attachments.",
        );
      sendingRef.current = true;
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

  useEffect(() => {
    setViewedNotificationSession(chat.activeSessionId);
    return () => setViewedNotificationSession(null);
  }, [chat.activeSessionId]);

  useEffect(() => {
    const target = notifications.state.target;
    if (
      !target ||
      !client ||
      loading ||
      target.organizationId !== session?.organizationId
    )
      return;
    let active = true;
    // Resolve through authenticated REST first. A push cannot grant access to
    // another account's workspace or make us silently open a different session.
    void Promise.all([
      client.workspaces.get(target.workspaceId),
      client.harness.listSessions(target.workspaceId),
    ])
      .then(([owner, sessions]) => {
        if (!active) return;
        if (!sessions.some((item) => item.id === target.sessionId))
          throw Object.assign(
            new Error("This conversation is no longer available."),
            { status: 404 },
          );
        setWorkspaces((rows) =>
          rows.some((item) => item.id === owner.id) ? rows : [...rows, owner],
        );
        setSheet(null);
        setDraft("");
        setAttachments([]);
        requestNavigation(owner.id, { sessionId: target.sessionId });
        notifications.controller?.consumeTarget();
      })
      .catch((cause: unknown) => {
        if (active) {
          const status =
            cause && typeof cause === "object" && "status" in cause
              ? cause.status
              : undefined;
          if (status === 403 || status === 404) {
            setError(
              "This notification's conversation is no longer available.",
            );
            notifications.controller?.consumeTarget();
          } else {
            setError(
              "Could not open this conversation. Check your connection and retry.",
            );
          }
        }
      });
    return () => {
      active = false;
    };
  }, [
    notifications.state.target,
    notifications.controller,
    client,
    loading,
    workspaces,
    session?.organizationId,
  ]);

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
      newChat: isReady,
    });
  };

  const newChat = () => {
    if (!workspace || !ready) return;
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

  const markAllRead = async () => {
    const running = new Set(
      workspaces
        .filter((item) => item.status === "running")
        .map((item) => item.id),
    );
    try {
      await Promise.all(
        conversations
          .filter((row) => row.unread && running.has(row.workspace_id))
          .map((row) => client!.harness.markRead(row.session_id)),
      );
      await loadOverview(true);
    } catch (cause) {
      setDrawerError(
        errorMessage(cause, "Could not mark conversations as read."),
      );
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

  const chosenModels = modelsForEngine;
  const visibleWorkspaces = workspaces.filter(
    (item) => !HIDDEN.has(item.status),
  );
  const profileTitle = user?.first_name?.trim() || user?.email || "Account";
  const filteredWorkspaces = visibleWorkspaces.filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  // A chat keeps its engine; only a new chat offers every engine's modes.
  const agentOptions: ModelSheetAgent[] = harnessEngineChoices(engines)
    .filter((item) => !chat.activeSessionId || item.id === harness)
    .flatMap((item) =>
      (["build", "plan"] as const)
        .filter((value) => item.modes.includes(value))
        .map((value) => ({
          engine: item.id,
          mode: value,
          label: `${item.name} ${value === "build" ? "Build" : "Plan"}`,
          disabled: !item.connected,
          note: item.connected ? undefined : "Not connected",
        })),
    );
  const activeConversation =
    conversations.find((row) => row.session_id === chat.activeSessionId) ??
    null;
  const chatMenuActions: NativeMenuAction[] = [
    {
      id: "workspace",
      title: "Switch workspace",
      symbol: "square.stack.3d.up",
    },
    ...(chat.activeSession && !activeIsSubtask
      ? [
          {
            id: "rename",
            title: "Rename chat",
            symbol: "pencil",
            disabled: isTransitioning,
          },
        ]
      : []),
    ...(activeConversation
      ? [
          {
            id: "delete",
            title: "Delete chat",
            symbol: "trash",
            destructive: true,
            disabled: isTransitioning || actionBusy,
          },
        ]
      : []),
  ];
  /** Sheets cover the chat, so the keyboard closes before one opens. */
  const openSheet = (name: Exclude<SheetName, null>) => {
    void KeyboardController.dismiss().catch(() => undefined);
    setSheet(name);
  };
  const chatMenuAction = (id: string) => {
    if (id === "workspace") {
      setSearch("");
      openSheet("workspaces");
    } else if (id === "rename" && chat.activeSession) {
      rename(chat.activeSession);
    } else if (id === "delete" && activeConversation) {
      sessionMenu(activeConversation);
    }
  };

  if (!client || !user)
    return (
      <View style={[styles.center, { backgroundColor: theme.background }]}>
        <ActivityIndicator color={theme.primary} />
      </View>
    );

  return (
    <View style={styles.root}>
      <Drawer
        visible={sheet === "drawer"}
        onOpen={() => setSheet("drawer")}
        onClose={() =>
          setSheet((current) => (current === "drawer" ? null : current))
        }
        title={workspace?.name ?? "OpenCuria"}
        onCreate={newChat}
        canCreate={ready}
        menuActions={chatMenuActions}
        onMenuAction={chatMenuAction}
        navigation={
          <WorkspaceSidebar
            workspaces={workspaces}
            conversations={conversations}
            activeWorkspaceId={workspaceId}
            activeSessionId={chat.activeSessionId}
            scopeKey={`${client.api.baseUrl}:${session?.organizationId ?? ""}`}
            canCreate={ready}
            onCreate={newChat}
            onWorkspace={chooseWorkspace}
            onConversation={openConversation}
            onMarkAllRead={() => {
              void markAllRead();
            }}
            onAllWorkspaces={() => {
              setSearch("");
              setSheet("workspaces");
            }}
            onConversationAction={(row, action) => {
              if (action === "rename") rename(row);
              else if (action === "read") void markConversation(row);
              else sessionMenu(row);
            }}
            profileTitle={profileTitle}
            onProfile={() => setSheet("profile")}
            error={drawerError}
            onDismissError={() => setDrawerError(null)}
          />
        }
      >
        <ChatLayout
          composer={
            workspace ? (
              <ChatComposer
                draft={draft}
                onDraftChange={(value) => setDraft(value)}
                onSend={send}
                onStop={async () => {
                  await controller?.abort();
                }}
                busy={chat.busy}
                disabled={chatUnavailable}
                placeholder={
                  !navigationReady
                    ? "Loading workspace conversations…"
                    : chatUnavailable
                      ? "Choose an available, running workspace…"
                      : chat.messages.length > 0
                        ? "Reply to OpenCuria"
                        : "Ask OpenCuria"
                }
                settings={{
                  harness: harnessEngineLabel(harness, engines),
                  mode,
                  model: selectedModel?.name ?? model,
                  effort: effort ? formatEffort(effort) : "",
                }}
                onSettings={() => openSheet("settings")}
                skills={skills.map((item) => ({
                  id: item.id,
                  name: item.name,
                }))}
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
            ) : null
          }
          overlay={
            <>
              <ChromeOverlay onHeightChange={setNoticeInset}>
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
                    <Text style={{ color: theme.mutedText }}>
                      Loading workspaces…
                    </Text>
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
                      backgroundColor: theme.background,
                    }}
                  >
                    {chat.activeSessionLineage.map((ancestor, index) => (
                      <NativeAction
                        key={ancestor.id}
                        accessibilityRole="button"
                        accessibilityLabel={`Open parent conversation ${ancestor.title}`}
                        onPress={() => {
                          if (workspaceId)
                            requestNavigation(workspaceId, {
                              sessionId: ancestor.id,
                            });
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
                      </NativeAction>
                    ))}
                  </View>
                ) : null}
              </ChromeOverlay>
              {!visibleWorkspaces.length && !loading ? (
                <View style={styles.noWorkspace}>
                  <View
                    style={[
                      styles.emptyMark,
                      { backgroundColor: theme.surfaceMuted },
                    ]}
                  >
                    <CircleAlert size={24} color={theme.primary} />
                  </View>
                  <Text style={[styles.emptyTitle, { color: theme.text }]}>
                    No workspaces yet
                  </Text>
                  <Text style={[styles.emptyText, { color: theme.mutedText }]}>
                    Create a workspace in OpenCuria on the web, then return here
                    to chat.
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
            </>
          }
        >
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
            contentInsetTop={noticeInset}
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
              if (workspaceId)
                requestNavigation(workspaceId, { sessionId: id });
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
        </ChatLayout>
      </Drawer>
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

      <NativeModal
        visible={sheet === "workspaces"}
        onClose={() => setSheet(null)}
        title="Choose workspace"
        scrollable={false}
        testID="workspace-picker-sheet"
      >
        <SearchField
          value={search}
          onChangeText={setSearch}
          placeholder="Search workspaces…"
          label="Search workspaces"
        />
        <NativeList
          sections={[
            {
              id: "workspaces",
              rows: filteredWorkspaces.length
                ? filteredWorkspaces.map((item) => ({
                    id: item.id,
                    title: item.name,
                    subtitle: workspaceLabel(
                      item,
                      Boolean(
                        item.active_operation || item.intervention_required,
                      ),
                    ),
                    symbol: "desktopcomputer",
                    selected: workspaceId === item.id,
                    busy: Boolean(
                      item.has_active_session || item.active_operation,
                    ),
                    onPress: () => chooseWorkspace(item),
                  }))
                : [{ id: "empty", title: "No workspaces found" }],
            },
          ]}
        />
      </NativeModal>

      <NativeModal
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
        <NativePicker
          label="Organization"
          value={session?.organizationId ?? ""}
          options={organizations.map((org) => ({
            value: org.id,
            label: org.name,
          }))}
          onChange={(id) => {
            setSheet(null);
            void selectOrganization(id);
          }}
        />
        <SectionTitle title="NOTIFICATIONS" />
        <NotificationSettings />
        <SectionTitle title="APPEARANCE" />
        <SegmentedControl
          values={["System", "Light", "Dark"]}
          selectedIndex={["system", "light", "dark"].indexOf(themeMode)}
          appearance={theme.scheme}
          onChange={({ nativeEvent }) =>
            setThemeMode(
              (["system", "light", "dark"] as const)[
                nativeEvent.selectedSegmentIndex
              ]!,
            )
          }
        />

        <Button
          title="Sign out"
          variant="danger"
          symbol="rectangle.portrait.and.arrow.right"
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
          style={{ alignSelf: "stretch", marginTop: 8 }}
          testID="sign-out"
        />
      </NativeModal>

      <ModelSheet
        visible={sheet === "settings"}
        onClose={() =>
          setSheet((current) => (current === "settings" ? null : current))
        }
        models={chosenModels}
        recentModelIds={recentModels.map((row) => row.model)}
        model={model}
        effort={effort}
        agents={agentOptions}
        engine={harness}
        mode={mode}
        unavailableMessage={`${harnessEngineLabel(harness, engines)} models unavailable. Check the provider account in Settings.`}
        onModelChange={selectModel}
        onEffortChange={(value) => {
          selectionDirty.current = true;
          setEffort(value);
        }}
        onAgentChange={selectAgent}
      />

      <NativeModal
        visible={sheet === "rename"}
        onClose={() => setSheet(null)}
        title="Rename conversation"
        testID="rename-session-sheet"
      >
        <NativeTextField
          label="Conversation name"
          value={renameText}
          onChangeText={setRenameText}
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
      </NativeModal>
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
    </View>
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

/**
 * Status banners float just below the translucent header; the transcript pads
 * its first message by their measured height so nothing stays hidden.
 */
function ChromeOverlay({
  children,
  onHeightChange,
}: React.PropsWithChildren<{
  onHeightChange: (height: number) => void;
}>): React.JSX.Element {
  const top = useChromeTopInset();
  return (
    <View
      pointerEvents="box-none"
      onLayout={(event) => onHeightChange(event.nativeEvent.layout.height)}
      style={[styles.overlay, { top }]}
    >
      {children}
    </View>
  );
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
  return (
    <NativeTextField
      value={value}
      onChangeText={onChangeText}
      placeholder={placeholder}
      label={label}
    />
  );
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
    <NativeModal
      visible={visible}
      onClose={onClose}
      title={file?.name ?? "Attachment"}
      testID="media-viewer-sheet"
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
    </NativeModal>
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
  notice: { paddingHorizontal: 12, paddingTop: 7 },
  overlay: { position: "absolute", left: 0, right: 0, zIndex: 3 },
  loading: {
    marginTop: 8,
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
    fontSize: 13,
    fontWeight: "500",
    marginTop: 18,
    marginBottom: 6,
  },
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
  docPreview: {
    minHeight: 210,
    borderRadius: 18,
    padding: 22,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
  },
});
