import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  BackHandler,
  Platform,
  StyleSheet,
  Text,
  View,
  useWindowDimensions,
} from "react-native";
import Animated, { useAnimatedStyle } from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  KeyboardController,
  KeyboardEvents,
} from "react-native-keyboard-controller";
import { CircleDot, SquarePen } from "lucide-react-native";
import type { PointerMode } from "@opencuria/chat-core/visualRecording";
import type { HarnessConversation } from "@opencuria/client";
import { PublishedAppCard } from "../apps/PublishedAppCard";
import { appScope } from "../apps/appPresentation";
import { useApps } from "../apps/useApps";
import {
  CompanionSheet,
  type SheetDetent,
} from "../apps/companion/CompanionSheet";
import { CompanionChatList } from "../apps/companion/CompanionChatList";
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
import { useAppSurface } from "../apps/companion/useAppSurface";
import { PointerOverlay } from "../components/PointerOverlay";
import { RecordingBar, recordingBarRegion } from "../components/RecordingBar";
import { recordingHint } from "../components/RecordingOverlay";
import {
  WorkspaceChatSheets,
  WorkspaceChatView,
} from "../components/WorkspaceChatView";
import { useChromeTopInset, useHiddenBottom } from "../ui/chromeInset";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { SheetStack } from "../ui/SheetStack";
import type { SheetPage, SheetPageMenu } from "../ui/SheetStack.types";
import { statusColor } from "../ui/statusColors";
import { useTheme } from "../ui/theme";
import { showToast } from "../ui/toast";
import { useWorkspaceChat } from "../state/useWorkspaceChat";
import {
  activeRecording,
  cancelRecording,
  finishRecording,
  setRecordingMode,
  useActiveRecording,
} from "../voice/voiceRecording";
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
/** The navigation bar before its first measurement. */
const DEFAULT_BAR_HEIGHT = Platform.OS === "ios" ? 44 : 60;
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
  /** The user dragged the sheet; the keyboard needs its full height. */
  const dragged = useCallback((next: SheetDetent) => {
    if (next === "half")
      void KeyboardController.dismiss().catch(() => undefined);
    setDetent(next);
  }, []);
  /** The drag area below the grabber follows the measured bar. */
  const [barHeight, setBarHeight] = useState(DEFAULT_BAR_HEIGHT);
  return {
    open,
    shown,
    detent,
    setDetent,
    dragged,
    show,
    hide,
    hidden,
    barHeight,
    setBarHeight,
  };
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
  sheet,
  message,
}: AppCompanionProps & {
  sheet: SheetState;
  message: string | null;
}): React.JSX.Element {
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
      barHeight={sheet.barHeight}
      testID="companion-sheet"
    >
      <SheetStack
        root={{
          title: appName,
          subtitle: versionName ? `Version ${versionName}` : undefined,
          testID: "companion-app",
          menu: appMenu({
            triggers: triggers.value,
            onTriggers: triggers.set,
            onReload: sheet.hide,
          }),
          children: <ShellMessage message={message} />,
        }}
        detailShown={false}
        onDetailShownChange={() => undefined}
        onBarHeight={sheet.setBarHeight}
      />
    </CompanionSheet>
  );
}

/** Centered in what the sheet shows of itself, below its bar. */
function ShellMessage({ message }: { message: string | null }) {
  const { theme } = useTheme();
  const top = useChromeTopInset();
  const hiddenBottom = useHiddenBottom();
  const visible = useAnimatedStyle(() => ({
    paddingBottom: Math.max(0, hiddenBottom.value),
  }));
  return (
    <Animated.View style={[styles.shell, { paddingTop: top }, visible]}>
      {message ? (
        <Text style={[styles.shellText, { color: theme.mutedText }]}>
          {message}
        </Text>
      ) : (
        <ActivityIndicator color={theme.mutedText} />
      )}
    </Animated.View>
  );
}

