import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  BackHandler,
  Image,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
} from "react-native";
import Animated, { SlideInLeft, SlideOutLeft } from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  KeyboardController,
  KeyboardEvents,
} from "react-native-keyboard-controller";
import { ChevronDown, Ellipsis, SquarePen } from "lucide-react-native";
import type { HarnessConversation } from "@opencuria/client";
import { PublishedAppCard } from "../apps/PublishedAppCard";
import { appScope } from "../apps/appPresentation";
import { useApps } from "../apps/useApps";
import {
  CompanionSheet,
  type SheetDetent,
} from "../apps/companion/CompanionSheet";
import {
  CompanionChatList,
  CompanionChatListHeader,
} from "../apps/companion/CompanionChatList";
import { PointerOverlay } from "../apps/companion/PointerOverlay";
import {
  RecordingBar,
  recordingBarRegion,
} from "../apps/companion/RecordingBar";
import {
  DEFAULT_MENU_TRIGGERS,
  closeOpenAppFromCompanion,
  companionReady,
  menuTriggers,
  onCompanionOpen,
  reloadOpenApp,
  setCompanionTouchMode,
  setMenuTrigger,
  type CompanionMenuTrigger,
  type CompanionMenuTriggers,
  type CompanionRegion,
} from "../apps/companion/companionNative";
import {
  lastCompanionChat,
  rememberCompanionChat,
} from "../apps/companion/companionMemory";
import { useAppRecording } from "../apps/companion/useAppRecording";
import { WorkspaceChatView } from "../components/WorkspaceChatView";
import { ChromeInsetContext } from "../ui/chromeInset";
import { ComposerAction } from "../ui/ComposerAction";
import { NativeMenu, type NativeMenuAction } from "../ui/NativeMenu";
import { Banner } from "../ui/primitives";
import { statusColor } from "../ui/statusColors";
import { useTheme } from "../ui/theme";
import { showToast } from "../ui/toast";
import { useWorkspaceChat } from "../state/useWorkspaceChat";
import { useSession } from "./SessionContext";

/** What the native runtime tells the companion about the open app. */
export interface AppCompanionProps {
  appId: string;
  appName: string;
  versionId?: string;
  versionName?: string;
  /** The app's workspace, when the install knows it. */
  workspaceId?: string | null;
  /** `appScope` of the account the app was installed for. */
  scope?: string | null;
  /** `file://` URI of the app's icon. */
  iconUri?: string | null;
  /** Open right away (a menu trigger created the companion). */
  open?: boolean;
}

const OFFLINE_SHEETS = new Set(["chats"]);
const TRIGGERS: Array<{
  id: CompanionMenuTrigger;
  title: string;
  symbol: string;
}> = [
  {
    id: "shake",
    title: "Open by shaking",
    symbol: "iphone.radiowaves.left.and.right",
  },
  {
    id: "threeFingerPress",
    title: "Open with a three-finger hold",
    symbol: "hand.raised",
  },
  { id: "button", title: "Show the menu button", symbol: "circle.circle" },
];

/** Opens and closes the companion's sheet, as the native triggers ask. */
function useCompanionSheet(initiallyOpen: boolean) {
  const [open, setOpen] = useState(initiallyOpen);
  /** Open, or still sliding away. */
  const [shown, setShown] = useState(initiallyOpen);
  const [detent, setDetent] = useState<SheetDetent>("half");
  useEffect(() => {
    companionReady();
    const subscription = onCompanionOpen(() => {
      setDetent("half");
      setShown(true);
      setOpen(true);
    });
    return () => subscription.remove();
  }, []);
  const show = useCallback(() => {
    setShown(true);
    setOpen(true);
  }, []);
  const hide = useCallback(() => {
    void KeyboardController.dismiss().catch(() => undefined);
    setOpen(false);
  }, []);
  const hidden = useCallback(() => setShown(false), []);
  return { open, shown, detent, setDetent, show, hide, hidden };
}

