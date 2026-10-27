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
  Copy,
  ChevronDown,
  ChevronRight,
  Clock3,
  Code2,
  FileCode2,
  EllipsisVertical,
  GitBranch,
  Hammer,
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
  buildMessageBlocks,
  type MessageRenderBlock,
  type RenderBlock,
} from "@opencuria/chat-core/harnessBlocks";
import { getMessageCopyText } from "@opencuria/chat-core/harnessMessageCopy";
import { sortHarnessMessages } from "@opencuria/chat-core/harnessReducer";
import { toolAttachments } from "@opencuria/chat-core/harnessAttachments";
import { resolveChildSessionId } from "@opencuria/chat-core/harnessSubtaskActivity";
import { parseComposerSegments } from "@opencuria/chat-core/composerTokens";
import {
  formatMessageUsage,
  resolveMessageUsage,
} from "@opencuria/chat-core/harnessUsage";
import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";
import { useChromeTopInset } from "../ui/chromeInset";
import { useChatLayout } from "./ChatLayout";
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
  testID?: string;
}

const VIEWABILITY = { itemVisiblePercentThreshold: 1 };
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
  testID = "chat-messages",
}: ChatMessagesProps): React.JSX.Element {
  const { theme } = useTheme();
  const layout = useChatLayout();
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
  const [todosOpen, setTodosOpen] = useState(false);
  // Live replies have no server position yet; the shared rule keeps them last.
  const ordered = useMemo(() => sortHarnessMessages(messages), [messages]);
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
  // Snapshots clone every session on each streamed token; rows only need to
  // re-render when the session tree they resolve subtasks against changes.
  const sessionTreeKey = sessions
    .map(
      (session) =>
        `${session.id}\u0001${session.parent_id ?? ""}\u0001${session.title}`,
    )
    .join("\u0002");
  const rowSessions = useMemo(() => sessions, [sessionTreeKey]);
  const renderItem = useCallback(
    ({ item }: { item: HarnessMessage }) => (
      <MessageRow
        message={item}
        sessions={rowSessions}
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
      resolveMedia,
      rowSessions,
      streamingMessageId,
      visibleMessageIdsState,
      mediaScopeKey,
    ],
  );
  const keyExtractor = useCallback((message: HarnessMessage) => message.id, []);
  const activeRun = sessions.find((session) => session.status === "busy");
  const runLabel = activeRun
    ? `OpenCuria is working${activeRun.agent_name ? ` · ${activeRun.agent_name}` : ""}`
    : null;
  const todosKey = JSON.stringify(todos);
  const header = useMemo(
    () => (
      <>
        {runLabel ? (
          <View
            style={[styles.runHeader, { backgroundColor: theme.surfaceMuted }]}
          >
            <View style={[styles.runDot, { backgroundColor: theme.primary }]} />
            <Text style={[styles.runText, { color: theme.mutedText }]}>
              {runLabel}
            </Text>
            <LoaderCircle size={16} color={theme.primary} />
          </View>
        ) : null}
        {todos.length ? (
          <TodoStrip todos={todos} onPress={() => setTodosOpen(true)} />
        ) : null}
      </>
    ),
    // `todos` is a fresh clone per snapshot; its content decides the header.
    [runLabel, theme, todosKey],
  );
  // "Scroll to latest" follows the list's own end tracking, which includes the
  // keyboard and composer insets and also reacts to content growing below.
  const hasMessages = ordered.length > 0;
  useEffect(() => {
    const state = hasMessages ? listRef.current?.getState() : undefined;
    if (!state) return;
    setShowJumpToLatest(!state.isNearEnd);
    return state.listen("isNearEnd", (nearEnd) =>
      setShowJumpToLatest(!nearEnd),
    );
  }, [hasMessages]);
  const jumpToLatest = useCallback(() => {
    setShowJumpToLatest(false);
    void listRef.current?.scrollToEnd({ animated: true });
  }, []);

  if (ordered.length === 0) {
    return <EmptyTranscript topInset={topInset} testID={testID} />;
  }

  return (
    <View style={styles.listRoot}>
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
      />
      <JumpToLatest visible={showJumpToLatest} onPress={jumpToLatest} />
      <NativeModal
        visible={todosOpen}
        onClose={() => setTodosOpen(false)}
        title="Tasks"
        testID="chat-todos-sheet"
      >
        {!todos.length ? (
          <Text style={{ color: theme.mutedText, fontSize: 14 }}>
            No tasks yet.
          </Text>
        ) : (
          todos.map((todo, index) => {
            const done =
              todo.status === "completed" || todo.status === "cancelled";
            return (
              <View
                key={todo.id}
                style={[styles.todoRow, { borderBottomColor: theme.border }]}
              >
                <View
                  style={[
                    styles.todoMarker,
                    {
                      backgroundColor:
                        todo.status === "in_progress"
                          ? theme.primary
                          : done
                            ? theme.success
                            : theme.border,
                    },
                  ]}
                />
                <View style={{ flex: 1 }}>
                  <Text
                    style={{ color: theme.text, fontSize: 14, lineHeight: 20 }}
                  >
                    {todo.content}
                  </Text>
                  <Text
                    style={{
                      color: theme.subtleText,
                      fontSize: 12,
                      marginTop: 2,
                    }}
                  >
                    {todo.status.replace("_", " ")} · {todo.priority}
                  </Text>
                </View>
                <Text style={{ color: theme.subtleText, fontSize: 12 }}>
                  {index + 1}
                </Text>
              </View>
            );
          })
        )}
      </NativeModal>
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
}: {
  topInset: number;
  testID: string;
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
  sessions: HarnessSession[];
  streaming: boolean;
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
}

const MessageRow = memo(function MessageRow({
  message,
  sessions,
  streaming,
  disabled,
  resolveMedia,
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
  const blocks = useMemo(
    () =>
      buildMessageBlocks(
        [...(message.parts ?? [])].sort(
          (left, right) => (left.position ?? 0) - (right.position ?? 0),
        ),
        { finished: !streaming },
      ),
    [message.parts, streaming],
  );
  const safeContent = useMemo(() => {
    if (isUser) return message.content;
    if ((message.parts ?? []).length === 0) return message.content;
    return blocks
      .filter((block) => block.kind === "text")
      .map((block) => block.part.output)
      .join("");
  }, [blocks, isUser, message.content, message.parts]);
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
              tone="error"
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
          {blocks.length ? (
            blocks.map((block, index) => (
              <BlockView
                key={blockKey(block, index)}
                block={block}
                streaming={streaming}
                sessions={sessions}
                mediaVisible={mediaVisible}
                mediaScopeKey={mediaScopeKey}
                resolveMedia={resolveMedia}
                onOpenFile={onOpenFile}
                onOpenSubtask={onOpenSubtask}
                onRequestPartDetail={onRequestPartDetail}
                onOpenAttachment={onOpenAttachment}
              />
            ))
          ) : safeContent ? (
            <ChatMarkdown
              markdown={safeContent}
              streaming={streaming}
              mediaScopeKey={mediaScopeKey}
              mediaVisible={mediaVisible}
              resolveMedia={resolveMedia}
              onOpenFile={onOpenFile}
            />
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
                style={[styles.cursorBar, { backgroundColor: theme.primary }]}
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
            icon={<Copy size={19} strokeWidth={1.7} color={theme.mutedText} />}
            symbol="doc.on.doc"
            label="Copy message"
            onPress={() => {
              void doCopy();
            }}
            disabled={!copyText}
            size={40}
            testID={`chat-copy-${message.id}`}
          />
          {!disabled && onFork ? (
            <IconButton
              icon={
                <GitBranch
                  size={19}
                  strokeWidth={1.7}
                  color={theme.mutedText}
                />
              }
              symbol="arrow.triangle.branch"
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
                size={20}
                strokeWidth={1.7}
                color={theme.mutedText}
              />
            }
            symbol="info.circle"
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
            <Sparkles size={13} color={theme.primary} />
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
            <FileCode2 size={13} color={theme.primary} />
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
  block: MessageRenderBlock;
  streaming: boolean;
  sessions: HarnessSession[];
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
}

function BlockView({
  block,
  streaming,
  sessions,
  resolveMedia,
  onOpenFile,
  onOpenSubtask,
  onRequestPartDetail,
  onOpenAttachment,
  mediaVisible,
  mediaScopeKey,
}: BlockProps): React.JSX.Element {
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
  if (block.kind === "workedFor")
    return (
      <WorkedForBlock
        block={block}
        streaming={streaming}
        sessions={sessions}
        mediaVisible={mediaVisible}
        mediaScopeKey={mediaScopeKey}
        resolveMedia={resolveMedia}
        onOpenFile={onOpenFile}
        onOpenSubtask={onOpenSubtask}
        onRequestPartDetail={onRequestPartDetail}
        onOpenAttachment={onOpenAttachment}
      />
    );
  if (block.kind === "group")
    return (
      <WorkedGroup
        parts={block.parts}
        onRequestPartDetail={onRequestPartDetail}
        resolveMedia={resolveMedia}
        onOpenFile={onOpenFile}
        onOpenAttachment={onOpenAttachment}
      />
    );
  if (block.kind === "single")
    return (
      <WorkRow
        part={block.part}
        onRequestPartDetail={onRequestPartDetail}
        resolveMedia={resolveMedia}
        onOpenFile={onOpenFile}
        onOpenAttachment={onOpenAttachment}
      />
    );
  if (block.kind === "agent")
    return (
      <ProcessRow
        part={block.part}
        label="Agent plan"
        icon={<Sparkles size={16} color={theme.primary} />}
        onRequestPartDetail={onRequestPartDetail}
      />
    );
  if (block.kind === "compaction")
    return (
      <ProcessRow
        part={block.part}
        label={block.part.title || "Context compacted"}
        icon={<Clock3 size={16} color={theme.subtleText} />}
        onRequestPartDetail={onRequestPartDetail}
      />
    );
  if (block.kind === "card") {
    if (block.part.type === "subtask")
      return (
        <SubtaskCard
          part={block.part}
          sessions={sessions}
          onOpenSubtask={onOpenSubtask}
        />
      );
    if (block.part.type === "patch")
      return (
        <PatchCard
          part={block.part}
          onRequestPartDetail={onRequestPartDetail}
        />
      );
    if (block.part.type === "tool") return <QuestionCard part={block.part} />;
    return (
      <ProcessRow
        part={block.part}
        label={block.part.title || block.part.type}
        icon={<MessageSquareText size={16} color={theme.mutedText} />}
        onRequestPartDetail={onRequestPartDetail}
      />
    );
  }
  return <View />;
}

interface WorkedForProps extends Pick<
  ChatMessagesProps,
  | "resolveMedia"
  | "onOpenFile"
  | "onOpenSubtask"
  | "onRequestPartDetail"
  | "onOpenAttachment"
> {
  block: Extract<MessageRenderBlock, { kind: "workedFor" }>;
  streaming: boolean;
  sessions: HarnessSession[];
  mediaVisible: boolean;
  mediaScopeKey?: string | number;
}

function WorkedForBlock(props: WorkedForProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const { theme } = useTheme();
  const workCount = props.block.blocks.reduce(
    (count, item) =>
      count +
      (item.kind === "group"
        ? item.parts.length
        : item.kind === "text"
          ? 0
          : 1),
    0,
  );
  return (
    <View style={styles.workedFor} testID="chat-worked-for">
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${open ? "Collapse" : "Expand"} work details, ${workCount} items`}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen(!open)}
        style={styles.workedForHeader}
      >
        {open ? (
          <ChevronDown size={16} color={theme.mutedText} />
        ) : (
          <ChevronRight size={16} color={theme.mutedText} />
        )}
        <Hammer size={15} color={theme.primary} />
        <Text style={[styles.workedForTitle, { color: theme.text }]}>
          Worked
        </Text>
        <Text style={[styles.workedForCount, { color: theme.subtleText }]}>
          {workCount} {workCount === 1 ? "step" : "steps"}
        </Text>
      </NativeAction>
      {open ? (
        <View
          style={[styles.workedForContent, { borderLeftColor: theme.border }]}
        >
          {props.block.blocks.map((inner, index) => (
            <BlockView
              key={blockKey(inner, index)}
              block={inner}
              streaming={props.streaming}
              sessions={props.sessions}
              resolveMedia={props.resolveMedia}
              onOpenFile={props.onOpenFile}
              onOpenSubtask={props.onOpenSubtask}
              onRequestPartDetail={props.onRequestPartDetail}
              onOpenAttachment={props.onOpenAttachment}
              mediaVisible={props.mediaVisible}
              mediaScopeKey={props.mediaScopeKey}
            />
          ))}
        </View>
      ) : null}
    </View>
  );
}

type WorkDetailProps = Pick<
  ChatMessagesProps,
  "onRequestPartDetail" | "resolveMedia" | "onOpenFile" | "onOpenAttachment"
> & { mediaVisible?: boolean; mediaScopeKey?: string | number };
function WorkedGroup({
  parts,
  onRequestPartDetail,
  resolveMedia,
  onOpenFile,
  onOpenAttachment,
  mediaVisible = true,
  mediaScopeKey,
}: { parts: HarnessPart[] } & WorkDetailProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const { theme } = useTheme();
  const active = parts.find(
    (part) => part.state === "running" || part.state === "pending",
  );
  return (
    <View style={styles.workGroup} testID="chat-worked-group">
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${open ? "Collapse" : "Expand"} ${parts.length} tool steps`}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen(!open)}
        style={styles.workedForHeader}
      >
        {open ? (
          <ChevronDown size={15} color={theme.mutedText} />
        ) : (
          <ChevronRight size={15} color={theme.mutedText} />
        )}
        <Text style={[styles.workedForTitle, { color: theme.mutedText }]}>
          Worked
        </Text>
        <Text style={[styles.workedForCount, { color: theme.subtleText }]}>
          {parts.length}
        </Text>
        {active ? (
          <>
            <Text
              numberOfLines={1}
              style={[styles.liveLabel, { color: theme.mutedText }]}
            >
              {partLabel(active)}
            </Text>
            <LoaderCircle size={14} color={theme.primary} />
          </>
        ) : null}
      </NativeAction>
      {open ? (
        <View style={styles.groupContent}>
          {parts.map((part) => (
            <WorkRow
              key={part.id}
              part={part}
              grouped
              onRequestPartDetail={onRequestPartDetail}
              resolveMedia={resolveMedia}
              onOpenFile={onOpenFile}
              onOpenAttachment={onOpenAttachment}
              mediaVisible={mediaVisible}
              mediaScopeKey={mediaScopeKey}
            />
          ))}
        </View>
      ) : null}
    </View>
  );
}

function WorkRow({
  part,
  grouped = false,
  onRequestPartDetail,
  resolveMedia,
  onOpenFile,
  onOpenAttachment,
  mediaVisible = true,
  mediaScopeKey,
}: {
  part: HarnessPart;
  grouped?: boolean;
} & WorkDetailProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<HarnessPart | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const { theme } = useTheme();
  const current = detail ?? part;
  const attachments = toolAttachments(current);
  const preview =
    current.type === "reasoning"
      ? (current.display?.summary ?? current.output)
      : (current.display?.summary ?? "");
  const loadDetail = async () => {
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
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && part.detail_loaded === false && !detail) void loadDetail();
  };
  const icon =
    current.type === "reasoning" ? (
      <Sparkles size={15} color={theme.mutedText} />
    ) : current.type === "tool" ? (
      <Code2
        size={15}
        color={current.state === "error" ? theme.danger : theme.mutedText}
      />
    ) : (
      <MessageSquareText size={15} color={theme.mutedText} />
    );
  const title = current.type === "reasoning" ? "Reasoning" : partLabel(current);
  return (
    <View
      style={grouped ? styles.groupRow : styles.standaloneRow}
      testID={`chat-work-row-${part.id}`}
    >
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${open ? "Collapse" : "Expand"} ${title}`}
        accessibilityState={{ expanded: open }}
        onPress={toggle}
        style={styles.workedForHeader}
      >
        {open ? (
          <ChevronDown size={15} color={theme.mutedText} />
        ) : (
          <ChevronRight size={15} color={theme.mutedText} />
        )}
        {icon}
        <Text
          numberOfLines={1}
          style={[
            styles.workRowTitle,
            {
              color: current.state === "error" ? theme.danger : theme.mutedText,
            },
          ]}
        >
          {title}
        </Text>
        {!open && preview ? (
          <Text
            numberOfLines={1}
            style={[styles.workPreview, { color: theme.subtleText }]}
          >
            {preview}
          </Text>
        ) : null}
        {current.state === "running" || current.state === "pending" ? (
          <LoaderCircle size={14} color={theme.primary} />
        ) : null}
      </NativeAction>
      {open ? (
        <View style={[styles.workDetail, { borderLeftColor: theme.border }]}>
          {loading ? (
            <Text
              style={{
                color: theme.mutedText,
                fontSize: 13,
                paddingVertical: 8,
              }}
            >
              Loading full details…
            </Text>
          ) : null}
          {error ? (
            <Banner
              tone="error"
              message="Could not load this detail."
              actionLabel="Retry"
              onAction={() => {
                void loadDetail();
              }}
            />
          ) : null}
          {!loading && !error ? (
            <>
              {current.type === "reasoning" && current.output ? (
                <ChatMarkdown
                  markdown={current.output}
                  mediaVisible={mediaVisible}
                  mediaScopeKey={mediaScopeKey}
                  resolveMedia={resolveMedia}
                  onOpenFile={onOpenFile}
                />
              ) : null}
              {current.title && current.type === "tool" ? (
                <Text style={[styles.detailTitle, { color: theme.text }]}>
                  {current.title}
                </Text>
              ) : null}
              {current.input && Object.keys(current.input).length ? (
                <CodePanel title="Input" value={formatData(current.input)} />
              ) : null}
              {current.type !== "reasoning" && current.output ? (
                <ChatMarkdown
                  markdown={current.output}
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
            </>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

function ProcessRow({
  part,
  label,
  icon,
  onRequestPartDetail,
  mediaVisible = true,
  mediaScopeKey,
}: {
  part: HarnessPart;
  label: string;
  icon: React.ReactNode;
  onRequestPartDetail?: ChatMessagesProps["onRequestPartDetail"];
  mediaVisible?: boolean;
  mediaScopeKey?: string | number;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<HarnessPart | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const { theme } = useTheme();
  const current = detail ?? part;
  const loadDetail = async () => {
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
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && part.detail_loaded === false && !detail) void loadDetail();
  };
  return (
    <View testID={`chat-process-${part.id}`} style={styles.processRow}>
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${open ? "Collapse" : "Expand"} ${label}`}
        accessibilityState={{ expanded: open }}
        onPress={toggle}
        style={styles.workedForHeader}
      >
        {open ? (
          <ChevronDown size={15} color={theme.mutedText} />
        ) : (
          <ChevronRight size={15} color={theme.mutedText} />
        )}
        {icon}
        <Text style={[styles.workRowTitle, { color: theme.mutedText }]}>
          {label}
        </Text>
        {part.state === "running" ? (
          <LoaderCircle size={14} color={theme.primary} />
        ) : null}
      </NativeAction>
      {open ? (
        <View style={[styles.workDetail, { borderLeftColor: theme.border }]}>
          {loading ? (
            <Text style={{ color: theme.mutedText, fontSize: 13 }}>
              Loading details…
            </Text>
          ) : error ? (
            <Banner
              tone="error"
              message="Could not load this detail."
              actionLabel="Retry"
              onAction={() => {
                void loadDetail();
              }}
            />
          ) : current.output ? (
            <ChatMarkdown
              markdown={current.output}
              mediaVisible={mediaVisible}
              mediaScopeKey={mediaScopeKey}
            />
          ) : current.input ? (
            <CodePanel title="Details" value={formatData(current.input)} />
          ) : (
            <Text style={{ color: theme.subtleText, fontSize: 13 }}>
              No additional details.
            </Text>
          )}
        </View>
      ) : null}
    </View>
  );
}

function PatchCard({
  part,
  onRequestPartDetail,
}: {
  part: HarnessPart;
  onRequestPartDetail?: ChatMessagesProps["onRequestPartDetail"];
  mediaVisible?: boolean;
  mediaScopeKey?: string | number;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<HarnessPart | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const { theme } = useTheme();
  const current = detail ?? part;
  const path = String(
    current.display?.path ??
      current.meta?.path ??
      current.title ??
      "Changed file",
  );
  const additions = Number(current.display?.additions ?? 0);
  const deletions = Number(current.display?.deletions ?? 0);
  const loadDetail = async () => {
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
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && part.detail_loaded === false && !detail) void loadDetail();
  };
  return (
    <View
      testID={`chat-patch-${part.id}`}
      style={[
        styles.patchCard,
        { backgroundColor: theme.surface, borderColor: theme.border },
      ]}
    >
      <NativeAction
        accessibilityRole="button"
        accessibilityLabel={`${open ? "Collapse" : "Expand"} changes to ${path}`}
        accessibilityState={{ expanded: open }}
        onPress={toggle}
        style={styles.patchHeader}
      >
        {open ? (
          <ChevronDown size={15} color={theme.mutedText} />
        ) : (
          <ChevronRight size={15} color={theme.mutedText} />
        )}
        <FileCode2 size={15} color={theme.primary} />
        <Text
          numberOfLines={1}
          style={[styles.patchPath, { color: theme.text }]}
        >
          {path}
        </Text>
        {additions ? (
          <Text style={{ color: theme.success, fontSize: 12 }}>
            +{additions}
          </Text>
        ) : null}
        {deletions ? (
          <Text style={{ color: theme.danger, fontSize: 12 }}>
            −{deletions}
          </Text>
        ) : null}
      </NativeAction>
      {open ? (
        <View style={[styles.diff, { backgroundColor: theme.codeBackground }]}>
          {loading ? (
            <Text style={{ color: theme.mutedText, fontSize: 13 }}>
              Loading full diff…
            </Text>
          ) : null}
          {error ? (
            <Banner
              tone="error"
              message="Could not load the full diff."
              actionLabel="Retry"
              onAction={() => {
                void loadDetail();
              }}
            />
          ) : null}
          {!loading && !error ? (
            <Text
              selectable
              style={[styles.diffText, { color: theme.mutedText }]}
            >
              {current.output ||
                (current.display?.preview ?? [])
                  .map(
                    (line) =>
                      `${line.type === "add" ? "+" : line.type === "del" ? "−" : " "}${line.content}`,
                  )
                  .join("\n") ||
                "No diff preview available."}
            </Text>
          ) : null}
        </View>
      ) : null}
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
      <FileCode2 size={18} color={theme.primary} />
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

function SubtaskCard({
  part,
  sessions,
  onOpenSubtask,
}: {
  part: HarnessPart;
  sessions: HarnessSession[];
  onOpenSubtask?: ChatMessagesProps["onOpenSubtask"];
}): React.JSX.Element {
  const { theme } = useTheme();
  const child = resolveChildSessionId(part, sessions) ?? "";
  const agent = String(part.meta?.agent ?? part.display?.agent ?? "Agent");
  return (
    <NativeAction
      accessibilityRole="button"
      accessibilityLabel={`${part.title || "Subtask"}, ${agent}${child ? ", open subtask conversation" : ""}`}
      disabled={!child}
      onPress={() => onOpenSubtask?.(child)}
      testID={`chat-subtask-${part.id}`}
      style={[styles.subtaskCard, { backgroundColor: theme.surfaceMuted }]}
    >
      <View style={[styles.subtaskMark, { backgroundColor: theme.surface }]}>
        <Sparkles
          size={16}
          color={part.state === "running" ? theme.primary : theme.mutedText}
        />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={[styles.subtaskTitle, { color: theme.text }]}>
          {part.title || "Subtask"}
        </Text>
        <Text style={[styles.subtaskMeta, { color: theme.mutedText }]}>
          {agent} ·{" "}
          {part.state === "running"
            ? "Working"
            : part.state === "error"
              ? "Failed"
              : "Completed"}
        </Text>
        {part.output ? (
          <Text
            numberOfLines={2}
            style={[
              styles.subtaskMeta,
              { color: theme.mutedText, marginTop: 4 },
            ]}
          >
            {part.output}
          </Text>
        ) : null}
      </View>
      {child ? <ChevronRight size={17} color={theme.mutedText} /> : null}
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
        <MessageSquareText size={16} color={theme.primary} />
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

function TodoStrip({
  todos,
  onPress,
}: {
  todos: HarnessTodo[];
  onPress: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const active = todos
    .filter(
      (todo) => todo.status !== "completed" && todo.status !== "cancelled",
    )
    .sort((left, right) => left.order - right.order);
  const label = active.length
    ? `${active[0]?.status === "in_progress" ? "In progress · " : ""}${active[0]?.content}${active.length > 1 ? ` · +${active.length - 1}` : ""}`
    : "All tasks complete";
  return (
    <NativeAction
      accessibilityRole="button"
      accessibilityLabel={`Show tasks. ${active.length} open of ${todos.length}`}
      onPress={onPress}
      style={[styles.todoSummary, { backgroundColor: theme.surfaceMuted }]}
    >
      <Text
        numberOfLines={2}
        style={{
          flex: 1,
          color: active.length ? theme.mutedText : theme.success,
          fontSize: 13,
          lineHeight: 18,
        }}
      >
        {label}
      </Text>
      <Text style={{ color: theme.subtleText, fontSize: 12 }}>
        {todos.filter((todo) => todo.status === "completed").length}/
        {todos.length}
      </Text>
      <ChevronRight size={15} color={theme.mutedText} />
    </NativeAction>
  );
}

function formatData(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
function partLabel(part: HarnessPart): string {
  const tool = String(
    part.input?.tool ?? part.tool ?? part.display?.tool ?? part.type,
  );
  if (part.type === "reasoning") return "Reasoning";
  return part.title || tool.replace(/[_-]+/g, " ");
}
function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
function blockKey(
  block: MessageRenderBlock | RenderBlock,
  index: number,
): string {
  if (block.kind === "workedFor") {
    const first = block.blocks[0];
    return `worked:${first?.kind === "group" ? (first.parts[0]?.id ?? index) : first?.kind === "text" ? first.part.id : first?.kind === "compaction" ? first.part.id : (first?.part.id ?? index)}`;
  }
  if (block.kind === "group") return `group:${block.parts[0]?.id ?? index}`;
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
  todoSummary: {
    flexDirection: "row",
    gap: 10,
    alignItems: "center",
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 14,
  },
  todoRow: {
    flexDirection: "row",
    gap: 11,
    alignItems: "center",
    minHeight: 56,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  todoMarker: { width: 8, height: 8, borderRadius: 5 },
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
  assistantBody: { width: "100%", gap: 10 },
  steerRow: { alignSelf: "flex-end", alignItems: "flex-end", maxWidth: "94%" },
  steerCaption: { fontSize: 11, lineHeight: 16, marginTop: 3 },
  textBlock: { width: "100%" },
  messageActions: {
    minHeight: 36,
    flexDirection: "row",
    alignItems: "center",
    gap: 0,
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
  workedFor: { width: "100%", paddingVertical: 8 },
  workedForHeader: {
    minHeight: 40,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  workedForTitle: { fontSize: 14, fontWeight: "600" },
  workedForCount: { fontSize: 12 },
  workedForContent: {
    borderLeftWidth: 1,
    marginLeft: 7,
    paddingLeft: 12,
    paddingVertical: 4,
    gap: 10,
  },
  workGroup: { gap: 3 },
  groupContent: { paddingLeft: 21, gap: 2 },
  liveLabel: { flex: 1, fontSize: 13 },
  standaloneRow: { width: "100%", gap: 5 },
  groupRow: { width: "100%", gap: 3 },
  workRowTitle: { maxWidth: "45%", fontSize: 13 },
  workPreview: { flex: 1, fontSize: 12 },
  workDetail: {
    borderLeftWidth: 1,
    marginLeft: 7,
    paddingLeft: 12,
    paddingVertical: 8,
    gap: 8,
  },
  detailTitle: { fontSize: 14, lineHeight: 20, fontWeight: "600" },
  codePanel: { padding: 12, borderRadius: 12, gap: 6 },
  codeTitle: {
    fontSize: 11,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
  codeText: { fontSize: 12, lineHeight: 18, fontFamily: "monospace" },
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
  processRow: { width: "100%", gap: 4, paddingVertical: 3 },
  patchCard: {
    width: "100%",
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 14,
    overflow: "hidden",
  },
  patchHeader: {
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 12,
  },
  patchPath: { flex: 1, fontSize: 13, fontWeight: "500" },
  diff: { maxHeight: 320, padding: 12 },
  diffText: { fontFamily: "monospace", fontSize: 12, lineHeight: 18 },
  subtaskCard: {
    width: "100%",
    borderRadius: 16,
    padding: 12,
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 11,
  },
  subtaskMark: {
    width: 30,
    height: 30,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
  },
  subtaskTitle: { fontSize: 14, lineHeight: 20, fontWeight: "600" },
  subtaskMeta: { fontSize: 12, lineHeight: 17 },
  questionCard: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 15,
    padding: 13,
    gap: 9,
  },
  questionHeader: { flexDirection: "row", alignItems: "center", gap: 8 },
});
