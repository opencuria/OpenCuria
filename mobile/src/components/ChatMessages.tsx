import React, {
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  Animated,
  Keyboard,
  Platform,
  Pressable,
  ScrollView,
  Share,
  StyleSheet,
  Text,
  TextInput,
  View,
  useWindowDimensions,
  type GestureResponderEvent,
} from "react-native";
import { KeyboardAwareLegendList } from "@legendapp/list/keyboard";
import type {
  LegendListRef,
  OnViewableItemsChangedInfo,
} from "@legendapp/list/react-native";
import { Image } from "expo-image";
import {
  ArrowDown,
  ArrowRight,
  Check,
  ChevronRight,
  CircleDashed,
  CircleX,
  Copy,
  FileCode2,
  EllipsisVertical,
  GitBranch,
  LoaderCircle,
  MessageSquareText,
} from "lucide-react-native";
import type {
  HarnessMessage,
  HarnessPart,
  HarnessSession,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import {
  buildActivityMessageBlocks,
  findTodoAnchor,
  summarizeActivity,
  type ActivityMessageBlock,
} from "@opencuria/chat-core/harnessActivity";
import { elapsedMs, formatElapsed } from "@opencuria/chat-core/formatElapsed";
import { getMessageCopyText } from "@opencuria/chat-core/harnessMessageCopy";
import { formatSendTime } from "@opencuria/chat-core/sendLater";
import { HARNESS_PAGE_SIZE } from "@opencuria/chat-core/harnessPagination";
import {
  isOptimisticPrompt,
  sortHarnessMessages,
} from "@opencuria/chat-core/harnessReducer";
import { messageRenderKey } from "@opencuria/chat-core/optimisticPrompts";
import { toolAttachments } from "@opencuria/chat-core/harnessAttachments";
import { resolveChildSessionId } from "@opencuria/chat-core/harnessSubtaskActivity";
import {
  formatMessageUsage,
  resolveMessageUsage,
} from "@opencuria/chat-core/harnessUsage";
import {
  parseToolArguments,
  toolDisplayLabel,
} from "@opencuria/chat-core/toolDisplay";
import {
  messagePublishedApps,
  type PublishedAppReference,
} from "@opencuria/chat-core/publishedApps";
import { useChromeTopInset } from "../ui/chromeInset";
import { useChatLayout } from "./ChatLayout";
import {
  END_SLOP,
  EndPullArea,
  EndPullHint,
  distanceFromEnd,
  useEndPull,
  useEndPullState,
  type EndPullScrollHandlers,
} from "./EndPull";
import { NativeContextMenu } from "../ui/ContextMenu";
import { useTheme } from "../ui/theme";
import {
  Banner,
  Button,
  IconButton,
  NativeModal,
  NativeAction,
} from "../ui/primitives";
import { ChatMarkdown } from "./ChatMarkdown";
import { PromptImages, PromptParagraph } from "./PromptContent";
import { PromptFlight, TranscriptReadyContext } from "./PromptFlight";
import { promptLayout, type PromptLayout } from "./promptBlocks";
import { composerDisplay } from "./composerDisplay";
import {
  chatImages,
  galleryFor,
  imageMentionMarkdown,
  type MediaGallery,
} from "./chatGallery";
import {
  MediaGalleryContext,
  MediaViewer,
  type OpenMediaGallery,
} from "./MediaViewer";
import type { ChatMediaResolver } from "./useWorkspaceMedia";

export interface ChatMessagesProps {
  messages: HarnessMessage[];
  sessions?: HarnessSession[];
  todos?: HarnessTodo[];
  busy?: boolean;
  loading?: boolean;
  streamingMessageId?: string | null;
  disabled?: boolean;
  resolveMedia?: ChatMediaResolver;
  /** Edits a sent prompt; the host moves it into the composer. */
  onEdit?: (message: HarnessMessage) => void;
  /**
   * The prompt being edited. The transcript ends with it and notes that
   * sending the edit restarts the chat from there.
   */
  editingMessageId?: string | null;
  onFork?: (messageId: string) => void | Promise<void>;
  onDismissNotice?: (messageId: string) => void | Promise<void>;
  onCopy?: (message: HarnessMessage, text: string) => void | Promise<void>;
  onOpenSubtask?: (childSessionId: string) => void;
  onRequestPartDetail?: (
    sessionId: string,
    partId: string,
  ) => Promise<HarnessPart | void>;
  onOpenFile?: (path: string) => void;
  /** Parent-owned handler for non-image tool attachments such as PDFs. */
  onOpenAttachment?: (attachment: {
    uri: string;
    filename: string;
    mime: string;
  }) => void;
  mediaScopeKey?: string | number;
  /** Extra top padding for host overlays such as status banners. */
  contentInsetTop?: number;
  /**
   * Pulling up past the newest message calls this (e.g. to open workspace
   * tools). Only a touch that starts while the transcript rests at its end
   * arms the pull; scrolling to the end never opens it.
   */
  onPullUpAtEnd?: () => void;
  /** Hint shown while pulling, such as "Git, Desktop, Terminal & Files". */
  pullUpLabel?: string;
  /** Renders an app the agent sent to the phone, e.g. with an install button. */
  renderPublishedApp?: (app: PublishedAppReference) => React.ReactNode;
  /** Shown in place of an empty chat; defaults to an invitation to ask. */
  emptyText?: string;
  testID?: string;
}

/**
 * Renderer for apps in the transcript. A context, so that install progress
 * updates the card without re-rendering transcript rows.
 */
const PublishedAppContext = React.createContext<
  ChatMessagesProps["renderPublishedApp"] | null
>(null);

const VIEWABILITY = { itemVisiblePercentThreshold: 1 };
/** Message actions keep native touch targets; their glyphs align with the text. */
const ACTION_SIZE = Platform.OS === "ios" ? 44 : 36;
const ACTION_ICON = 17;
/** A prompt bubble fills at most this share of its row. */
const USER_BUBBLE_SHARE = 0.82;
const USER_BUBBLE_RADIUS = 24;
/** How long the iOS context menu takes to close after an action. */
const MENU_DISMISS_MS = 350;
/** Selection handles reach this far beyond a selected bubble. */
const HANDLE_SLOP = 16;

/** Reports a touch on the transcript, in page coordinates. */
type SelectionTouch = (pageX: number, pageY: number) => void;
/**
 * The prompt selected in place, if any. It ends its selection when the
 * transcript is touched elsewhere, as tapping beside selected text does.
 */
const PromptSelectionContext = React.createContext<{
  current: SelectionTouch | null;
}>({ current: null });
/** Stay pinned to the newest output while it streams in and grows. */
const FOLLOW_END = {
  animated: false,
  on: { dataChange: true, itemLayout: true, layout: true },
};

/**
 * Native ordered transcript. It fills the chat and scrolls beneath the header
 * and the docked composer; the keyboard shifts its content, never its layout.
 * While the reader stays at the end it follows new and streaming output.
 */
export function ChatMessages({
  messages,
  todos = [],
  busy = false,
  loading = false,
  streamingMessageId,
  disabled = false,
  resolveMedia,
  onEdit,
  editingMessageId = null,
  onFork,
  onCopy,
  onDismissNotice,
  onOpenSubtask,
  onRequestPartDetail,
  onOpenFile,
  onOpenAttachment,
  mediaScopeKey,
  contentInsetTop = 0,
  onPullUpAtEnd,
  pullUpLabel = "Workspace tools",
  renderPublishedApp,
  emptyText = "What can I help with?",
  testID = "chat-messages",
}: ChatMessagesProps): React.JSX.Element {
  const { theme } = useTheme();
  const layout = useChatLayout();
  const pullState = useEndPullState();
  const pull = useEndPull(pullState, loading ? undefined : onPullUpAtEnd);
  // Messages scroll beneath translucent navigation chrome and host overlays.
  const topInset = useChromeTopInset() + contentInsetTop;
  const listRef = useRef<LegendListRef>(null);
  const promptSelection = useRef<SelectionTouch | null>(null);
  const touchTranscript = useCallback(
    (event: GestureResponderEvent) =>
      promptSelection.current?.(
        event.nativeEvent.pageX,
        event.nativeEvent.pageY,
      ),
    [],
  );
  const { registerScrollToEnd } = layout;
  useEffect(
    () =>
      registerScrollToEnd(async () => {
        await listRef.current?.scrollToEnd({ animated: true });
      }),
    [registerScrollToEnd],
  );
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const visibleMessageIds = useRef(new Set<string>());
  const [visibleMessageIdsState, setVisibleMessageIdsState] = useState(
    new Set<string>(),
  );
  const onViewableItemsChanged = useCallback(
    ({ viewableItems }: OnViewableItemsChangedInfo<HarnessMessage>) => {
      const next = new Set(
        viewableItems
          .filter((item) => item.isViewable)
          .map((item) => messageRenderKey(item.item)),
      );
      visibleMessageIds.current = next;
      setVisibleMessageIdsState((previous) =>
        previous.size === next.size && [...next].every((id) => previous.has(id))
          ? previous
          : next,
      );
    },
    [],
  );
  // Live replies have no server position yet; the shared rule keeps them last.
  // An edit replaces everything after its prompt, so that is hidden meanwhile.
  const ordered = useMemo(() => {
    const sorted = sortHarnessMessages(messages);
    const edited = editingMessageId
      ? sorted.findIndex((message) => message.id === editingMessageId)
      : -1;
    return edited < 0 ? sorted : sorted.slice(0, edited + 1);
  }, [editingMessageId, messages]);
  // The prompt being edited becomes the end of the transcript.
  useEffect(() => {
    if (!editingMessageId) return;
    const frame = requestAnimationFrame(() => {
      void listRef.current?.scrollToEnd({ animated: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [editingMessageId]);
  // A tapped picture opens in a gallery of every picture in the chat, read
  // from the messages so it also reaches rows that are not rendered.
  const orderedRef = useRef(ordered);
  orderedRef.current = ordered;
  const [gallery, setGallery] = useState<MediaGallery | null>(null);
  const openGallery = useCallback<OpenMediaGallery>(
    (media, siblings) =>
      setGallery(galleryFor(media, chatImages(orderedRef.current), siblings)),
    [],
  );
  const viewer = gallery ? (
    <MediaViewer
      gallery={gallery}
      resolveMedia={resolveMedia}
      scope={String(mediaScopeKey ?? "")}
      onClose={() => setGallery(null)}
    />
  ) : null;
  // `todos` is a fresh clone per snapshot; its content decides the to-do card.
  const todosKey = JSON.stringify(todos);
  const liveTodos = useMemo(() => todos, [todosKey]);
  // The latest to-do call shows the session list in place of that call.
  const todoAnchor = useMemo(
    () => (liveTodos.length ? findTodoAnchor(ordered) : null),
    [liveTodos.length, ordered],
  );
  const todoMessageId = todoAnchor?.messageId ?? null;
  const todoPartId = todoAnchor?.partId ?? null;
  // Hosts often pass inline handlers. Stable ones keep `renderItem` unchanged
  // while a reply streams, so only the growing message renders again.
  const handleEdit = useLatestHandler(onEdit);
  const handleFork = useLatestHandler(onFork);
  const handleCopy = useLatestHandler(onCopy);
  const handleDismissNotice = useLatestHandler(onDismissNotice);
  const handleOpenSubtask = useLatestHandler(onOpenSubtask);
  const handleRequestPartDetail = useLatestHandler(onRequestPartDetail);
  const handleOpenFile = useLatestHandler(onOpenFile);
  const handleOpenAttachment = useLatestHandler(onOpenAttachment);
  const renderItem = useCallback(
    ({ item }: { item: HarnessMessage }) => (
      <MessageRow
        message={item}
        mediaVisible={visibleMessageIds.current.has(messageRenderKey(item))}
        mediaScopeKey={mediaScopeKey ?? item.session_id}
        disabled={disabled || busy}
        streaming={
          busy &&
          item.role === "assistant" &&
          (streamingMessageId
            ? item.id === streamingMessageId
            : item.completed_at == null)
        }
        resolveMedia={resolveMedia}
        todos={item.id === todoMessageId ? liveTodos : undefined}
        todoPartId={item.id === todoMessageId ? todoPartId : undefined}
        editing={item.id === editingMessageId}
        onEdit={handleEdit}
        onFork={handleFork}
        onCopy={handleCopy}
        onDismissNotice={handleDismissNotice}
        onOpenSubtask={handleOpenSubtask}
        onRequestPartDetail={handleRequestPartDetail}
        onOpenFile={handleOpenFile}
        onOpenAttachment={handleOpenAttachment}
      />
    ),
    [
      busy,
      disabled,
      editingMessageId,
      handleCopy,
      handleDismissNotice,
      handleEdit,
      handleFork,
      handleOpenAttachment,
      handleOpenFile,
      handleOpenSubtask,
      handleRequestPartDetail,
      liveTodos,
      resolveMedia,
      streamingMessageId,
      todoMessageId,
      todoPartId,
      visibleMessageIdsState,
      mediaScopeKey,
    ],
  );
  // A sent prompt keeps its row when the server's copy replaces it.
  const keyExtractor = useCallback(
    (message: HarnessMessage) => messageRenderKey(message),
    [],
  );
  // "Scroll to latest" follows the list's own end tracking, which includes the
  // keyboard and composer insets and also reacts to content growing below.
  const hasMessages = !loading && ordered.length > 0;
  // A list mounting with its first rows hides them until they are placed.
  const [listReady, setListReady] = useState(false);
  if (!hasMessages && listReady) setListReady(false);
  const onListReady = useCallback(() => setListReady(true), []);
  /**
   * The pull arms at the end with a little slack: the list's own flag is
   * exact to a point, while insets and fractional offsets keep a resting list
   * a few points short of it.
   */
  const updateAtEnd = useCallback(() => {
    const state = listRef.current?.getState();
    pullState.atEnd.value =
      !state || state.isAtEnd || state.distanceFromEnd <= END_SLOP;
  }, [pullState]);
  useEffect(() => {
    const state = hasMessages ? listRef.current?.getState() : undefined;
    if (!state) {
      setShowJumpToLatest(false);
      pullState.atEnd.value = true;
      return;
    }
    setShowJumpToLatest(!state.isNearEnd);
    updateAtEnd();
    const stopNearEnd = state.listen("isNearEnd", (nearEnd) => {
      setShowJumpToLatest(!nearEnd);
      updateAtEnd();
    });
    const stopAtEnd = state.listen("isAtEnd", updateAtEnd);
    return () => {
      stopNearEnd();
      stopAtEnd();
    };
  }, [hasMessages, pullState, updateAtEnd]);
  const pullScroll = pull.scroll;
  const scrollHandlers = useMemo<EndPullScrollHandlers>(
    () => ({
      ...pullScroll,
      onScroll: (event) => {
        pullState.atEnd.value =
          distanceFromEnd(event) <= END_SLOP ||
          Boolean(listRef.current?.getState().isAtEnd);
        pullScroll.onScroll(event);
      },
    }),
    [pullScroll, pullState],
  );
  const pullHint = !loading && onPullUpAtEnd ? (
    <EndPullHint
      progress={pull.progress}
      ready={pull.ready}
      label={pullUpLabel}
      bottom={layout.dockOffset + 12}
    />
  ) : null;
  const jumpToLatest = useCallback(() => {
    setShowJumpToLatest(false);
    void listRef.current?.scrollToEnd({ animated: true });
  }, []);

  if (!hasMessages) {
    return (
      <View style={styles.listRoot}>
        <EndPullArea gesture={pull.gesture}>
          <View style={styles.listRoot} collapsable={false}>
            <EmptyTranscript
              loading={loading}
              text={emptyText}
              topInset={topInset}
              testID={testID}
              scrollHandlers={pullScroll}
            />
          </View>
        </EndPullArea>
        {pullHint}
      </View>
    );
  }

  return (
    <MediaGalleryContext.Provider value={openGallery}>
      <PublishedAppContext.Provider value={renderPublishedApp ?? null}>
      <TranscriptReadyContext.Provider value={listReady}>
      <PromptSelectionContext.Provider value={promptSelection}>
      <View style={styles.listRoot} onTouchStart={touchTranscript}>
        <EndPullArea gesture={pull.gesture}>
          <View style={styles.listRoot} collapsable={false}>
            <KeyboardAwareLegendList
              ref={listRef}
              testID={testID}
              data={ordered}
              renderItem={renderItem}
              // Rendered rows update only for new item data or extraData, not for
              // a new renderItem. Row state such as media visibility lives in its
              // closure, so the callback itself is the extra data.
              extraData={renderItem}
              keyExtractor={keyExtractor}
              estimatedItemSize={220}
              drawDistance={600}
              onViewableItemsChanged={onViewableItemsChanged}
              viewabilityConfig={VIEWABILITY}
              initialScrollAtEnd
              onReady={onListReady}
              maintainScrollAtEnd={FOLLOW_END}
              maintainVisibleContentPosition
              recycleItems={false}
              contentInsetEndAdjustment={layout.dockHeight}
              keyboardOffset={layout.keyboardOffset}
              keyboardLiftBehavior="whenAtEnd"
              // Short transcripts must stay draggable in the inset area, so the
              // keyboard can be pulled down from anywhere (facebook/react-native#54123).
              applyWorkaroundForContentInsetHitTestBug={Platform.OS === "ios"}
              keyboardDismissMode={layout.keyboardDismissMode}
              keyboardShouldPersistTaps="handled"
              // Insets are owned by the keyboard-aware list, never by UIKit.
              automaticallyAdjustContentInsets={false}
              contentInsetAdjustmentBehavior="never"
              showsVerticalScrollIndicator={false}
              contentContainerStyle={[
                styles.listContent,
                { paddingTop: styles.listContent.paddingTop + topInset },
              ]}
              style={{ backgroundColor: theme.background }}
              {...scrollHandlers}
              scrollEventThrottle={16}
            />
          </View>
        </EndPullArea>
        <JumpToLatest visible={showJumpToLatest} onPress={jumpToLatest} />
        {pullHint}
      </View>
      </PromptSelectionContext.Provider>
      </TranscriptReadyContext.Provider>
      </PublishedAppContext.Provider>
      {viewer}
    </MediaGalleryContext.Provider>
  );
}

/** Floats just above the composer and rides along with the keyboard. */
function JumpToLatest({
  visible,
  onPress,
}: {
  visible: boolean;
  onPress: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { dockOffset, dockTranslateY } = useChatLayout();
  const appearance = useRef(new Animated.Value(visible ? 1 : 0)).current;
  useEffect(() => {
    Animated.spring(appearance, {
      toValue: visible ? 1 : 0,
      speed: 28,
      bounciness: 0,
      useNativeDriver: true,
    }).start();
  }, [appearance, visible]);
  const style = useMemo(
    () => [
      styles.jumpButton,
      {
        opacity: appearance,
        transform: [
          { translateY: Animated.add(dockTranslateY, -(dockOffset + 10)) },
          {
            scale: appearance.interpolate({
              inputRange: [0, 1],
              outputRange: [0.8, 1],
            }),
          },
        ],
      },
    ],
    [appearance, dockOffset, dockTranslateY],
  );
  return (
    <Animated.View
      pointerEvents={visible ? "box-none" : "none"}
      accessibilityElementsHidden={!visible}
      importantForAccessibility={visible ? "auto" : "no-hide-descendants"}
      style={style}
    >
      <IconButton
        icon={<ArrowDown size={20} color={theme.text} />}
        symbol="arrow.down"
        tone="floating"
        label="Scroll to latest message"
        onPress={onPress}
        testID="chat-scroll-to-latest"
      />
    </Animated.View>
  );
}

/**
 * New chats greet in the middle of the space left between header, composer
 * and keyboard. It still scrolls, so the keyboard can be dragged away here too.
 */
function EmptyTranscript({
  loading,
  text,
  topInset,
  testID,
  scrollHandlers,
}: {
  loading: boolean;
  text: string;
  topInset: number;
  testID: string;
  scrollHandlers: EndPullScrollHandlers;
}): React.JSX.Element {
  const { theme } = useTheme();
  const { dockOffset, dockTranslateY, keyboardDismissMode } = useChatLayout();
  const centered = useMemo(
    () => ({
      transform: [
        {
          translateY: Animated.add(
            Animated.multiply(dockTranslateY, 0.5),
            (topInset - dockOffset) / 2,
          ),
        },
      ],
    }),
    [dockOffset, dockTranslateY, topInset],
  );
  return (
    <ScrollView
      testID={testID}
      style={{ backgroundColor: theme.background }}
      contentContainerStyle={styles.empty}
      keyboardDismissMode={keyboardDismissMode}
      keyboardShouldPersistTaps="handled"
      showsVerticalScrollIndicator={false}
      alwaysBounceVertical
      scrollEventThrottle={16}
      {...scrollHandlers}
    >
      <Animated.View style={centered}>
        {loading ? (
          <ActivityIndicator
            testID="chat-loading"
            accessibilityLabel="Loading chat"
            color={theme.mutedText}
          />
        ) : (
          <Text style={[styles.emptyTitle, { color: theme.text }]}>
            {text}
          </Text>
        )}
      </Animated.View>
    </ScrollView>
  );
}

/** A stable callback that calls the latest handler; `undefined` without one. */
function useLatestHandler<Handler extends (...args: never[]) => unknown>(
  handler: Handler | undefined,
): Handler | undefined {
  const latest = useRef(handler);
  latest.current = handler;
  const stable = useCallback(
    (...args: Parameters<Handler>) => latest.current?.(...args),
    [],
  ) as unknown as Handler;
  return handler ? stable : undefined;
}

interface MessageRowProps extends Omit<
  ChatMessagesProps,
  | "messages"
  | "sessions"
  | "todos"
  | "busy"
  | "loading"
  | "streamingMessageId"
  | "testID"
> {
  message: HarnessMessage;
  streaming: boolean;
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
  /** Session to-dos, passed only to the message that hosts the to-do card. */
  todos?: HarnessTodo[];
  /** The hosting `todowrite` part; null appends the card after the message. */
  todoPartId?: string | null;
  /** This prompt is being edited in the composer. */
  editing: boolean;
}

const MessageRow = memo(function MessageRow({
  message,
  streaming,
  disabled,
  resolveMedia,
  todos,
  todoPartId,
  editing,
  onEdit,
  onFork,
  onCopy,
  onDismissNotice,
  onOpenSubtask,
  onRequestPartDetail,
  onOpenFile,
  onOpenAttachment,
  mediaVisible,
  mediaScopeKey,
}: MessageRowProps): React.JSX.Element {
  const { theme } = useTheme();
  const [actionsOpen, setActionsOpen] = useState(false);
  const isUser = message.role === "user";
  const prompt = useMemo(
    () => (isUser ? promptLayout(message.content) : null),
    [isUser, message.content],
  );
  const showTodos = Boolean(todos?.length);
  const blocks = useMemo(
    () =>
      buildActivityMessageBlocks(
        [...message.parts].sort(
          (left, right) => (left.position ?? 0) - (right.position ?? 0),
        ),
        { todoPartId: showTodos ? todoPartId : null, finished: !streaming },
      ),
    [message.parts, showTodos, todoPartId, streaming],
  );
  // Apps the agent sent show below the message, not where it sent them.
  const apps = useMemo(
    () => (isUser ? [] : messagePublishedApps(message.parts)),
    [isUser, message.parts],
  );
  const duration = elapsedMs(message.created_at, message.completed_at);
  const elapsedLabel = duration === null ? "" : formatElapsed(duration);
  const copyText = getMessageCopyText(message, streaming);
  const forkMessage = useCallback(
    async (messageId: string) => {
      try {
        await onFork?.(messageId);
      } catch {
        /* Parent owns the fork workflow and its failure feedback. */
      }
    },
    [onFork],
  );
  const doCopy = useCallback(async () => {
    if (!copyText) return;
    try {
      const Clipboard = await import("expo-clipboard");
      await Clipboard.setStringAsync(copyText);
      try {
        await onCopy?.(message, copyText);
      } catch {
        /* Clipboard succeeded; parent callback is informational. */
      }
    } catch {
      /* Clipboard can be unavailable on a platform. */
    }
  }, [copyText, message, onCopy]);

  // A prompt already being edited is not offered again; a local echo has no
  // server copy to edit yet.
  const editPrompt =
    isUser &&
    !editing &&
    !disabled &&
    !message.id.startsWith("local-user-") &&
    onEdit
      ? () => onEdit(message)
      : undefined;

  return (
    <MessageFrame message={message}>
      {isUser ? (
        <PromptBubble
          messageId={message.id}
          text={copyText}
          sentAt={message.created_at}
          onCopy={() => {
            void doCopy();
          }}
          onEdit={editPrompt}
        >
          <UserMessageContent
            markdown={message.content}
            layout={prompt ?? undefined}
            mediaScopeKey={mediaScopeKey}
            mediaVisible={mediaVisible}
            resolveMedia={resolveMedia}
            onOpenFile={onOpenFile}
            selectable={false}
            testID={`chat-message-markdown-${message.id}`}
          />
        </PromptBubble>
      ) : (
        <View style={styles.assistantBody}>
          {message.error && !message.notice_dismissed_at ? (
            <Banner
              tone="neutral"
              title="Run failed"
              message={message.error}
              onDismiss={
                onDismissNotice
                  ? () => {
                      void Promise.resolve(onDismissNotice(message.id)).catch(
                        () => undefined,
                      );
                    }
                  : undefined
              }
              testID={`chat-message-error-${message.id}`}
            />
          ) : null}
          {blocks.map((block, index) => (
            <BlockView
              key={blockKey(block, index)}
              block={block}
              elapsedLabel={elapsedLabel}
              streaming={streaming}
              todos={todos}
              mediaVisible={mediaVisible}
              mediaScopeKey={mediaScopeKey}
              resolveMedia={resolveMedia}
              onOpenFile={onOpenFile}
              onOpenSubtask={onOpenSubtask}
              onRequestPartDetail={onRequestPartDetail}
              onOpenAttachment={onOpenAttachment}
            />
          ))}
          {todos?.length && todoPartId === null ? (
            <TodoCard todos={todos} />
          ) : null}
          {message.finish === "aborted" &&
          !streaming &&
          !message.notice_dismissed_at &&
          !message.error ? (
            <Text style={{ color: theme.mutedText, fontSize: 13 }}>
              Run stopped.
            </Text>
          ) : null}
          {streaming ? (
            <View
              accessibilityLabel="OpenCuria is responding"
              style={styles.streamingCursor}
            >
              <View
                style={[styles.cursorBar, { backgroundColor: theme.mutedText }]}
              />
              <Text style={{ color: theme.mutedText, fontSize: 13 }}>
                Thinking
              </Text>
            </View>
          ) : null}
          {apps.map((app) => (
            <PublishedAppBlock key={app.appId} app={app} />
          ))}
        </View>
      )}
      {isUser && prompt?.images.length ? (
        <PromptImages
          images={prompt.images}
          resolveMedia={resolveMedia}
          mediaVisible={mediaVisible}
          mediaScopeKey={mediaScopeKey}
          testID={`chat-message-images-${message.id}`}
        />
      ) : null}
      {editing ? (
        <Text
          style={[styles.editHint, { color: theme.mutedText }]}
          testID={`chat-edit-hint-${message.id}`}
        >
          Editing this message restarts the chat from here.
        </Text>
      ) : null}
      {!isUser && !streaming ? (
        <View style={styles.messageActions}>
          <IconButton
            icon={
              <Copy
                size={ACTION_ICON}
                strokeWidth={1.8}
                color={theme.mutedText}
              />
            }
            symbol="doc.on.doc"
            tone="quiet"
            size={ACTION_SIZE}
            label="Copy message"
            onPress={() => {
              void doCopy();
            }}
            disabled={!copyText}
            testID={`chat-copy-${message.id}`}
          />
          {!disabled && onFork ? (
            <IconButton
              icon={
                <GitBranch
                  size={ACTION_ICON}
                  strokeWidth={1.8}
                  color={theme.mutedText}
                />
              }
              symbol="arrow.triangle.branch"
              tone="quiet"
              size={ACTION_SIZE}
              label="Fork conversation from this answer"
              onPress={() => {
                void forkMessage(message.id);
              }}
              testID={`chat-fork-${message.id}`}
            />
          ) : null}
          <IconButton
            icon={
              <EllipsisVertical
                size={ACTION_ICON}
                strokeWidth={1.8}
                color={theme.mutedText}
              />
            }
            symbol="info.circle"
            tone="quiet"
            size={ACTION_SIZE}
            label="Message details"
            onPress={() => setActionsOpen(true)}
            testID={`chat-message-menu-${message.id}`}
          />
        </View>
      ) : null}
      <NativeModal
        visible={actionsOpen}
        onClose={() => setActionsOpen(false)}
        title={isUser ? "Message" : "OpenCuria"}
        testID={`chat-message-actions-${message.id}`}
      >
        <Button
          title="Copy message"
          symbol="doc.on.doc"
          variant="ghost"
          disabled={!copyText}
          onPress={() => {
            void doCopy();
            setActionsOpen(false);
          }}
          testID={isUser ? `chat-copy-${message.id}` : undefined}
        />
        {editPrompt ? (
          <Button
            symbol="pencil"
            title="Edit and run again"
            variant="ghost"
            onPress={() => {
              setActionsOpen(false);
              editPrompt();
            }}
            testID={`chat-edit-action-${message.id}`}
          />
        ) : null}
        {!isUser && !disabled && !streaming && onFork ? (
          <Button
            symbol="arrow.triangle.branch"
            title="Fork conversation from this answer"
            variant="ghost"
            onPress={() => {
              setActionsOpen(false);
              void forkMessage(message.id);
            }}
          />
        ) : null}
        {message.model ? (
          <Text selectable style={{ color: theme.mutedText, fontSize: 14 }}>
            {message.model}
          </Text>
        ) : null}
        {message.created_at ? (
          <Text style={{ color: theme.mutedText, fontSize: 13 }}>
            {formatTime(message.created_at)}
          </Text>
        ) : null}
        {formatMessageUsage(resolveMessageUsage(message)) ? (
          <Text style={{ color: theme.subtleText, fontSize: 13 }}>
            {formatMessageUsage(resolveMessageUsage(message))}
          </Text>
        ) : null}
      </NativeModal>
    </MessageFrame>
  );
});

/** A row's frame; a prompt just sent flies into it from the composer. */
function MessageFrame({
  message,
  children,
}: React.PropsWithChildren<{ message: HarnessMessage }>): React.JSX.Element {
  const testID = `chat-message-${message.id}`;
  if (message.role !== "user")
    return (
      <View
        testID={testID}
        style={[styles.messageFrame, styles.assistantFrame]}
      >
        {children}
      </View>
    );
  return (
    <PromptFlight
      testID={testID}
      fly={isOptimisticPrompt(message)}
      flightKey={messageRenderKey(message)}
      style={[styles.messageFrame, styles.userFrame]}
    >
      {children}
    </PromptFlight>
  );
}

/**
 * Text of a prompt. Mentions are chips in the flow of their paragraph, as in
 * the webapp; pictures it mentions show as `PromptImages` below the bubble.
 */
export function UserMessageContent({
  markdown,
  layout,
  resolveMedia,
  onOpenFile,
  mediaVisible = true,
  mediaScopeKey,
  selectable,
  testID = "chat-user-message-content",
}: {
  markdown: string;
  /** `promptLayout(markdown)`, when the host already has it. */
  layout?: PromptLayout;
  resolveMedia?: ChatMessagesProps["resolveMedia"];
  onOpenFile?: ChatMessagesProps["onOpenFile"];
  mediaVisible?: boolean;
  mediaScopeKey?: string | number;
  /** Native text selection (default); off inside a bubble with a menu. */
  selectable?: boolean;
  testID?: string;
}): React.JSX.Element {
  const blocks = useMemo(
    () => (layout ?? promptLayout(markdown)).blocks,
    [layout, markdown],
  );
  return (
    <View testID={testID} style={styles.userMessageContent}>
      {blocks.map((block, index) =>
        block.kind === "paragraph" ? (
          <PromptParagraph
            key={`paragraph:${index}`}
            runs={block.runs}
            onOpenFile={onOpenFile}
          />
        ) : (
          <ChatMarkdown
            key={`markdown:${index}`}
            markdown={block.markdown}
            variant="prompt"
            selectable={selectable}
            mediaVisible={mediaVisible}
            mediaScopeKey={mediaScopeKey}
            resolveMedia={resolveMedia}
            onOpenFile={onOpenFile}
          />
        ),
      )}
    </View>
  );
}

/**
 * A sent prompt's bubble. Holding it lifts it with a haptic tick and opens
 * its actions under the time it was sent, as in native messengers.
 */
function PromptBubble({
  messageId,
  text,
  sentAt,
  onCopy,
  onEdit,
  children,
}: React.PropsWithChildren<{
  messageId: string;
  /** The prompt as sent: copied, shared and offered for selection. */
  text: string | null;
  sentAt?: string | null;
  onCopy: () => void;
  /** Hidden when undefined. */
  onEdit?: () => void;
}>): React.JSX.Element {
  const { theme } = useTheme();
  const window = useWindowDimensions();
  // iOS hosts the bubble at its own size, so its width cap is in points:
  // first the window less the transcript padding, then its measured row.
  const [rowWidth, setRowWidth] = useState(() => window.width - 32);
  const [selecting, setSelecting] = useState(false);
  const bubble = useRef<View>(null);
  const bubbleWidth = useRef<number | undefined>(undefined);
  const selectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeSelection = useContext(PromptSelectionContext);
  const shown = useMemo(() => (text ? composerDisplay(text).text : ""), [text]);
  useEffect(
    () => () => {
      if (selectTimer.current) clearTimeout(selectTimer.current);
    },
    [],
  );
  useEffect(() => {
    if (!selecting) return;
    const end = () => setSelecting(false);
    const touchedAt: SelectionTouch = (x, y) => {
      bubble.current?.measure((_x, _y, width, height, left, top) => {
        const inside =
          x >= left - HANDLE_SLOP &&
          x <= left + width + HANDLE_SLOP &&
          y >= top - HANDLE_SLOP &&
          y <= top + height + HANDLE_SLOP;
        if (!inside) end();
      });
    };
    activeSelection.current = touchedAt;
    const keyboard = Keyboard.addListener("keyboardWillShow", end);
    return () => {
      keyboard.remove();
      if (activeSelection.current === touchedAt) activeSelection.current = null;
    };
  }, [activeSelection, selecting]);
  const select = (action: string) => {
    if (action === "copy") onCopy();
    else if (action === "edit") {
      // The composer only takes focus once the iOS menu is gone.
      if (Platform.OS === "ios")
        selectTimer.current = setTimeout(() => onEdit?.(), MENU_DISMISS_MS);
      else onEdit?.();
    } else if (action === "select") {
      // The text view only keeps its selection once the menu is gone.
      selectTimer.current = setTimeout(
        () => setSelecting(true),
        MENU_DISMISS_MS,
      );
    } else if (action === "share" && text)
      void Share.share({ message: text }).catch(() => undefined);
  };
  const bubbleStyle = [
    styles.userBubble,
    {
      backgroundColor: theme.userBubble,
      maxWidth: Math.floor(rowWidth * USER_BUBBLE_SHARE),
    },
  ];
  return (
    <View
      style={styles.promptRow}
      onLayout={(event) => setRowWidth(event.nativeEvent.layout.width)}
    >
      {selecting ? (
        // Out of the menu, so holding the text adjusts the selection.
        <View
          ref={bubble}
          style={[bubbleStyle, { width: bubbleWidth.current }]}
        >
          <SelectedPrompt
            text={shown}
            onDeselect={() => setSelecting(false)}
            testID={`chat-select-text-${messageId}`}
          />
        </View>
      ) : (
        <NativeContextMenu
          label={(sentAt && formatSendTime(sentAt)) || "Your message"}
          fitContent
          previewRadius={USER_BUBBLE_RADIUS}
          actions={[
            {
              id: "copy",
              title: "Copy",
              symbol: "doc.on.doc",
              disabled: !text,
            },
            ...(onEdit
              ? [{ id: "edit", title: "Edit", symbol: "pencil" }]
              : []),
            // Android cannot select a text view's text for the user.
            ...(Platform.OS === "ios"
              ? [
                  {
                    id: "select",
                    title: "Select text",
                    symbol: "selection.pin.in.out",
                    disabled: !text,
                  },
                ]
              : []),
            {
              id: "share",
              title: "Share",
              symbol: "square.and.arrow.up",
              disabled: !text,
            },
          ]}
          onSelect={select}
        >
          {(showMenu) => (
            // iOS presents the menu itself; Android opens it on this long press.
            <Pressable
              testID={`chat-message-menu-${messageId}`}
              accessibilityLabel={`Your message: ${text ?? ""}`}
              onLongPress={showMenu}
              onLayout={(event) => {
                bubbleWidth.current = event.nativeEvent.layout.width;
              }}
              style={bubbleStyle}
            >
              {children}
            </Pressable>
          )}
        </NativeContextMenu>
      )}
    </View>
  );
}

/**
 * A prompt's text selected in place: a read-only native text view that
 * selects everything at first, with the system's handles and edit menu
 * (Copy, Look Up, Translate, Share).
 */
function SelectedPrompt({
  text,
  onDeselect,
  testID,
}: {
  text: string;
  /** The reader cleared the selection, e.g. by tapping the text. */
  onDeselect: () => void;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  // Selects all once; afterwards the handles own the selection.
  const [initial, setInitial] = useState<
    { start: number; end: number } | undefined
  >({ start: 0, end: text.length });
  const ranged = useRef(false);
  return (
    <TextInput
      testID={testID}
      value={text}
      editable={false}
      multiline
      scrollEnabled={false}
      // Native, unlike focus(), which skips read-only fields.
      autoFocus
      selection={initial}
      onSelectionChange={({ nativeEvent: { selection } }) => {
        if (selection.end > selection.start) {
          ranged.current = true;
          setInitial(undefined);
        } else if (ranged.current) onDeselect();
      }}
      style={[styles.selectText, { color: theme.text }]}
    />
  );
}

/** A message the user sent while the agent worked, inside its turn. */
function SteerBubble({
  partId,
  markdown,
  resolveMedia,
  onOpenFile,
  mediaVisible,
  mediaScopeKey,
}: {
  partId: string;
  markdown: string;
  resolveMedia?: ChatMessagesProps["resolveMedia"];
  onOpenFile?: ChatMessagesProps["onOpenFile"];
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
}): React.JSX.Element {
  const { theme } = useTheme();
  const layout = useMemo(() => promptLayout(markdown), [markdown]);
  return (
    <View testID={`chat-steer-${partId}`} style={styles.steerRow}>
      <View
        style={[styles.userBubble, { backgroundColor: theme.surfaceMuted }]}
      >
        <UserMessageContent
          markdown={markdown}
          layout={layout}
          mediaScopeKey={mediaScopeKey}
          mediaVisible={mediaVisible}
          resolveMedia={resolveMedia}
          onOpenFile={onOpenFile}
          testID={`chat-steer-content-${partId}`}
        />
      </View>
      {layout.images.length ? (
        <PromptImages
          images={layout.images}
          resolveMedia={resolveMedia}
          mediaVisible={mediaVisible}
          mediaScopeKey={mediaScopeKey}
          testID={`chat-steer-images-${partId}`}
        />
      ) : null}
      <Text style={[styles.steerCaption, { color: theme.subtleText }]}>
        Sent while working
      </Text>
    </View>
  );
}

function MentionImage({
  path,
  name,
  resolveMedia,
  mediaVisible,
  mediaScopeKey,
}: {
  path: string;
  name: string;
  resolveMedia?: ChatMessagesProps["resolveMedia"];
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
}): React.JSX.Element {
  return (
    <ChatMarkdown
      markdown={imageMentionMarkdown(path, name)}
      resolveMedia={resolveMedia}
      mediaVisible={mediaVisible}
      mediaScopeKey={mediaScopeKey}
    />
  );
}

interface BlockProps extends Pick<
  ChatMessagesProps,
  | "resolveMedia"
  | "onOpenFile"
  | "onOpenSubtask"
  | "onRequestPartDetail"
  | "onOpenAttachment"
> {
  block: ActivityMessageBlock;
  elapsedLabel: string;
  streaming: boolean;
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
  todos?: HarnessTodo[];
}

function BlockView({
  block,
  elapsedLabel,
  streaming,
  todos,
  resolveMedia,
  onOpenFile,
  onOpenSubtask,
  onRequestPartDetail,
  onOpenAttachment,
  mediaVisible,
  mediaScopeKey,
}: BlockProps): React.JSX.Element | null {
  const { theme } = useTheme();
  if (block.kind === "workedFor")
    return (
      <WorkedForBlock
        blocks={block.blocks}
        elapsedLabel={elapsedLabel}
        streaming={streaming}
        todos={todos}
        resolveMedia={resolveMedia}
        onOpenFile={onOpenFile}
        onOpenSubtask={onOpenSubtask}
        onRequestPartDetail={onRequestPartDetail}
        onOpenAttachment={onOpenAttachment}
        mediaVisible={mediaVisible}
        mediaScopeKey={mediaScopeKey}
      />
    );
  if (block.kind === "steer")
    return (
      <SteerBubble
        partId={block.part.id}
        markdown={block.part.output}
        mediaScopeKey={mediaScopeKey}
        mediaVisible={mediaVisible}
        resolveMedia={resolveMedia}
        onOpenFile={onOpenFile}
      />
    );
  if (block.kind === "text")
    return (
      <View testID={`chat-part-${block.part.id}`} style={styles.textBlock}>
        <ChatMarkdown
          markdown={block.part.output ?? ""}
          streaming={streaming}
          mediaVisible={mediaVisible}
          mediaScopeKey={mediaScopeKey}
          resolveMedia={resolveMedia}
          onOpenFile={onOpenFile}
        />
      </View>
    );
  if (block.kind === "question") return <QuestionCard part={block.part} />;
  if (block.kind === "todos")
    return todos?.length ? <TodoCard todos={todos} /> : null;
  return (
    <ActivityGroup
      parts={block.parts}
      onOpenSubtask={onOpenSubtask}
      onRequestPartDetail={onRequestPartDetail}
      resolveMedia={resolveMedia}
      onOpenFile={onOpenFile}
      onOpenAttachment={onOpenAttachment}
      mediaVisible={mediaVisible}
      mediaScopeKey={mediaScopeKey}
    />
  );
}

/** Finished work uses the same quiet disclosure as exploration and commands. */
function WorkedForBlock({
  blocks,
  ...props
}: Omit<BlockProps, "block"> & {
  blocks: Extract<ActivityMessageBlock, { kind: "workedFor" }>["blocks"];
}): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(HARNESS_PAGE_SIZE);
  const label = props.elapsedLabel
    ? `Worked for ${props.elapsedLabel}`
    : "Worked";
  const hidden = blocks.length - shown;
  return (
    <View testID="chat-worked-for" style={styles.activity}>
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen(!open)}
        style={styles.activityHeader}
      >
        <Text style={[styles.activityTitle, { color: theme.mutedText }]}>
          {label}
        </Text>
      </NativeAction>
      {open ? (
        <View
          style={[
            styles.activitySteps,
            styles.workedForContent,
            { borderLeftColor: theme.border },
          ]}
        >
          {blocks.slice(0, shown).map((block, index) => (
            <BlockView key={blockKey(block, index)} block={block} {...props} />
          ))}
          {hidden > 0 ? (
            <NativeAction
              accessibilityRole="button"
              onPress={() => setShown(shown + HARNESS_PAGE_SIZE)}
              style={styles.stepRow}
            >
              <Text style={[styles.stepText, { color: theme.subtleText }]}>
                {`Show ${Math.min(hidden, HARNESS_PAGE_SIZE)} more`}
              </Text>
            </NativeAction>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

type PartDetailProps = Pick<
  ChatMessagesProps,
  "onRequestPartDetail" | "resolveMedia" | "onOpenFile" | "onOpenAttachment"
> & { mediaVisible?: boolean; mediaScopeKey?: string | number };

interface PartDetailState {
  current: HarnessPart;
  loading: boolean;
  error: boolean;
  load: () => Promise<void>;
  /** Fetch omitted heavy fields the first time the part is expanded. */
  ensure: () => void;
}

function usePartDetail(
  part: HarnessPart,
  onRequestPartDetail?: ChatMessagesProps["onRequestPartDetail"],
): PartDetailState {
  const [detail, setDetail] = useState<HarnessPart | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const load = async () => {
    if (!onRequestPartDetail || loading) return;
    setLoading(true);
    setError(false);
    try {
      const loaded = await onRequestPartDetail(part.session_id, part.id);
      if (loaded) setDetail(loaded);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  };
  return {
    current: detail ?? part,
    loading,
    error,
    load,
    ensure: () => {
      if (part.detail_loaded === false && !detail) void load();
    },
  };
}

/**
 * One quiet line such as "Explored 9 files, 2 searches" for a run of agent
 * work. It expands to the individual steps, one page at a time; a lone thought
 * or compaction expands straight to its text.
 */
function ActivityGroup({
  parts,
  onOpenSubtask,
  ...detailProps
}: {
  parts: HarnessPart[];
  onOpenSubtask?: ChatMessagesProps["onOpenSubtask"];
} & PartDetailProps): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(HARNESS_PAGE_SIZE);
  const summary = summarizeActivity(parts);
  const hidden = parts.length - shown;
  const first = parts[0]!;
  const lone =
    parts.length === 1 &&
    (first.type === "reasoning" || first.type === "compaction");
  const detail = usePartDetail(first, detailProps.onRequestPartDetail);
  return (
    <View testID={`chat-activity-${first.id}`} style={styles.activity}>
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={[summary.title, summary.detail]
          .filter(Boolean)
          .join(" ")}
        accessibilityState={{ expanded: open, busy: summary.running }}
        onPress={() => {
          setOpen(!open);
          if (!open && lone) detail.ensure();
        }}
        style={styles.activityHeader}
      >
        <Text style={[styles.activityTitle, { color: theme.mutedText }]}>
          {summary.title}
          {summary.detail ? (
            <Text style={{ color: theme.subtleText }}>
              {`\u2002${summary.detail}`}
            </Text>
          ) : null}
        </Text>
      </NativeAction>
      {open ? (
        <View style={[styles.activitySteps, { borderLeftColor: theme.border }]}>
          {lone ? (
            <PartDetail state={detail} {...detailProps} />
          ) : (
            parts
              .slice(0, shown)
              .map((part) =>
                part.type === "subtask" ? (
                  <SubtaskStep
                    key={part.id}
                    part={part}
                    onOpenSubtask={onOpenSubtask}
                  />
                ) : (
                  <ActivityStep key={part.id} part={part} {...detailProps} />
                ),
              )
          )}
          {!lone && hidden > 0 ? (
            <NativeAction
              accessibilityRole="button"
              onPress={() => setShown(shown + HARNESS_PAGE_SIZE)}
              style={styles.stepRow}
            >
              <Text style={[styles.stepText, { color: theme.subtleText }]}>
                {`Show ${Math.min(hidden, HARNESS_PAGE_SIZE)} more`}
              </Text>
            </NativeAction>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

function ActivityStep({
  part,
  ...detailProps
}: { part: HarnessPart } & PartDetailProps): React.JSX.Element {
  const { theme } = useTheme();
  const [open, setOpen] = useState(false);
  const detail = usePartDetail(part, detailProps.onRequestPartDetail);
  const { label, note } = stepLabel(detail.current);
  return (
    <View testID={`chat-work-row-${part.id}`}>
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={note ? `${label}, ${note}` : label}
        accessibilityState={{ expanded: open }}
        onPress={() => {
          setOpen(!open);
          if (!open) detail.ensure();
        }}
        style={styles.stepRow}
      >
        <Text
          numberOfLines={open ? undefined : 1}
          style={[styles.stepText, { color: theme.mutedText }]}
        >
          {label}
          {note ? (
            <Text style={{ color: theme.subtleText }}>{`\u2002${note}`}</Text>
          ) : null}
        </Text>
      </NativeAction>
      {open ? <PartDetail state={detail} {...detailProps} /> : null}
    </View>
  );
}

function SubtaskStep({
  part,
  onOpenSubtask,
}: {
  part: HarnessPart;
  onOpenSubtask?: ChatMessagesProps["onOpenSubtask"];
}): React.JSX.Element {
  const { theme } = useTheme();
  const child = resolveChildSessionId(part) ?? "";
  const agent = String(part.meta?.agent ?? part.display?.agent ?? "Agent");
  const status =
    part.state === "running"
      ? "Working"
      : part.state === "error"
        ? "Failed"
        : "Completed";
  return (
    <NativeAction
      accessibilityRole="button"
      accessibilityLabel={`${part.title || "Subtask"}, ${agent}, ${status}${child ? ", open subtask conversation" : ""}`}
      disabled={!child}
      onPress={() => onOpenSubtask?.(child)}
      testID={`chat-subtask-${part.id}`}
      style={styles.stepRow}
    >
      <View style={styles.subtaskStep}>
        <Text
          numberOfLines={1}
          style={[styles.stepText, { flex: 1, color: theme.mutedText }]}
        >
          {part.title || "Subtask"}
          <Text style={{ color: theme.subtleText }}>
            {`\u2002${agent} · ${status}`}
          </Text>
        </Text>
        {child ? <ChevronRight size={15} color={theme.subtleText} /> : null}
      </View>
    </NativeAction>
  );
}

function PartDetail({
  state,
  resolveMedia,
  onOpenFile,
  onOpenAttachment,
  mediaVisible = true,
  mediaScopeKey,
}: { state: PartDetailState } & PartDetailProps): React.JSX.Element {
  const { theme } = useTheme();
  const { current, loading, error, load } = state;
  if (loading)
    return (
      <Text style={[styles.detailNote, { color: theme.subtleText }]}>
        Loading details…
      </Text>
    );
  if (error)
    return (
      <Banner
        tone="neutral"
        message="Could not load this detail."
        actionLabel="Retry"
        onAction={() => {
          void load();
        }}
      />
    );
  if (current.type === "patch")
    return (
      <View style={[styles.diff, { backgroundColor: theme.codeBackground }]}>
        <Text selectable style={[styles.diffText, { color: theme.mutedText }]}>
          {current.output.trimEnd() ||
            (current.display?.preview ?? [])
              .map(
                (line) =>
                  `${line.type === "add" ? "+" : line.type === "del" ? "−" : " "}${line.content}`,
              )
              .join("\n") ||
            "No diff preview available."}
        </Text>
      </View>
    );
  const input = current.type === "tool" ? toolInput(current) : "";
  const output =
    current.output ||
    (current.type === "compaction"
      ? String(current.display?.summary ?? "")
      : "");
  const attachments = toolAttachments(current);
  if (!input && !output && !attachments.length)
    return (
      <Text style={[styles.detailNote, { color: theme.subtleText }]}>
        No additional details.
      </Text>
    );
  return (
    <View style={styles.partDetail}>
      {input ? <CodePanel title="Input" value={input} /> : null}
      {output ? (
        <ChatMarkdown
          markdown={output}
          variant="quiet"
          mediaVisible={mediaVisible}
          mediaScopeKey={mediaScopeKey}
          resolveMedia={resolveMedia}
          onOpenFile={onOpenFile}
        />
      ) : null}
      {attachments.map((attachment, index) => (
        <ToolAttachmentCard
          key={`${attachment.filename}-${index}`}
          attachment={attachment}
          onOpenAttachment={onOpenAttachment}
          onOpenFile={onOpenFile}
          resolveMedia={resolveMedia}
          mediaVisible={mediaVisible}
          mediaScopeKey={mediaScopeKey}
        />
      ))}
    </View>
  );
}

function ToolAttachmentCard({
  attachment,
  onOpenAttachment,
  onOpenFile,
  resolveMedia,
  mediaVisible = true,
  mediaScopeKey,
}: {
  attachment: ReturnType<typeof toolAttachments>[number];
} & Omit<PartDetailProps, "onRequestPartDetail">): React.JSX.Element {
  const { theme } = useTheme();
  const { path, url, mime } = attachment;
  const filename =
    attachment.filename ||
    (mime.toLowerCase() === "application/pdf"
      ? "PDF attachment"
      : "Tool attachment");
  const isImage = mime.toLowerCase().startsWith("image/");
  // Current parts reference a workspace file; legacy parts inline a data URL.
  if (path && isImage)
    return (
      <MentionImage
        path={path}
        name={filename}
        resolveMedia={resolveMedia}
        mediaVisible={mediaVisible}
        mediaScopeKey={mediaScopeKey}
      />
    );
  const open = () => {
    if (path) onOpenFile?.(path);
    else if (url) onOpenAttachment?.({ uri: url, filename, mime });
  };
  if (isImage && url) {
    return (
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`Open image preview ${filename}`}
        onPress={open}
        style={styles.toolAttachmentImage}
      >
        <Image
          source={{ uri: url }}
          contentFit="contain"
          style={styles.attachmentImage}
          accessibilityLabel={filename}
        />
        <Text
          numberOfLines={1}
          style={{ color: theme.subtleText, fontSize: 12 }}
        >
          {filename}
        </Text>
      </NativeAction>
    );
  }
  return (
    <NativeAction
      accessibilityRole="button"
      accessibilityLabel={`Open attachment ${filename}`}
      onPress={open}
      style={[styles.pdfCard, { backgroundColor: theme.surfaceMuted }]}
    >
      <FileCode2 size={18} color={theme.mutedText} />
      <View style={{ flex: 1 }}>
        <Text numberOfLines={1} style={{ color: theme.text, fontSize: 14 }}>
          {filename}
        </Text>
        <Text style={{ color: theme.mutedText, fontSize: 12 }}>{mime}</Text>
      </View>
      <ChevronRight size={17} color={theme.mutedText} />
    </NativeAction>
  );
}

/** An app the agent sent to the phone; the host renders its install card. */
function PublishedAppBlock({
  app,
}: {
  app: PublishedAppReference;
}): React.JSX.Element {
  const { theme } = useTheme();
  const render = React.useContext(PublishedAppContext);
  return (
    <View testID={`chat-app-${app.appId}`} style={styles.appBlock}>
      {render ? (
        render(app)
      ) : (
        <Text style={{ color: theme.mutedText, fontSize: 14 }}>
          {`Sent ${app.name} to your phone`}
        </Text>
      )}
    </View>
  );
}

function QuestionCard({ part }: { part: HarnessPart }): React.JSX.Element {
  const { theme } = useTheme();
  const rows = Array.isArray(part.display?.question_rows)
    ? part.display.question_rows
    : [];
  return (
    <View
      testID={`chat-question-${part.id}`}
      style={[
        styles.questionCard,
        { borderColor: theme.border, backgroundColor: theme.surfaceMuted },
      ]}
    >
      <View style={styles.questionHeader}>
        <MessageSquareText size={16} color={theme.mutedText} />
        <Text style={[styles.detailTitle, { color: theme.text }]}>
          Question
        </Text>
      </View>
      {rows.length ? (
        rows.map((row, index) => (
          <View key={index} style={{ gap: 4 }}>
            <Text style={{ color: theme.text, fontSize: 14, lineHeight: 21 }}>
              {row.question}
            </Text>
            {row.answer ? (
              <Text
                style={{ color: theme.mutedText, fontSize: 14, lineHeight: 20 }}
              >
                {row.answer}
              </Text>
            ) : null}
          </View>
        ))
      ) : (
        <ChatMarkdown
          markdown={part.output || part.title || "Question answered"}
        />
      )}
    </View>
  );
}

function CodePanel({
  title,
  value,
}: {
  title: string;
  value: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={[styles.codePanel, { backgroundColor: theme.codeBackground }]}>
      <Text style={[styles.codeTitle, { color: theme.subtleText }]}>
        {title}
      </Text>
      <Text selectable style={[styles.codeText, { color: theme.text }]}>
        {value}
      </Text>
    </View>
  );
}

/** The session's to-do list, shown where the agent last updated it. */
function TodoCard({ todos }: { todos: HarnessTodo[] }): React.JSX.Element {
  const { theme } = useTheme();
  const sorted = useMemo(
    () => [...todos].sort((left, right) => left.order - right.order),
    [todos],
  );
  const done = todos.filter((todo) => todo.status === "completed").length;
  const settled = todos.every(
    (todo) => todo.status === "completed" || todo.status === "cancelled",
  );
  // A finished list folds to its header until the reader opens it.
  const [override, setOverride] = useState<boolean | null>(null);
  const open = override ?? !settled;
  return (
    <View
      testID="chat-todos"
      style={[styles.todoCard, { borderColor: theme.border }]}
    >
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`To-dos, ${done} of ${todos.length} done`}
        accessibilityState={{ expanded: open }}
        onPress={() => setOverride(!open)}
        style={styles.todoHeader}
      >
        <Text style={[styles.todoText, { color: theme.text }]}>
          To-dos
          <Text style={{ color: theme.subtleText }}>
            {`\u2002${done}/${todos.length}`}
          </Text>
        </Text>
      </NativeAction>
      {open ? (
        <>
          <View
            style={[styles.todoDivider, { backgroundColor: theme.border }]}
          />
          {sorted.map((todo, index) => {
            const finished =
              todo.status === "completed" || todo.status === "cancelled";
            return (
              <View
                key={todo.id}
                accessible
                accessibilityLabel={`${todo.content}, ${todo.status.replace("_", " ")}`}
                testID={`chat-todo-${todo.id}`}
                style={styles.todoRow}
              >
                <View style={styles.todoIcon}>
                  <TodoStatusIcon status={todo.status} />
                </View>
                <View
                  style={[
                    styles.todoTextBox,
                    index > 0 && {
                      borderTopWidth: StyleSheet.hairlineWidth,
                      borderTopColor: theme.border,
                    },
                  ]}
                >
                  <Text
                    style={[
                      styles.todoText,
                      {
                        color:
                          todo.status === "in_progress"
                            ? theme.text
                            : finished
                              ? theme.subtleText
                              : theme.mutedText,
                        textDecorationLine: finished ? "line-through" : "none",
                      },
                    ]}
                  >
                    {todo.content}
                  </Text>
                </View>
              </View>
            );
          })}
        </>
      ) : null}
    </View>
  );
}

function TodoStatusIcon({
  status,
}: {
  status: HarnessTodo["status"];
}): React.JSX.Element {
  const { theme } = useTheme();
  if (status === "pending")
    return (
      <CircleDashed size={20} strokeWidth={1.6} color={theme.subtleText} />
    );
  if (status === "cancelled")
    return <CircleX size={20} strokeWidth={1.6} color={theme.subtleText} />;
  const active = status === "in_progress";
  return (
    <View
      style={[
        styles.todoBadge,
        { backgroundColor: active ? theme.text : theme.subtleText },
      ]}
    >
      {active ? (
        <ArrowRight size={13} strokeWidth={2.6} color={theme.background} />
      ) : (
        <Check size={12} strokeWidth={3} color={theme.background} />
      )}
    </View>
  );
}

function formatData(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
/** Tool arguments for the detail panel, without the redundant tool id. */
function toolInput(part: HarnessPart): string {
  const args = parseToolArguments(part);
  const rest = Object.fromEntries(
    Object.entries(part.input ?? {}).filter(([key]) => key !== "tool"),
  );
  const shown = Object.keys(args).length ? args : rest;
  return Object.keys(shown).length ? formatData(shown) : "";
}
/** One-line label and quiet note for a step inside an activity run. */
function stepLabel(part: HarnessPart): { label: string; note: string } {
  const failed = part.state === "error" ? "failed" : "";
  if (part.type === "patch") {
    const path = String(
      part.display?.path || part.meta?.path || part.title || "",
    );
    const additions = Number(part.display?.additions ?? 0);
    const deletions = Number(part.display?.deletions ?? 0);
    return {
      label: path ? `Edited ${basename(path)}` : "Edited file",
      note: [
        additions ? `+${additions}` : "",
        deletions ? `−${deletions}` : "",
        failed,
      ]
        .filter(Boolean)
        .join(" "),
    };
  }
  if (part.type === "reasoning")
    return { label: "Thought", note: String(part.display?.summary ?? "") };
  if (part.type === "compaction")
    return { label: part.title || "Context compacted", note: "" };
  if (part.type === "tool") {
    // Detail-less timeline parts may name their tool only in the projection.
    const named =
      part.tool || !part.display?.tool
        ? part
        : { ...part, tool: String(part.display.tool) };
    return { label: toolDisplayLabel(named), note: failed };
  }
  return { label: part.title || part.type, note: failed };
}
function basename(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() || path;
}
function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
function blockKey(block: ActivityMessageBlock, index: number): string {
  if (block.kind === "workedFor")
    return `workedFor:${blockKey(block.blocks[0]!, index)}`;
  if (block.kind === "activity")
    return `activity:${block.parts[0]?.id ?? index}`;
  return `${block.kind}:${block.part.id}`;
}

const styles = StyleSheet.create({
  listRoot: { flex: 1, minHeight: 0 },
  appBlock: { marginVertical: 6 },
  empty: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 32,
  },
  emptyTitle: {
    fontSize: 22,
    lineHeight: 28,
    fontWeight: "400",
    textAlign: "center",
  },
  listContent: {
    paddingHorizontal: 16,
    paddingTop: 10,
    paddingBottom: 4,
  },
  jumpButton: { position: "absolute", alignSelf: "center", bottom: 0 },
  messageFrame: { maxWidth: 820, width: "100%", gap: 4, paddingBottom: 24 },
  userFrame: { alignSelf: "flex-end", alignItems: "flex-end" },
  assistantFrame: { alignSelf: "flex-start", alignItems: "flex-start" },
  userBubble: {
    maxWidth: `${USER_BUBBLE_SHARE * 100}%`,
    minWidth: 48,
    borderRadius: USER_BUBBLE_RADIUS,
    paddingVertical: 13,
    paddingHorizontal: 16,
  },
  promptRow: { alignSelf: "stretch", alignItems: "flex-end" },
  selectText: { fontSize: 17, lineHeight: 26, padding: 0 },
  userMessageContent: { gap: 10 },
  assistantBody: { width: "100%", gap: 6 },
  steerRow: { alignSelf: "flex-end", alignItems: "flex-end", maxWidth: "94%" },
  steerCaption: { fontSize: 11, lineHeight: 16, marginTop: 3 },
  textBlock: { width: "100%" },
  // Glyphs, not their touch targets, line up with the text above.
  messageActions: {
    flexDirection: "row",
    alignItems: "center",
    marginLeft: -(ACTION_SIZE - ACTION_ICON) / 2,
  },
  editHint: {
    maxWidth: "92%",
    marginTop: 6,
    fontSize: 14,
    lineHeight: 20,
    textAlign: "right",
  },
  streamingCursor: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
    height: 24,
  },
  cursorBar: { width: 2, height: 16, borderRadius: 2 },
  activity: { width: "100%" },
  activityHeader: { paddingVertical: 5 },
  activityTitle: { fontSize: 17, lineHeight: 22 },
  workedForContent: { gap: 6 },
  activitySteps: {
    borderLeftWidth: StyleSheet.hairlineWidth,
    marginLeft: 2,
    marginBottom: 4,
    paddingLeft: 12,
  },
  stepRow: { paddingVertical: 5 },
  stepText: { fontSize: 15, lineHeight: 20 },
  subtaskStep: { flexDirection: "row", alignItems: "center", gap: 6 },
  partDetail: { gap: 8, paddingBottom: 8 },
  detailNote: { fontSize: 13, lineHeight: 18, paddingBottom: 8 },
  detailTitle: { fontSize: 14, lineHeight: 20, fontWeight: "600" },
  codePanel: { padding: 12, borderRadius: 12, gap: 6 },
  codeTitle: {
    fontSize: 11,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
  codeText: { fontSize: 12, lineHeight: 18, fontFamily: "monospace" },
  diff: { maxHeight: 320, padding: 12, borderRadius: 12, marginBottom: 8 },
  diffText: { fontFamily: "monospace", fontSize: 12, lineHeight: 18 },
  attachmentImage: { width: "100%", height: 220, borderRadius: 12 },
  toolAttachmentImage: {
    width: "100%",
    minHeight: 44,
    alignItems: "flex-start",
    gap: 5,
  },
  pdfCard: {
    minHeight: 52,
    borderRadius: 13,
    paddingHorizontal: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  questionCard: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 15,
    padding: 13,
    gap: 9,
  },
  questionHeader: { flexDirection: "row", alignItems: "center", gap: 8 },
  todoCard: {
    width: "100%",
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 18,
    overflow: "hidden",
  },
  todoHeader: { paddingHorizontal: 12, paddingVertical: 10 },
  todoDivider: { height: StyleSheet.hairlineWidth },
  todoRow: { flexDirection: "row", alignItems: "flex-start", paddingLeft: 12 },
  todoIcon: {
    width: 20,
    height: 21,
    marginTop: 10,
    marginRight: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  todoBadge: {
    width: 20,
    height: 20,
    borderRadius: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  todoTextBox: { flex: 1, marginRight: 12, paddingVertical: 10 },
  todoText: { fontSize: 17, lineHeight: 21 },
});