/**
 * The app companion: a sheet over an open app with OpenCuria's chat for the
 * app's workspace, and "Record" to talk while using the app. Rendered by
 * OpenCuria's own React instance (`AppRegistry` name `OpenCuriaAppCompanion`)
 * into a layer above the app; the layer passes touches to the app wherever
 * the companion shows nothing (`setCompanionTouchMode`).
 */
export function AppCompanionScreen(
  props: AppCompanionProps,
): React.JSX.Element {
  const { client, session, user, offline, booting } = useSession();
  const account = user
    ? appScope(client?.api.baseUrl ?? "", session?.organizationId, user.id)
    : null;
  const sheet = useCompanionSheet(props.open ?? true);
  const [workspaceId, setWorkspaceId] = useState(props.workspaceId ?? null);
  const [lookupFailed, setLookupFailed] = useState(false);
  const [initialSession, setInitialSession] = useState<
    string | null | undefined
  >(undefined);
  const ownAccount = !props.scope || props.scope === account;

  // Installs from before the companion did not keep their workspace.
  useEffect(() => {
    if (workspaceId || !client || !ownAccount || offline) return;
    let alive = true;
    client.apps
      .get(props.appId)
      .then((app) => {
        if (!alive) return;
        if (app.workspace_id) setWorkspaceId(app.workspace_id);
        else setLookupFailed(true);
      })
      .catch(() => alive && setLookupFailed(true));
    return () => {
      alive = false;
    };
  }, [client, offline, ownAccount, props.appId, workspaceId]);

  useEffect(() => {
    if (!account) return;
    let alive = true;
    void lastCompanionChat(account, props.appId).then((id) => {
      if (alive) setInitialSession(id);
    });
    return () => {
      alive = false;
    };
  }, [account, props.appId]);

  const ready =
    Boolean(user && ownAccount && workspaceId) && initialSession !== undefined;
  if (ready && account)
    return (
      <CompanionWorkspace
        {...props}
        account={account}
        workspaceId={workspaceId!}
        initialSessionId={initialSession ?? null}
        sheet={sheet}
      />
    );
  const message = !user
    ? booting
      ? null
      : "Sign in to OpenCuria to chat about this app."
    : !ownAccount
      ? "This app was installed with another OpenCuria account. Switch to that account to chat about it."
      : lookupFailed || offline
        ? offline
          ? "You’re offline. Chatting about this app needs a connection to OpenCuria."
          : "The workspace this app comes from is no longer available."
        : null;
  return <CompanionShell {...props} sheet={sheet} message={message} />;
}

type SheetState = ReturnType<typeof useCompanionSheet>;

/** The sheet without a chat: signed out, another account, or still loading. */
function CompanionShell({
  appName,
  versionName,
  iconUri,
  sheet,
  message,
}: AppCompanionProps & {
  sheet: SheetState;
  message: string | null;
}): React.JSX.Element {
  const { theme } = useTheme();
  useCompanionLayer(sheet.shown, null);
  useBackClose(sheet.shown ? () => (sheet.hide(), true) : null);
  const triggers = useMenuTriggers();
  return (
    <CompanionSheet
      open={sheet.open}
      detent="half"
      onDetentChange={() => undefined}
      onDismiss={sheet.hide}
      onHidden={sheet.hidden}
      testID="companion-sheet"
      header={
        <View style={styles.header}>
          <AppIcon uri={iconUri} />
          <View style={styles.titleButton}>
            <Text
              numberOfLines={1}
              style={[styles.title, { color: theme.text }]}
            >
              {appName}
            </Text>
            {versionName ? (
              <Text
                numberOfLines={1}
                style={[styles.subtitle, { color: theme.mutedText }]}
              >
                Version {versionName}
              </Text>
            ) : null}
          </View>
          <AppMenu
            triggers={triggers.value}
            onTriggers={triggers.set}
            chatActions={[]}
            onChatAction={() => undefined}
            onReload={sheet.hide}
          />
        </View>
      }
    >
      <View style={styles.shell}>
        {message ? (
          <Text style={[styles.shellText, { color: theme.mutedText }]}>
            {message}
          </Text>
        ) : (
          <ActivityIndicator color={theme.mutedText} />
        )}
      </View>
    </CompanionSheet>
  );
}