function CompanionWorkspace({
  appId,
  appName,
  versionName,
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
  /** Over the chat list it rests at the bottom, below the sheet's bar. */
  const [sheetBarAtTop, setSheetBarAtTop] = useState(false);
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
    overApp: true,
  });
  // One recording at a time, from Record or the composer's microphone.
  const recording = useActiveRecording();
  const recordingActive = recording !== null;
  const recordingMode = recording?.mode ?? "use";
  // The app is the surface a recording shows while the sheet is away.
  useAppSurface(recordingActive && !sheet.open, {
    app_id: appId,
    name: appName,
    ...(versionName ? { version: versionName } : {}),
    platform: Platform.OS === "ios" ? "ios" : "android",
  });
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
  // While a recording runs, the bar takes over wherever the composer is out
  // of sight: over the app once the sheet leaves, and over the chat list.
  const overApp = recordingActive && !sheet.open;
  const barShown = overApp || (recordingActive && page === "chats");
  useCompanionLayer(
    sheet.shown && !overApp,
    recordingActive ? { mode: recordingMode, region } : null,
  );
  // Android Back follows the pages: from the chat to the chat list, from
  // there out of the sheet.
  useBackClose(
    sheet.shown
      ? page === "chat"
        ? () => (setPage("chats"), true)
        : () => (sheet.hide(), true)
      : recordingActive && recordingMode === "point"
        ? () => (setRecordingMode("use"), true)
        : null,
  );

  /**
   * The layer switches right away, before React renders the new mode, so the
   * next touch already goes where the bar says.
   */
  const changeMode = (mode: PointerMode) => {
    if (overApp)
      setCompanionTouchMode(mode === "point" ? "all" : "regions", [region]);
    setRecordingMode(mode);
  };

  const startRecording = async () => {
    if (!chat.draftScope || starting || recordingActive || !chat.voice) return;
    setStarting(true);
    try {
      if (await chat.voice.record()) {
        setPage("chat");
        // The app takes touches while the sheet slides away.
        setCompanionTouchMode("regions", [region]);
        sheet.hide();
      }
    } finally {
      setStarting(false);
    }
  };
  /** Back to the chat, where the text arrives in the composer. */
  const showChat = () => {
    setPage("chat");
    sheet.setDetent("half");
    sheet.show();
  };
  const finish = () => {
    finishRecording(false);
    showChat();
  };
  const cancel = () => {
    cancelRecording();
    showChat();
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
  const newChat = () => {
    setPage("chat");
    chat.newChat();
  };
  const chatActions: NativeMenuAction[] = [
    {
      id: "new",
      title: "New chat",
      symbol: "square.and.pencil",
      disabled: !chat.ready,
    },
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
    if (id === "new") newChat();
    else if (id === "rename" && chat.chat.activeSession)
      chat.rename(chat.chat.activeSession);
    else if (id === "delete" && chat.activeConversation)
      chat.confirmDelete(chat.activeConversation);
  };
  const listAction = (row: HarnessConversation, action: string) => {
    if (action === "rename") chat.rename(row);
    else if (action === "read") void chat.markConversation(row);
    else chat.confirmDelete(row);
  };

  const chatsPage: SheetPage = {
    title: "Chats",
    subtitle: chat.workspace?.name,
    testID: "companion-chats",
    actions: [
      {
        id: "new",
        label: "New chat",
        symbol: "square.and.pencil",
        icon: SquarePen,
        disabled: !chat.ready,
        onPress: newChat,
        testID: "companion-chats-new",
      },
    ],
    children: (
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
    ),
  };
  const recordingBusy = starting || recordingActive;
  const chatPage: SheetPage = {
    title,
    subtitle,
    testID: "companion-chat",
    actions: [
      {
        id: "record",
        label: "Record",
        accessibilityHint:
          "Talk while you use the app; taps, screens and logs are recorded for the agent",
        symbol: "record.circle",
        icon: CircleDot,
        // Its one color is the recording red.
        tint: statusColor("critical", theme),
        disabled:
          recordingBusy || chat.offline || !chat.navigationReady || !chat.voice,
        standalone: true,
        onPress: () => void startRecording(),
        testID: "companion-record",
      },
    ],
    menu: appMenu({
      triggers: triggers.value,
      onTriggers: triggers.set,
      chatActions,
      onChatAction: chatAction,
      onReload: sheet.hide,
    }),
    children: (
      <WorkspaceChatView
        chat={chat}
        // The chat list renames chats too, while this page is not mounted.
        sheets={false}
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
    ),
  };

  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="box-none">
      {overApp && recordingMode === "point" ? (
        <PointerOverlay testID="companion-pointer-overlay" />
      ) : null}
      <CompanionSheet
        open={sheet.open}
        detent={sheet.detent}
        onDetentChange={sheet.dragged}
        onDismiss={sheet.hide}
        onHidden={sheet.hidden}
        barHeight={sheet.barHeight}
        testID="companion-sheet"
      >
        <SheetStack
          root={chatsPage}
          detail={chatPage}
          detailShown={page === "chat"}
          onDetailShownChange={(shown) => {
            if (!shown)
              void KeyboardController.dismiss().catch(() => undefined);
            setPage(shown ? "chat" : "chats");
          }}
          onBarHeight={sheet.setBarHeight}
        />
      </CompanionSheet>
      {/* Above the sheet and its scrim: over the chat list it stays usable. */}
      {barShown && recording && activeRecording() ? (
        <RecordingBar
          phase={recording.phase}
          meter={activeRecording()!.meter}
          mode={recordingMode}
          modes={overApp}
          onModeChange={changeMode}
          onCancel={cancel}
          onFinish={finish}
          // Over the sheet it rests at the bottom, clear of the sheet's bar.
          atTop={overApp ? barAtTop : sheetBarAtTop}
          onAtTopChange={overApp ? setBarAtTop : setSheetBarAtTop}
          hint={recordingHint({ kind: "app", pointable: true })}
        />
      ) : null}
      <WorkspaceChatSheets chat={chat} onOpenSettings={askForSettings} />
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

/**
 * Chat actions, Reload and Close app, and the ways that open the companion,
 * as groups of the overflow menu.
 */
function appMenu({
  triggers,
  onTriggers,
  chatActions = [],
  onChatAction = () => undefined,
  onReload,
}: {
  triggers: CompanionMenuTriggers;
  onTriggers: (triggers: CompanionMenuTriggers) => void;
  chatActions?: NativeMenuAction[];
  onChatAction?: (id: string) => void;
  onReload: () => void;
}): SheetPageMenu {
  const enabled = TRIGGERS.filter((item) => triggers[item.id]).length;
  return {
    label: "More",
    testID: "companion-menu",
    sections: [
      { id: "chat", actions: chatActions },
      {
        id: "app",
        actions: [
          { id: "reload", title: "Reload app", symbol: "arrow.clockwise" },
          { id: "close", title: "Close app", symbol: "xmark.circle" },
        ],
      },
      {
        id: "triggers",
        actions: TRIGGERS.map((item) => ({
          id: `trigger:${item.id}`,
          title: item.title,
          symbol: item.symbol,
          selected: triggers[item.id],
          // The last way stays on, so the companion can always be opened.
          disabled: triggers[item.id] && enabled === 1,
        })),
      },
    ],
    onSelect: (id) => {
      if (id === "reload") {
        // The app starts over in view.
        onReload();
        void reloadOpenApp();
      } else if (id === "close") void closeOpenAppFromCompanion();
      else if (id.startsWith("trigger:")) {
        const trigger = id.slice("trigger:".length) as CompanionMenuTrigger;
        void setMenuTrigger(trigger, !triggers[trigger]).then(onTriggers);
      } else onChatAction(id);
    },
  };
}

const styles = StyleSheet.create({
  shell: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 32,
  },
  shellText: { fontSize: 15, lineHeight: 21, textAlign: "center" },
});
