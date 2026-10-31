import React, {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Animated,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
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
  Sparkles,
} from "lucide-react-native";
import type {
  HarnessMessage,
  HarnessPart,
  HarnessSession,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import {
  buildActivityBlocks,
  findTodoAnchor,
  summarizeActivity,
  type ActivityTimelineBlock,
} from "@opencuria/chat-core/harnessActivity";
import { getMessageCopyText } from "@opencuria/chat-core/harnessMessageCopy";
import { HARNESS_PAGE_SIZE } from "@opencuria/chat-core/harnessPagination";
import { sortHarnessMessages } from "@opencuria/chat-core/harnessReducer";
import { toolAttachments } from "@opencuria/chat-core/harnessAttachments";
import { resolveChildSessionId } from "@opencuria/chat-core/harnessSubtaskActivity";
import { parseComposerSegments } from "@opencuria/chat-core/composerTokens";
import {
  formatMessageUsage,
  resolveMessageUsage,
} from "@opencuria/chat-core/harnessUsage";
import {
  parseToolArguments,
  toolDisplayLabel,
} from "@opencuria/chat-core/toolDisplay";
import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";
import { useChromeTopInset } from "../ui/chromeInset";
import { useChatLayout } from "./ChatLayout";
import {
  EndPullArea,
  EndPullHint,
  useEndPull,
  useEndPullState,
  type EndPullState,
} from "./EndPull";
import { NativeMenu } from "../ui/NativeMenu";
import { useTheme } from "../ui/theme";
import {
  Banner,
  Button,
  IconButton,
  NativeModal,
  NativeAction,
} from "../ui/primitives";
import { ChatMarkdown, type ResolvedChatMedia } from "./ChatMarkdown";

export interface ChatMessagesProps {
  messages: HarnessMessage[];
  sessions?: HarnessSession[];
  todos?: HarnessTodo[];
  busy?: boolean;
  streamingMessageId?: string | null;
  disabled?: boolean;
  resolveMedia?: (path: string) => Promise<ResolvedChatMedia>;
  onEdit?: (messageId: string, text: string) => void | Promise<void>;
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
  /** Hint shown while pulling, such as "Git, Terminal & Files". */
  pullUpLabel?: string;
  testID?: string;
}

const VIEWABILITY = { itemVisiblePercentThreshold: 1 };
/** Message actions keep native touch targets; their glyphs align with the text. */
const ACTION_SIZE = Platform.OS === "ios" ? 44 : 36;
const ACTION_ICON = 17;
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
  sessions = [],
  todos = [],
  busy = false,
  streamingMessageId,
  disabled = false,
  resolveMedia,
  onEdit,
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
  testID = "chat-messages",
}: ChatMessagesProps): React.JSX.Element {
  const { theme } = useTheme();
  const layout = useChatLayout();
  const pullState = useEndPullState();
  const pull = useEndPull(pullState, onPullUpAtEnd);
  // Messages scroll beneath translucent navigation chrome and host overlays.
  const topInset = useChromeTopInset() + contentInsetTop;
  const listRef = useRef<LegendListRef>(null);
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
          .map((item) => item.item.id),
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
  const ordered = useMemo(() => sortHarnessMessages(messages), [messages]);
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
        mediaVisible={visibleMessageIds.current.has(item.id)}
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
  const keyExtractor = useCallback((message: HarnessMessage) => message.id, []);
  const activeRun = sessions.find((session) => session.status === "busy");
  const runLabel = activeRun
    ? `OpenCuria is working${activeRun.agent_name ? ` · ${activeRun.agent_name}` : ""}`
    : null;
  const header = useMemo(
    () =>
      runLabel ? (
        <View
          style={[styles.runHeader, { backgroundColor: theme.surfaceMuted }]}
        >
          <View style={[styles.runDot, { backgroundColor: theme.mutedText }]} />
          <Text style={[styles.runText, { color: theme.mutedText }]}>
            {runLabel}
          </Text>
          <LoaderCircle size={16} color={theme.subtleText} />
        </View>
      ) : null,
    [runLabel, theme],
  );
  // "Scroll to latest" follows the list's own end tracking, which includes the
  // keyboard and composer insets and also reacts to content growing below.
  const hasMessages = ordered.length > 0;
  useEffect(() => {
    const state = hasMessages ? listRef.current?.getState() : undefined;
    if (!state) {
      pullState.atEnd.value = true;
      return;
    }
    setShowJumpToLatest(!state.isNearEnd);
    pullState.atEnd.value = state.isAtEnd;
    const stopNearEnd = state.listen("isNearEnd", (nearEnd) =>
      setShowJumpToLatest(!nearEnd),
    );
    const stopAtEnd = state.listen("isAtEnd", (atEnd) => {
      pullState.atEnd.value = atEnd;
    });
    return () => {
      stopNearEnd();
      stopAtEnd();
    };
  }, [hasMessages, pullState]);
  const onScroll = useCallback(() => {
    pullState.lastScrollAt.value = Date.now();
  }, [pullState]);
  const pullHint = onPullUpAtEnd ? (
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

  if (ordered.length === 0) {
    return (
      <View style={styles.listRoot}>
        <EndPullArea gesture={pull.gesture}>
          <View style={styles.listRoot} collapsable={false}>
            <EmptyTranscript
              topInset={topInset}
              testID={testID}
              pullState={pullState}
            />
          </View>
        </EndPullArea>
        {pullHint}
      </View>
    );
  }

  return (
    <View style={styles.listRoot}>
      <EndPullArea gesture={pull.gesture}>
        <View style={styles.listRoot} collapsable={false}>
          <KeyboardAwareLegendList
            ref={listRef}
            testID={testID}
            data={ordered}
            renderItem={renderItem}
            keyExtractor={keyExtractor}
            ListHeaderComponent={header}
            estimatedItemSize={220}
            drawDistance={600}
            onViewableItemsChanged={onViewableItemsChanged}
            viewabilityConfig={VIEWABILITY}
            initialScrollAtEnd
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
            onScroll={onScroll}
            scrollEventThrottle={32}
          />
        </View>
      </EndPullArea>
      <JumpToLatest visible={showJumpToLatest} onPress={jumpToLatest} />
      {pullHint}
    </View>
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
  topInset,
  testID,
  pullState,
}: {
  topInset: number;
  testID: string;
  pullState: EndPullState;
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
      scrollEventThrottle={32}
      onScroll={() => {
        pullState.lastScrollAt.value = Date.now();
      }}
    >
      <Animated.View style={centered}>
        <Text style={[styles.emptyTitle, { color: theme.text }]}>
          What can I help with?
        </Text>
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
  "messages" | "sessions" | "todos" | "busy" | "streamingMessageId" | "testID"
> {
  message: HarnessMessage;
  streaming: boolean;
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
  /** Session to-dos, passed only to the message that hosts the to-do card. */
  todos?: HarnessTodo[];
  /** The hosting `todowrite` part; null appends the card after the message. */
  todoPartId?: string | null;
}

const MessageRow = memo(function MessageRow({
  message,
  streaming,
  disabled,
  resolveMedia,
  todos,
  todoPartId,
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
  const [editing, setEditing] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [editText, setEditText] = useState(message.content);
  const isUser = message.role === "user";
  const showTodos = Boolean(todos?.length);
  const blocks = useMemo(
    () =>
      buildActivityBlocks(
        [...message.parts].sort(
          (left, right) => (left.position ?? 0) - (right.position ?? 0),
        ),
        { todoPartId: showTodos ? todoPartId : null },
      ),
    [message.parts, showTodos, todoPartId],
  );
  const copyText = getMessageCopyText(message, streaming);
  const editUserMessage = useCallback(
    async (messageId: string, text: string) => {
      try {
        await onEdit?.(messageId, text);
      } catch {
        /* The host owns surfacing edit/rerun failures. */
      }
    },
    [onEdit],
  );
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

  const userBubble = isUser ? (
    <View
      style={
        editing
          ? [styles.userBubble, { backgroundColor: theme.userBubble }]
          : undefined
      }
    >
      {editing ? (
        <>
          <TextInput
            multiline
            value={editText}
            onChangeText={setEditText}
            accessibilityLabel="Edit message"
            testID={`chat-edit-${message.id}`}
            style={[
              styles.editInput,
              { color: theme.text, borderColor: theme.border },
            ]}
          />
          <View style={styles.inlineActions}>
            <Button
              title="Cancel"
              variant="ghost"
              compact
              onPress={() => {
                setEditing(false);
                setEditText(message.content);
              }}
            />
            <Button
              title="Save & run"
              variant="primary"
              compact
              onPress={() => {
                setEditing(false);
                void editUserMessage(message.id, editText.trim());
              }}
              disabled={!editText.trim() || editText.trim() === message.content}
            />
          </View>
        </>
      ) : (
        <UserMessageContent
          markdown={message.content}
          mediaScopeKey={mediaScopeKey}
          mediaVisible={mediaVisible}
          resolveMedia={resolveMedia}
          onOpenFile={onOpenFile}
          testID={`chat-message-markdown-${message.id}`}
        />
      )}
    </View>
  ) : null;

  return (
    <View
      testID={`chat-message-${message.id}`}
      style={[
        styles.messageFrame,
        isUser ? styles.userFrame : styles.assistantFrame,
      ]}
    >
      {isUser ? (
        editing ? (
          userBubble
        ) : (
          <NativeMenu
            style={[styles.userBubble, { backgroundColor: theme.userBubble }]}
            label={`Your message: ${message.content}`}
            longPress
            testID={`chat-message-menu-${message.id}`}
            actions={[
              {
                id: "copy",
                title: "Copy message",
                symbol: "doc.on.doc",
                disabled: !copyText,
              },
              ...(!editing &&
              !disabled &&
              !message.id.startsWith("local-user-") &&
              onEdit
                ? [
                    {
                      id: "edit",
                      title: "Edit and run again",
                      symbol: "pencil",
                    },
                  ]
                : []),
            ]}
            onSelect={(action) => {
              if (action === "copy") void doCopy();
              else {
                setEditText(message.content);
                setEditing(true);
              }
            }}
          >
            {userBubble}
          </NativeMenu>
        )
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
        </View>
      )}
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
        {isUser &&
        !editing &&
        !disabled &&
        !message.id.startsWith("local-user-") &&
        onEdit ? (
          <Button
            symbol="pencil"
            title="Edit and run again"
            variant="ghost"
            onPress={() => {
              setActionsOpen(false);
              setEditText(message.content);
              setEditing(true);
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
    </View>
  );
});

export function UserMessageContent({
  markdown,
  resolveMedia,
  onOpenFile,
  mediaVisible = true,
  mediaScopeKey,
  testID = "chat-user-message-content",
}: {
  markdown: string;
  resolveMedia?: ChatMessagesProps["resolveMedia"];
  onOpenFile?: ChatMessagesProps["onOpenFile"];
  mediaVisible?: boolean;
  mediaScopeKey?: string | number;
  testID?: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  const segments = parseComposerSegments(markdown);
  return (
    <View testID={testID} style={styles.userMessageContent}>
      {segments.map((segment, index) =>
        segment.kind === "text" ? (
          segment.text ? (
            <ChatMarkdown
              key={`text:${index}`}
              markdown={segment.text}
              variant="prompt"
              mediaVisible={mediaVisible}
              mediaScopeKey={mediaScopeKey}
              resolveMedia={resolveMedia}
              onOpenFile={onOpenFile}
            />
          ) : null
        ) : segment.kind === "agent" ? (
          <View
            key={`agent:${segment.start}`}
            style={[styles.mentionChip, { backgroundColor: theme.surface }]}
          >
            <Sparkles size={13} color={theme.mutedText} />
            <Text style={{ color: theme.text, fontSize: 13 }}>
              {segment.name}
            </Text>
          </View>
        ) : classifyWorkspaceFile(segment.path) === "image" ? (
          <MentionImage
            key={`image:${segment.start}`}
            path={segment.path}
            name={segment.name}
            mediaScopeKey={mediaScopeKey}
            mediaVisible={mediaVisible}
            resolveMedia={resolveMedia}
          />
        ) : (
          <NativeAction
            key={`file:${segment.start}`}
            accessibilityRole="button"
            accessibilityLabel={`Open file ${segment.path}`}
            onPress={() => onOpenFile?.(segment.path)}
            style={[styles.mentionChip, { backgroundColor: theme.surface }]}
          >
            <FileCode2 size={13} color={theme.mutedText} />
            <Text numberOfLines={1} style={{ color: theme.text, fontSize: 13 }}>
              {segment.name}
            </Text>
          </NativeAction>
        ),
      )}
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
      markdown={`![${name.replace(/[\[\]]/g, "")}](<${path}>)`}
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
  block: ActivityTimelineBlock;
  streaming: boolean;
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
  todos?: HarnessTodo[];
}

function BlockView({
  block,
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
  if (block.kind === "steer")
    return (
      <View testID={`chat-steer-${block.part.id}`} style={styles.steerRow}>
        <View
          style={[styles.userBubble, { backgroundColor: theme.surfaceMuted }]}
        >
          <UserMessageContent
            markdown={block.part.output}
            mediaScopeKey={mediaScopeKey}
            mediaVisible={mediaVisible}
            resolveMedia={resolveMedia}
            onOpenFile={onOpenFile}
            testID={`chat-steer-content-${block.part.id}`}
          />
        </View>
        <Text style={[styles.steerCaption, { color: theme.subtleText }]}>
          Sent while working
        </Text>
      </View>
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
        />
      ))}
    </View>
  );
}

function ToolAttachmentCard({
  attachment,
  onOpenAttachment,
}: {
  attachment: ReturnType<typeof toolAttachments>[number];
  onOpenAttachment?: ChatMessagesProps["onOpenAttachment"];
}): React.JSX.Element {
  const { theme } = useTheme();
  const filename =
    attachment.filename ||
    (attachment.mime.toLowerCase() === "application/pdf"
      ? "PDF attachment"
      : "Tool attachment");
  if (attachment.mime.toLowerCase().startsWith("image/")) {
    return (
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`Open image preview ${filename}`}
        onPress={() =>
          onOpenAttachment?.({
            uri: attachment.url,
            filename,
            mime: attachment.mime,
          })
        }
        style={styles.toolAttachmentImage}
      >
        <Image
          source={{ uri: attachment.url }}
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
      onPress={() =>
        onOpenAttachment?.({
          uri: attachment.url,
          filename,
          mime: attachment.mime,
        })
      }
      style={[styles.pdfCard, { backgroundColor: theme.surfaceMuted }]}
    >
      <FileCode2 size={18} color={theme.mutedText} />
      <View style={{ flex: 1 }}>
        <Text numberOfLines={1} style={{ color: theme.text, fontSize: 14 }}>
          {filename}
        </Text>
        <Text style={{ color: theme.mutedText, fontSize: 12 }}>
          {attachment.mime}
        </Text>
      </View>
      <ChevronRight size={17} color={theme.mutedText} />
    </NativeAction>
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
function blockKey(block: ActivityTimelineBlock, index: number): string {
  if (block.kind === "activity")
    return `activity:${block.parts[0]?.id ?? index}`;
  return `${block.kind}:${block.part.id}`;
}

const styles = StyleSheet.create({
  listRoot: { flex: 1, minHeight: 0 },
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
  runHeader: {
    alignSelf: "center",
    minHeight: 38,
    borderRadius: 20,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 14,
  },
  runDot: { width: 7, height: 7, borderRadius: 4 },
  runText: { fontSize: 13, lineHeight: 18 },
  messageFrame: { maxWidth: 820, width: "100%", gap: 4, paddingBottom: 24 },
  userFrame: { alignSelf: "flex-end", alignItems: "flex-end" },
  assistantFrame: { alignSelf: "flex-start", alignItems: "flex-start" },
  userBubble: {
    maxWidth: "82%",
    minWidth: 48,
    borderRadius: 24,
    paddingVertical: 13,
    paddingHorizontal: 16,
  },
  userMessageContent: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    gap: 4,
  },
  mentionChip: {
    minHeight: 30,
    maxWidth: 240,
    borderRadius: 15,
    paddingHorizontal: 9,
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
  },
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
  inlineActions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: 8,
    marginTop: 8,
  },
  editInput: {
    minHeight: 88,
    maxHeight: 240,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 12,
    padding: 10,
    fontSize: 16,
    lineHeight: 23,
    textAlignVertical: "top",
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