function CompanionWorkspace({
  appId,
  appName,
  versionName,
  iconUri,
  account,
  workspaceId,
  initialSessionId,
  sheet,
}: AppCompanionProps & {
  account: string;
  workspaceId: string;
  initialSessionId: string | null;
  sheet: SheetState;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { reconnecting, retry } = useSession();
  const [page, setPage] = useState<"chat" | "chats">("chat");
  /** The recording bar rests at the top, where apps rarely put controls. */
  const [barAtTop, setBarAtTop] = useState(true);
  const triggers = useMenuTriggers();
  const [starting, setStarting] = useState(false);
  const askForSettings = useCallback(
    () =>
      showToast({
        title: "Set this up in OpenCuria",
        message: "Close the app and open Settings in OpenCuria.",
        tone: "info",
      }),
    [],
  );
  const chat = useWorkspaceChat({
    workspaceId,
    initialSessionId,
    offlineSheets: OFFLINE_SHEETS,
    onOpenSettings: askForSettings,
  });
  const recording = useAppRecording({
    client: chat.client,
    account,
    app: {
      app_id: appId,
      name: appName,
      ...(versionName ? { version: versionName } : {}),
      platform: Platform.OS === "ios" ? "ios" : "android",
    },
  });
  const recordingPhase = recording.state.phase;
  const recordingActive = recordingPhase !== "idle";
  const apps = useApps(chat.client, account, undefined, {
    offline: chat.offline,
  });
  const renderPublishedApp = useCallback(
    (reference: Parameters<typeof PublishedAppCard>[0]["reference"]) => (
      <PublishedAppCard reference={reference} apps={apps} />
    ),
    [apps],
  );

  const activeSessionId = chat.chat.activeSessionId;
  useEffect(() => {
    if (activeSessionId)
      void rememberCompanionChat(account, appId, activeSessionId);
  }, [account, appId, activeSessionId]);

  // The open keyboard needs the room of the full sheet.
  const { setDetent } = sheet;
  useEffect(() => {
    if (!sheet.open) return;
    const subscription = KeyboardEvents.addListener("keyboardWillShow", () =>
      setDetent("full"),
    );
    return () => subscription.remove();
  }, [setDetent, sheet.open]);

  const windowSize = useWindowDimensions();
  const insets = useSafeAreaInsets();
  const region = recordingBarRegion(windowSize, insets, barAtTop);
  // While a recording runs, the bar takes over as soon as the sheet leaves.
  const barShown = recordingActive && !sheet.open;
  useCompanionLayer(
    sheet.shown && !barShown,
    recordingActive ? { mode: recording.state.mode, region } : null,
  );
  useBackClose(
    page === "chats"
      ? () => (setPage("chat"), true)
      : sheet.shown
        ? () => (sheet.hide(), true)
        : recordingActive && recording.state.mode === "point"
          ? () => (recording.setMode("use"), true)
          : null,
  );

  /**
   * The layer switches right away, before React renders the new mode, so the
   * next touch already goes where the bar says.
   */
  const changeMode = (mode: "use" | "point") => {
    setCompanionTouchMode(mode === "point" ? "all" : "regions", [region]);
    recording.setMode(mode);
  };

  const startRecording = async () => {
    const scope = chat.draftScope;
    if (!scope || starting || chat.voice?.recording) return;
    setStarting(true);
    try {
      if (await recording.start(scope)) {
        setPage("chat");
        // The app takes touches while the sheet slides away.
        setCompanionTouchMode("regions", [region]);
        sheet.hide();
      }
    } finally {
      setStarting(false);
    }
  };
  const finishRecording = () => {
    void recording.finish();
    sheet.setDetent("half");
    sheet.show();
  };
  const cancelRecording = () => {
    recording.cancel();
    sheet.setDetent("half");
    sheet.show();
  };

  const conversations = useMemo(
    () =>
      chat.conversations
        .filter((row) => row.workspace_id === workspaceId)
        .sort((a, b) => b.last_message_at.localeCompare(a.last_message_at)),
    [chat.conversations, workspaceId],
  );
  const title = chat.chat.activeSession
    ? chat.activeConversation?.title || chat.chat.activeSession.title || "Chat"
    : chat.navigationReady
      ? "New chat"
      : " ";
  const subtitle = [appName, chat.workspace?.name].filter(Boolean).join(" · ");
  const openChats = () => {
    void KeyboardController.dismiss().catch(() => undefined);
    setPage("chats");
  };
  const chatActions: NativeMenuAction[] = [
    ...(chat.chat.activeSession && !chat.offline
      ? [
          {
            id: "rename",
            title: "Rename chat",
            symbol: "pencil",
            disabled: chat.isTransitioning,
          },
        ]
      : []),
    ...(chat.activeConversation && !chat.offline
      ? [
          {
            id: "delete",
            title: "Delete chat",
            symbol: "trash",
            destructive: true,
            disabled: chat.isTransitioning || chat.actionBusy,
          },
        ]
      : []),
  ];
  const chatAction = (id: string) => {
    if (id === "rename" && chat.chat.activeSession)
      chat.rename(chat.chat.activeSession);
    else if (id === "delete" && chat.activeConversation)
      chat.confirmDelete(chat.activeConversation);
  };
  const listAction = (row: HarnessConversation, action: string) => {
    if (action === "rename") chat.rename(row);
    else if (action === "read") void chat.markConversation(row);
    else chat.confirmDelete(row);
  };

  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="box-none">
      {barShown && recording.state.mode === "point" ? <PointerOverlay /> : null}
      {barShown ? (
        <RecordingBar
          phase={recordingPhase}
          meter={recording.meter}
          mode={recording.state.mode}
          onModeChange={changeMode}
          onCancel={cancelRecording}
          onFinish={finishRecording}
          atTop={barAtTop}
          onAtTopChange={setBarAtTop}
        />
      ) : null}
      <CompanionSheet
        open={sheet.open}
        detent={sheet.detent}
        onDetentChange={sheet.setDetent}
        onDismiss={sheet.hide}
        onHidden={sheet.hidden}
        testID="companion-sheet"
        header={
          page === "chats" ? (
            <CompanionChatListHeader
              workspaceName={chat.workspace?.name ?? ""}
              canCreate={chat.ready}
              onBack={() => setPage("chat")}
              onCreate={() => {
                setPage("chat");
                chat.newChat();
              }}
            />
          ) : (
            <View style={styles.header}>
              <Pressable
                style={styles.titleButton}
                onPress={openChats}
                accessibilityRole="button"
                accessibilityLabel={`Chats. ${title.trim() || "New chat"}`}
                accessibilityHint="Shows every chat of the app's workspace"
                testID="companion-title"
              >
                <View style={styles.titleRow}>
                  <Text
                    numberOfLines={1}
                    style={[
                      styles.title,
                      styles.titleText,
                      { color: theme.text },
                    ]}
                  >
                    {title}
                  </Text>
                  <ChevronDown
                    size={16}
                    strokeWidth={2.4}
                    color={theme.mutedText}
                  />
                </View>
                <Text
                  numberOfLines={1}
                  style={[styles.subtitle, { color: theme.mutedText }]}
                >
                  {subtitle}
                </Text>
              </Pressable>
              <RecordButton
                busy={starting || recordingActive}
                disabled={
                  chat.offline ||
                  !chat.navigationReady ||
                  Boolean(chat.voice?.recording)
                }
                onPress={() => void startRecording()}
              />
              <ComposerAction
                label="New chat"
                icon={SquarePen}
                disabled={!chat.ready}
                onPress={() => {
                  setPage("chat");
                  chat.newChat();
                }}
                testID="companion-new-chat"
              />
              <AppMenu
                triggers={triggers.value}
                onTriggers={triggers.set}
                chatActions={chatActions}
                onChatAction={chatAction}
                onReload={sheet.hide}
              />
            </View>
          )
        }
      >
        {recording.state.notice ? (
          <View style={styles.notice}>
            <Banner
              tone={
                recording.state.notice.tone === "error" ? "error" : "neutral"
              }
              message={recording.state.notice.message}
              onDismiss={recording.dismissNotice}
              testID="companion-recording-notice"
            />
          </View>
        ) : null}
        <ChromeInsetContext.Provider value={0}>
          <View style={styles.body}>
            <WorkspaceChatView
              chat={chat}
              scopeKey={`${account}:${workspaceId}`}
              renderPublishedApp={renderPublishedApp}
              onOpenSettings={askForSettings}
              onRetryConnection={() => void retry()}
              reconnecting={reconnecting}
              offlineMessage="You’re offline. Saved chats of this workspace still open."
              emptyText={
                chat.offline
                  ? conversations.length
                    ? "Open a saved chat from the chat list."
                    : "No chats of this workspace are saved on this device."
                  : undefined
              }
            />
            {page === "chats" ? (
              <Animated.View
                entering={SlideInLeft.duration(220)}
                exiting={SlideOutLeft.duration(180)}
                style={[
                  StyleSheet.absoluteFill,
                  { backgroundColor: theme.background },
                ]}
              >
                <CompanionChatList
                  conversations={conversations}
                  activeSessionId={activeSessionId}
                  offline={chat.offline}
                  actionsDisabled={chat.isTransitioning}
                  onSelect={(row) => {
                    setPage("chat");
                    chat.openConversation(row);
                  }}
                  onAction={listAction}
                />
              </Animated.View>
            ) : null}
            {/* The transcript fades out beneath the header. */}
            <View
              pointerEvents="none"
              style={[
                styles.fade,
                {
                  experimental_backgroundImage: `linear-gradient(to bottom, ${theme.background} 0%, ${theme.background}00 100%)`,
                },
              ]}
            />
          </View>
        </ChromeInsetContext.Provider>
      </CompanionSheet>
    </View>
  );
}

/** The ways that open the companion, as the device keeps them. */
function useMenuTriggers(): {
  value: CompanionMenuTriggers;
  set: (triggers: CompanionMenuTriggers) => void;
} {
  const [value, set] = useState(DEFAULT_MENU_TRIGGERS);
  useEffect(() => {
    let alive = true;
    void menuTriggers().then((loaded) => alive && set(loaded));
    return () => {
      alive = false;
    };
  }, []);
  return { value, set };
}

/** Which touches the native layer gives the companion. */
function useCompanionLayer(
  sheetShown: boolean,
  recording: { mode: "use" | "point"; region: CompanionRegion } | null,
): void {
  const mode = recording?.mode ?? null;
  const region = recording?.region ?? null;
  const regionKey = region?.join(",") ?? "";
  useEffect(() => {
    if (sheetShown) setCompanionTouchMode("all");
    else if (mode === "point" && region) setCompanionTouchMode("all", [region]);
    else if (mode === "use" && region)
      setCompanionTouchMode("regions", [region]);
    else setCompanionTouchMode("none");
    // `regionKey` identifies `region`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sheetShown, mode, regionKey]);
}

/** Android Back while the companion takes touches: `handler` returns true. */
function useBackClose(handler: (() => boolean) | null): void {
  useEffect(() => {
    if (!handler) return;
    const subscription = BackHandler.addEventListener(
      "hardwareBackPress",
      handler,
    );
    return () => subscription.remove();
  }, [handler]);
}

function AppIcon({ uri }: { uri?: string | null }): React.JSX.Element {
  const { theme } = useTheme();
  return uri ? (
    <Image
      source={{ uri }}
      style={styles.icon}
      accessibilityIgnoresInvertColors
    />
  ) : (
    <View style={[styles.icon, { backgroundColor: theme.controlFill }]} />
  );
}

/** "Record": talk while using the app; its one color is the recording red. */
function RecordButton({
  busy,
  disabled,
  onPress,
}: {
  busy: boolean;
  disabled: boolean;
  onPress: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const inactive = busy || disabled;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="Record"
      accessibilityHint="Talk while you use the app; taps, screens and logs are recorded for the agent"
      accessibilityState={{ disabled: inactive, busy }}
      disabled={inactive}
      onPress={onPress}
      hitSlop={4}
      testID="companion-record"
      style={({ pressed }) => [
        styles.record,
        {
          backgroundColor: theme.controlFill,
          opacity: disabled && !busy ? 0.45 : pressed ? 0.7 : 1,
        },
      ]}
    >
      {busy ? (
        <ActivityIndicator size="small" color={theme.mutedText} />
      ) : (
        <View
          style={[
            styles.recordDot,
            { backgroundColor: statusColor("critical", theme) },
          ]}
        />
      )}
      <Text style={[styles.recordText, { color: theme.text }]}>Record</Text>
    </Pressable>
  );
}

/** Reload and Close app, chat actions, and the ways that open the companion. */
function AppMenu({
  triggers,
  onTriggers,
  chatActions,
  onChatAction,
  onReload,
}: {
  onReload: () => void;
  triggers: CompanionMenuTriggers;
  onTriggers: (triggers: CompanionMenuTriggers) => void;
  chatActions: NativeMenuAction[];
  onChatAction: (id: string) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const enabled = TRIGGERS.filter((item) => triggers[item.id]).length;
  const actions: NativeMenuAction[] = [
    { id: "reload", title: "Reload app", symbol: "arrow.clockwise" },
    { id: "close", title: "Close app", symbol: "xmark.circle" },
    ...chatActions,
    ...TRIGGERS.map((item) => ({
      id: `trigger:${item.id}`,
      title: item.title,
      symbol: item.symbol,
      selected: triggers[item.id],
      // The last way stays on, so the companion can always be opened.
      disabled: triggers[item.id] && enabled === 1,
    })),
  ];
  return (
    <NativeMenu
      label="App"
      accessibilityLabel="More"
      actions={actions}
      onSelect={(id) => {
        if (id === "reload") {
          // The app starts over in view.
          onReload();
          void reloadOpenApp();
        } else if (id === "close") void closeOpenAppFromCompanion();
        else if (id.startsWith("trigger:")) {
          const trigger = id.slice("trigger:".length) as CompanionMenuTrigger;
          void setMenuTrigger(trigger, !triggers[trigger]).then(onTriggers);
        } else onChatAction(id);
      }}
      style={styles.menu}
      testID="companion-menu"
    >
      <View style={styles.menuIcon}>
        <Ellipsis color={theme.text} size={22} />
      </View>
    </NativeMenu>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingStart: 20,
    paddingEnd: 6,
    paddingBottom: 6,
    gap: 6,
  },
  icon: { width: 34, height: 34, borderRadius: 9 },
  titleButton: { flex: 1, minWidth: 0, paddingStart: 4, paddingVertical: 2 },
  titleRow: { flexDirection: "row", alignItems: "center", gap: 3 },
  title: { fontSize: 17, fontWeight: "600" },
  titleText: { flexShrink: 1 },
  subtitle: { fontSize: 13, marginTop: 1 },
  record: {
    height: 36,
    borderRadius: 18,
    paddingHorizontal: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
  },
  recordDot: { width: 10, height: 10, borderRadius: 5 },
  recordText: { fontSize: 15, fontWeight: "600" },
  menu: { width: 44, height: 44 },
  menuIcon: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  notice: { paddingHorizontal: 12, paddingBottom: 6 },
  body: { flex: 1 },
  fade: { position: "absolute", top: 0, left: 0, right: 0, height: 16 },
  shell: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 32,
    paddingBottom: 48,
  },
  shellText: { fontSize: 15, lineHeight: 21, textAlign: "center" },
});
